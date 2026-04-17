// Prevents additional console window on Windows in release
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use rusqlite::{Connection, OpenFlags};
use std::path::PathBuf;

fn db_path() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| String::from("/tmp"));
    PathBuf::from(home)
        .join("Library")
        .join("Application Support")
        .join("brainloop")
        .join("activity.db")
}

#[tauri::command]
fn row_count() -> Result<i64, String> {
    let path = db_path();
    if !path.exists() {
        return Ok(0);
    }
    let conn = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(|e| e.to_string())?;
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM activity_log", [], |row| row.get(0))
        .map_err(|e| e.to_string())?;
    Ok(count)
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![row_count])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
