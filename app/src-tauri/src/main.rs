// Prevents additional console window on Windows in release
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use rusqlite::{Connection, OpenFlags};
use serde::Serialize;
use std::fs;
use std::path::PathBuf;
use std::process::Command;
use tauri::Manager;

fn db_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| String::from("/tmp"));
    PathBuf::from(home)
        .join("Library")
        .join("Application Support")
        .join("brainloop")
        .join("activity.db")
}

fn open_read_only() -> Result<Connection, String> {
    let path = db_path();
    if !path.exists() {
        return Err(format!("db missing at {}", path.display()));
    }
    Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|e| e.to_string())
}

#[tauri::command]
fn row_count() -> Result<i64, String> {
    let path = db_path();
    if !path.exists() {
        return Ok(0);
    }
    let conn = open_read_only()?;
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM activity_log", [], |row| row.get(0))
        .map_err(|e| e.to_string())?;
    Ok(count)
}

#[derive(Serialize)]
struct DaySummary {
    date: String,
    generated_at: i64,
    model: String,
    activity_rows: i64,
    tokens_in: Option<i64>,
    tokens_out: Option<i64>,
    // The LLM payload as a parsed JSON value so the frontend consumes it directly.
    payload: serde_json::Value,
}

/// Returns the most recent day_summary row for today (local date). Returns
/// None when either the database is missing the table (daemon not yet updated),
/// or no row has been generated yet — frontend should show a listening state.
#[tauri::command]
fn today_summary() -> Result<Option<DaySummary>, String> {
    let path = db_path();
    if !path.exists() {
        return Ok(None);
    }
    let conn = open_read_only()?;

    // If the table doesn't exist yet, return None gracefully.
    let table_check: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='day_summary'",
            [],
            |row| row.get(0),
        )
        .map_err(|e| e.to_string())?;
    if table_check == 0 {
        return Ok(None);
    }

    // Today's local date — compute via SQLite so it matches the analyzer's PK.
    let mut stmt = conn
        .prepare(
            "SELECT date, generated_at, model, activity_rows, tokens_in, tokens_out, payload_json
             FROM day_summary
             WHERE date = date('now','localtime')
             LIMIT 1",
        )
        .map_err(|e| e.to_string())?;

    let mut rows = stmt.query([]).map_err(|e| e.to_string())?;
    let Some(row) = rows.next().map_err(|e| e.to_string())? else {
        return Ok(None);
    };

    let date: String = row.get(0).map_err(|e| e.to_string())?;
    let generated_at: i64 = row.get(1).map_err(|e| e.to_string())?;
    let model: String = row.get(2).map_err(|e| e.to_string())?;
    let activity_rows: i64 = row.get(3).map_err(|e| e.to_string())?;
    let tokens_in: Option<i64> = row.get(4).map_err(|e| e.to_string())?;
    let tokens_out: Option<i64> = row.get(5).map_err(|e| e.to_string())?;
    let payload_json: String = row.get(6).map_err(|e| e.to_string())?;

    let payload: serde_json::Value =
        serde_json::from_str(&payload_json).map_err(|e| e.to_string())?;

    Ok(Some(DaySummary {
        date,
        generated_at,
        model,
        activity_rows,
        tokens_in,
        tokens_out,
        payload,
    }))
}

#[derive(Serialize)]
struct DaemonStatus {
    running: bool,
    last_row_age_secs: Option<i64>,
    total_today: i64,
}

/// Returns a lightweight status report for the Settings screen: whether the
/// capture daemon has produced a row recently, and how many rows landed today.
#[tauri::command]
fn daemon_status() -> Result<DaemonStatus, String> {
    let path = db_path();
    if !path.exists() {
        return Ok(DaemonStatus { running: false, last_row_age_secs: None, total_today: 0 });
    }
    let conn = open_read_only()?;

    let last_ts: Option<f64> = conn
        .query_row("SELECT MAX(ts) FROM activity_log", [], |row| row.get(0))
        .ok()
        .flatten();
    let total_today: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM activity_log WHERE ts >= strftime('%s', date('now','localtime'))",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);

    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let (running, last_row_age_secs) = match last_ts {
        Some(ts) => {
            let age = now - ts as i64;
            // Treat <180s as running (heartbeat fires every 60s with 2x buffer).
            (age < 180, Some(age))
        }
        None => (false, None),
    };

    Ok(DaemonStatus { running, last_row_age_secs, total_today })
}

#[derive(Serialize)]
struct AppSlice {
    app: String,
    minutes: i64,
}

#[derive(Serialize)]
struct BucketApps {
    apps: Vec<AppSlice>,
    total_rows: i64,
}

/// Top apps inside a `[start_ts, end_ts)` time window, ordered by heartbeat
/// row count. Used by the waveform hover tooltip to say what the user was
/// actually doing in that 10-minute bucket. Each `minutes` is the number of
/// activity_log rows (heartbeats + events), not literal minutes — but at the
/// current 60 s heartbeat cadence it's close enough that the label reads cleanly.
#[tauri::command]
fn bucket_apps(start_ts: i64, end_ts: i64) -> Result<BucketApps, String> {
    let path = db_path();
    if !path.exists() {
        return Ok(BucketApps { apps: vec![], total_rows: 0 });
    }
    let conn = open_read_only()?;

    let total_rows: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM activity_log WHERE ts >= ? AND ts < ?",
            [start_ts, end_ts],
            |row| row.get(0),
        )
        .unwrap_or(0);

    let mut stmt = conn
        .prepare(
            "SELECT COALESCE(app_name, '—') AS app, COUNT(*) AS n
             FROM activity_log
             WHERE ts >= ? AND ts < ?
             GROUP BY app
             ORDER BY n DESC
             LIMIT 3",
        )
        .map_err(|e| e.to_string())?;

    let apps: Vec<AppSlice> = stmt
        .query_map([start_ts, end_ts], |row| {
            Ok(AppSlice {
                app: row.get::<_, String>(0)?,
                minutes: row.get::<_, i64>(1)?,
            })
        })
        .map_err(|e| e.to_string())?
        .filter_map(|r| r.ok())
        .collect();

    Ok(BucketApps { apps, total_rows })
}

#[derive(Serialize)]
struct PermissionsStatus {
    accessibility: String,     // "granted" | "not_granted" | "unknown"
    screen_recording: String,  // always "optional" — we don't use it
}

/// Best-effort Accessibility detection for the daemon.
///
/// We don't call AXIsProcessTrusted() directly — TCC grants are per-binary,
/// and the UI and daemon are different binaries, so a UI self-check would
/// lie. Instead we look at capture behavior over the LAST 10 heartbeat
/// rows (~10 min). For each row we know whether window_title was captured.
/// A majority rule shakes off stale rows from a freshly-revoked grant:
///
///   - ≥ 4/10 non-loginwindow rows with a populated window_title → granted.
///     (Threshold < 50% so a quick grant while the user is mid-app-switch
///     still flips green.)
///   - ≥ 4/10 non-loginwindow rows with no title          → not_granted.
///   - too few non-loginwindow rows (locked screen, etc.) → unknown.
///
/// If the user revokes AX, old titled rows still sit in the DB, but within
/// ~5-10 minutes the new null-title heartbeats outnumber them and the
/// badge flips. Same behavior on grant — new titles quickly dominate.
#[tauri::command]
fn permissions_status() -> Result<PermissionsStatus, String> {
    let mut out = PermissionsStatus {
        accessibility: "unknown".into(),
        screen_recording: "optional".into(),
    };
    let path = db_path();
    if !path.exists() {
        return Ok(out);
    }
    let conn = open_read_only()?;

    let mut stmt = conn
        .prepare(
            "SELECT window_title, app_name FROM activity_log
             WHERE trigger = 'heartbeat'
             ORDER BY ts DESC LIMIT 10",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([], |row| {
            Ok((
                row.get::<_, Option<String>>(0)?,
                row.get::<_, Option<String>>(1)?,
            ))
        })
        .map_err(|e| e.to_string())?;

    let mut non_login_with_title = 0;
    let mut non_login_without_title = 0;
    for r in rows.filter_map(|r| r.ok()) {
        let (title, app) = r;
        let is_user_app = app
            .as_deref()
            .map(|s| !s.is_empty() && s != "loginwindow")
            .unwrap_or(false);
        if !is_user_app {
            continue;
        }
        let has_title = title
            .as_deref()
            .map(|s| !s.trim().is_empty())
            .unwrap_or(false);
        if has_title {
            non_login_with_title += 1;
        } else {
            non_login_without_title += 1;
        }
    }

    const THRESHOLD: u32 = 4; // out of 10
    out.accessibility = if non_login_with_title >= THRESHOLD {
        "granted".into()
    } else if non_login_without_title >= THRESHOLD {
        "not_granted".into()
    } else {
        "unknown".into()
    };

    Ok(out)
}

/// Open the correct macOS System Settings pane for a permission the user
/// needs to toggle. Takes a short key ("accessibility", "apple_events",
/// "screen_recording") and runs `open x-apple.systempreferences:...`.
#[tauri::command]
fn open_permission_pane(kind: String) -> Result<(), String> {
    let url = match kind.as_str() {
        "accessibility"  => "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        "apple_events"   => "x-apple.systempreferences:com.apple.preference.security?Privacy_AppleEvents",
        "screen_recording" => "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
        _ => return Err(format!("unknown permission kind: {}", kind)),
    };
    Command::new("open")
        .arg(url)
        .status()
        .map_err(|e| format!("open: {}", e))?;
    Ok(())
}

#[derive(Serialize)]
struct AiConfig {
    provider: String,
    model: String,
    base_url: String,
    // key_hint is a non-sensitive "is one set + last 4" display — never the raw key.
    key_hint: String,
}

/// Read the AI provider config from `app_config`. Returns a minimal default
/// when no row is present yet so the UI can still show a usable form.
#[tauri::command]
fn ai_config_load() -> Result<AiConfig, String> {
    let mut cfg = AiConfig {
        provider: "anthropic".into(),
        model: "claude-sonnet-4-5".into(),
        base_url: "https://api.anthropic.com".into(),
        key_hint: "".into(),
    };
    let path = db_path();
    if !path.exists() {
        return Ok(cfg);
    }
    let conn = open_read_only()?;
    let has_table: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='app_config'",
            [],
            |row| row.get(0),
        )
        .unwrap_or(0);
    if has_table == 0 {
        return Ok(cfg);
    }
    let mut stmt = conn
        .prepare("SELECT key, value FROM app_config")
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)))
        .map_err(|e| e.to_string())?;
    for kv in rows.filter_map(|r| r.ok()) {
        match kv.0.as_str() {
            "ai_provider" => cfg.provider = kv.1,
            "ai_model" => cfg.model = kv.1,
            "ai_base_url" => cfg.base_url = kv.1,
            "ai_api_key" => {
                if !kv.1.is_empty() {
                    let tail: String = kv.1.chars().rev().take(4).collect::<String>().chars().rev().collect();
                    cfg.key_hint = format!("••• {}", tail);
                }
            }
            _ => {}
        }
    }
    Ok(cfg)
}

/// Persist the AI provider config. `api_key` is optional — if empty, keep
/// whatever key is already stored. The daemon reads these values on its next
/// analyzer tick.
#[tauri::command]
fn ai_config_save(
    provider: String,
    model: String,
    base_url: String,
    api_key: String,
) -> Result<(), String> {
    let path = db_path();
    if !path.exists() {
        return Err(format!(
            "database not found at {} — is the daemon installed?",
            path.display()
        ));
    }
    let conn = Connection::open(&path).map_err(|e| e.to_string())?;
    conn.execute(
        "CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        [],
    )
    .map_err(|e| e.to_string())?;
    let tx = conn.unchecked_transaction().map_err(|e| e.to_string())?;
    let upsert = |k: &str, v: &str| -> Result<(), String> {
        tx.execute(
            "INSERT INTO app_config(key,value) VALUES(?1,?2)
             ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [k, v],
        )
        .map(|_| ())
        .map_err(|e| e.to_string())
    };
    upsert("ai_provider", &provider)?;
    upsert("ai_model", &model)?;
    upsert("ai_base_url", &base_url)?;
    if !api_key.is_empty() {
        upsert("ai_api_key", &api_key)?;
    }
    tx.commit().map_err(|e| e.to_string())?;
    Ok(())
}

// ── First-launch daemon install ──────────────────────────────────────────────
//
// When the app starts from a bundled .app (say, /Applications/Brainloop.app),
// we write a LaunchAgent plist pointing at the embedded daemon binary and
// tell launchctl to load it. Idempotent: if the plist already points at this
// binary we do nothing; if the .app moved locations, we regenerate and reload.
//
// Dev-mode safety: if the resource binary doesn't exist (e.g. `cargo tauri
// dev` without `make bundle-daemon`), we skip silently so development isn't
// gated on a release build.

const DAEMON_LABEL: &str = "com.brainloop.agent";

fn launchagent_plist_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
    PathBuf::from(home)
        .join("Library")
        .join("LaunchAgents")
        .join(format!("{}.plist", DAEMON_LABEL))
}

fn runtime_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".into());
    PathBuf::from(home)
        .join("Library")
        .join("Application Support")
        .join("brainloop")
}

fn render_plist(daemon_bin: &PathBuf, rt_dir: &PathBuf) -> String {
    // Built from scratch rather than substituting into the source-mode
    // template — bundle mode runs the compiled binary directly, no
    // python -m indirection.
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{daemon}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{rt}/daemon.log</string>
    <key>StandardErrorPath</key>
    <string>{rt}/daemon-err.log</string>
</dict>
</plist>
"#,
        label = DAEMON_LABEL,
        daemon = daemon_bin.display(),
        rt = rt_dir.display(),
    )
}

fn ensure_daemon_installed(app: &tauri::AppHandle) -> Result<(), String> {
    // 1. Make sure the runtime directory exists (daemon writes its DB + logs there).
    let rt = runtime_dir();
    fs::create_dir_all(&rt).map_err(|e| format!("create runtime dir: {}", e))?;

    // 2. Resolve the embedded daemon binary. In dev mode this resource doesn't
    //    exist — skip silently so `cargo tauri dev` isn't gated on a release build.
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|e| format!("resolve resource dir: {}", e))?;
    let daemon_bin = resource_dir.join("resources").join("brainloopd");

    if !daemon_bin.exists() {
        eprintln!(
            "[brainloop] daemon binary not bundled at {} — dev mode, skipping install",
            daemon_bin.display()
        );
        return Ok(());
    }

    // 3. Render and compare against existing plist. If identical, the daemon
    //    should already be loaded — nothing to do.
    let plist_text = render_plist(&daemon_bin, &rt);
    let plist_path = launchagent_plist_path();
    fs::create_dir_all(plist_path.parent().unwrap())
        .map_err(|e| format!("create LaunchAgents dir: {}", e))?;

    if let Ok(existing) = fs::read_to_string(&plist_path) {
        if existing == plist_text {
            eprintln!("[brainloop] LaunchAgent already current at {}", plist_path.display());
            return Ok(());
        }
        // Existing plist is stale (e.g. user moved the .app). Unload first.
        let _ = Command::new("launchctl")
            .args(["unload", plist_path.to_str().unwrap()])
            .status();
    }

    // 4. Write the new plist and load it.
    fs::write(&plist_path, &plist_text).map_err(|e| format!("write plist: {}", e))?;
    let out = Command::new("launchctl")
        .args(["load", plist_path.to_str().unwrap()])
        .output()
        .map_err(|e| format!("launchctl load: {}", e))?;
    if !out.status.success() {
        return Err(format!(
            "launchctl load failed: {}",
            String::from_utf8_lossy(&out.stderr)
        ));
    }
    eprintln!(
        "[brainloop] Installed LaunchAgent → {}",
        plist_path.display()
    );
    Ok(())
}

fn main() {
    tauri::Builder::default()
        .setup(|app| {
            // Run the installer off the main thread — launchctl can block briefly
            // during unload/load and we don't want to hold up window creation.
            let handle = app.handle().clone();
            std::thread::spawn(move || {
                if let Err(e) = ensure_daemon_installed(&handle) {
                    eprintln!("[brainloop] daemon install failed: {}", e);
                }
            });
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            row_count,
            today_summary,
            daemon_status,
            bucket_apps,
            ai_config_load,
            ai_config_save,
            permissions_status,
            open_permission_pane
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
