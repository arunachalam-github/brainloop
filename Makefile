# Brainloop — macOS activity-capture daemon
#
# Runtime architecture (important: why it looks the way it does):
#
#   $(PROJ_DIR)                      ← this repo, in ~/Documents (source of truth)
#   $(RUNTIME_DIR)                   ← ~/Library/Application Support/brainloop
#     ├── activity.db                ← the SQLite log
#     ├── daemon.log / daemon-err.log
#     ├── venv/                      ← Python venv + pyobjc deps
#     └── src/daemon/                ← synced copy of daemon/ (the module launchd runs)
#
# Why the source gets copied out of Documents:
#   Under macOS Sequoia, LaunchAgents spawned by launchd are blocked by TCC
#   from reading files in ~/Documents. The failure presents as `EDEADLK` /
#   "Resource deadlock avoided" during Python's import bootstrap — extremely
#   confusing, not an obvious permission error. Keeping runtime code under
#   Application Support dodges the gate entirely.
#
# Why Python 3.13, not 3.14:
#   Python 3.14 has a separate venv-init deadlock bug under launchd.
#
# Override: make install PYTHON=/usr/bin/python3.13

PYTHON       ?= /opt/homebrew/bin/python3.13
PROJ_DIR     := $(shell pwd)
RUNTIME_DIR  := $(HOME)/Library/Application Support/brainloop
VENV_DIR     := $(RUNTIME_DIR)/venv
VENV_PY      := $(VENV_DIR)/bin/python3
SRC_DIR      := $(RUNTIME_DIR)/src
DB_PATH      := $(RUNTIME_DIR)/activity.db
LABEL        := com.brainloop.agent
TEMPLATE     := $(PROJ_DIR)/com.brainloop.agent.plist.template
PLIST_DST    := $(HOME)/Library/LaunchAgents/$(LABEL).plist
KEYCHAIN_SVC := com.brainloop.ai

.PHONY: help install uninstall reinstall restart status logs sync \
        clean clean-cache clean-all venv \
        ui ui-build analyze-now show-summary \
        config-gemini config-anthropic config-openai \
        bundle-daemon bundle clean-bundle

help:
	@echo "Brainloop — available targets:"
	@echo ""
	@echo "  daemon:"
	@echo "    make install      Build venv, sync source, load LaunchAgent"
	@echo "    make reinstall    Sync source, regenerate plist, relaunch daemon"
	@echo "    make uninstall    Stop daemon, remove LaunchAgent (data kept)"
	@echo "    make restart      Restart the daemon"
	@echo "    make status       Show LaunchAgent state + recent log lines"
	@echo "    make logs         Tail the live log (Ctrl-C to stop)"
	@echo ""
	@echo "  analyzer + UI:"
	@echo "    make config-gemini    Write Gemini provider config to app_config"
	@echo "    make config-anthropic Write Anthropic provider config"
	@echo "    make config-openai    Write OpenAI provider config"
	@echo "    make analyze-now      Force one analyzer tick (bypasses gates)"
	@echo "    make show-summary     Print today's generated headline + acts"
	@echo "    make ui               Launch the Tauri desktop app (cargo tauri dev)"
	@echo "    make ui-build         Produce a release .app bundle"
	@echo ""
	@echo "  cleanup:"
	@echo "    make clean-cache  Wipe __pycache__/ under runtime source"
	@echo "    make clean        Remove the runtime venv (run uninstall first)"
	@echo "    make clean-all    Remove venv + synced source + plist (DB kept)"

# ── venv lifecycle ─────────────────────────────────────────────────────────────

$(VENV_PY):
	@mkdir -p "$(RUNTIME_DIR)"
	@echo "→ Creating venv at $(VENV_DIR)"
	@$(PYTHON) -m venv "$(VENV_DIR)"
	@"$(VENV_PY)" -m pip install --quiet --upgrade pip
	@"$(VENV_PY)" -m pip install --quiet -r "$(PROJ_DIR)/requirements.txt"
	@echo "→ Deps installed"

venv: $(VENV_PY)

# ── source sync (repo → runtime) ───────────────────────────────────────────────

sync:
	@mkdir -p "$(SRC_DIR)"
	@rsync -a --delete --exclude '__pycache__' "$(PROJ_DIR)/daemon" "$(SRC_DIR)/"
	@echo "→ Synced daemon/ → $(SRC_DIR)/daemon"

# ── install / reinstall ────────────────────────────────────────────────────────

install: $(VENV_PY) sync
	@sed -e 's|{{PYTHON3_PATH}}|$(VENV_PY)|g' \
	     -e 's|{{PROJECT_DIR}}|$(SRC_DIR)|g' \
	     -e 's|{{HOME}}|$(HOME)|g' \
	     "$(TEMPLATE)" > "$(PLIST_DST)"
	@launchctl unload "$(PLIST_DST)" 2>/dev/null || true
	@pkill -9 -f 'daemon\.daemon' 2>/dev/null || true
	@sleep 2
	@launchctl load "$(PLIST_DST)"
	@echo "✓ Loaded $(LABEL)"
	@echo "  Python:  $(VENV_PY)"
	@echo "  Source:  $(SRC_DIR)/daemon"
	@echo "  DB:      $(RUNTIME_DIR)/activity.db"
	@echo ""
	@echo "If Accessibility isn't granted yet:"
	@echo "  System Settings → Privacy & Security → Accessibility"
	@echo "  Add: $(VENV_PY)"
	@echo "Chrome page_text capture:"
	@echo "  Chrome → View → Developer → Allow JavaScript from Apple Events"

reinstall: sync clean-cache
	@sed -e 's|{{PYTHON3_PATH}}|$(VENV_PY)|g' \
	     -e 's|{{PROJECT_DIR}}|$(SRC_DIR)|g' \
	     -e 's|{{HOME}}|$(HOME)|g' \
	     "$(TEMPLATE)" > "$(PLIST_DST)"
	@launchctl unload "$(PLIST_DST)" 2>/dev/null || true
	@pkill -9 -f 'daemon\.daemon' 2>/dev/null || true
	@sleep 2
	@launchctl load "$(PLIST_DST)"
	@echo "✓ Reloaded $(LABEL)"

# ── runtime control ────────────────────────────────────────────────────────────

uninstall:
	@launchctl unload "$(PLIST_DST)" 2>/dev/null || true
	@pkill -9 -f 'daemon\.daemon' 2>/dev/null || true
	@rm -f "$(PLIST_DST)"
	@echo "Unloaded $(LABEL)  (runtime dir + DB preserved: $(RUNTIME_DIR))"

restart:
	@launchctl unload "$(PLIST_DST)" 2>/dev/null || true
	@pkill -9 -f 'daemon\.daemon' 2>/dev/null || true
	@sleep 2
	@launchctl load "$(PLIST_DST)"
	@echo "Restarted $(LABEL)"

status:
	@echo "=== launchctl list ==="
	@launchctl list $(LABEL) 2>/dev/null || echo "  (not loaded)"
	@echo ""
	@echo "=== last 20 log lines ==="
	@tail -20 "$(RUNTIME_DIR)/daemon.log" 2>/dev/null || echo "  (no log yet)"
	@echo ""
	@echo "=== last 10 error lines ==="
	@tail -10 "$(RUNTIME_DIR)/daemon-err.log" 2>/dev/null || echo "  (no errors)"

logs:
	@tail -f "$(RUNTIME_DIR)/daemon.log"

# ── cleanup ────────────────────────────────────────────────────────────────────

clean-cache:
	@find "$(SRC_DIR)" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@echo "→ Cleared __pycache__/ under $(SRC_DIR)"

clean:
	rm -rf "$(VENV_DIR)"

clean-all: uninstall
	rm -rf "$(VENV_DIR)" "$(SRC_DIR)"
	@echo "Removed venv + synced source. DB preserved at $(RUNTIME_DIR)/activity.db"

# ── Analyzer (LLM day-summary) ────────────────────────────────────────────────

# Write the provider settings into the shared `app_config` table. The API key
# itself lives in Keychain under service "$(KEYCHAIN_SVC)" — add it yourself
# with `security add-generic-password -s $(KEYCHAIN_SVC) -a <provider> -w`
# because we never want a key printed in the shell history.
config-gemini:
	@sqlite3 "$(DB_PATH)" "INSERT OR REPLACE INTO app_config(key, value) VALUES ('ai_provider','gemini'), ('ai_model','gemini-2.5-flash'), ('ai_base_url','https://generativelanguage.googleapis.com/v1beta/openai'), ('ai_key_ref','$(KEYCHAIN_SVC):gemini');"
	@echo "✓ app_config set for Gemini (gemini-2.5-flash)"
	@echo "  Now add your key:  security add-generic-password -s $(KEYCHAIN_SVC) -a gemini -w"

config-anthropic:
	@sqlite3 "$(DB_PATH)" "INSERT OR REPLACE INTO app_config(key, value) VALUES ('ai_provider','anthropic'), ('ai_model','claude-sonnet-4-5'), ('ai_base_url','https://api.anthropic.com'), ('ai_key_ref','$(KEYCHAIN_SVC):anthropic');"
	@echo "✓ app_config set for Anthropic (claude-sonnet-4-5)"
	@echo "  Now add your key:  security add-generic-password -s $(KEYCHAIN_SVC) -a anthropic -w"

config-openai:
	@sqlite3 "$(DB_PATH)" "INSERT OR REPLACE INTO app_config(key, value) VALUES ('ai_provider','openai'), ('ai_model','gpt-4o-mini'), ('ai_base_url','https://api.openai.com/v1'), ('ai_key_ref','$(KEYCHAIN_SVC):openai');"
	@echo "✓ app_config set for OpenAI (gpt-4o-mini)"
	@echo "  Now add your key:  security add-generic-password -s $(KEYCHAIN_SVC) -a openai -w"

# Force one analyzer run against the repo's current daemon/ source,
# bypassing the 20-min regen gate and the "no new rows" gate. Requires
# the Keychain key + app_config rows to already exist.
analyze-now:
	@PYTHONPATH="$(PROJ_DIR)" "$(VENV_PY)" -m daemon.analyze --once --force

# Pretty-print today's day_summary row for a quick smoke check.
show-summary:
	@sqlite3 "$(DB_PATH)" "SELECT '  date: ' || date || char(10) || '  generated_at: ' || datetime(generated_at,'unixepoch','localtime') || char(10) || '  model: ' || model || char(10) || '  activity_rows: ' || activity_rows || char(10) || '  tokens_in/out: ' || tokens_in || ' / ' || tokens_out || char(10) || '  headline: ' || json_extract(payload_json,'\$$.headline') FROM day_summary WHERE date=date('now','localtime');" 2>/dev/null || echo "  (no day_summary row for today yet — run 'make analyze-now')"

# ── UI (Tauri desktop app) ────────────────────────────────────────────────────

# Dev launch: hot-reloads JS/HTML/CSS, rebuilds Rust on change.
# First launch takes ~2 min to compile the Tauri tree.
ui:
	@cd "$(PROJ_DIR)/app" && cargo tauri dev

# Release build: produces Brainloop.app under app/src-tauri/target/release/bundle/macos
ui-build:
	@cd "$(PROJ_DIR)/app" && cargo tauri build

# ── Bundling (standalone distribution) ────────────────────────────────────────
#
# `make bundle` produces a single Brainloop.app that friends can download and
# run — no Python, no Homebrew, no `make install`. The daemon is compiled to
# a standalone executable with PyInstaller, embedded inside the .app's
# Resources, and the Tauri UI installs the LaunchAgent on first launch.
#
#   make bundle-daemon  → build/dist/brainloopd (single-file executable)
#   make bundle         → bundle-daemon + cargo tauri build → .app + .dmg

BUILD_DIR      := $(PROJ_DIR)/build
BUILD_VENV     := $(BUILD_DIR)/venv
BUILD_VENV_PY  := $(BUILD_VENV)/bin/python3
BUILD_PYINST   := $(BUILD_VENV)/bin/pyinstaller
DAEMON_BIN     := $(BUILD_DIR)/dist/brainloopd
TAURI_RES_DIR  := $(PROJ_DIR)/app/src-tauri/resources

$(BUILD_VENV_PY):
	@mkdir -p "$(BUILD_DIR)"
	@echo "→ Creating build venv at $(BUILD_VENV)"
	@$(PYTHON) -m venv "$(BUILD_VENV)"
	@"$(BUILD_VENV_PY)" -m pip install --quiet --upgrade pip
	@"$(BUILD_VENV_PY)" -m pip install --quiet -r "$(PROJ_DIR)/requirements.txt"
	@"$(BUILD_VENV_PY)" -m pip install --quiet pyinstaller
	@echo "→ Build venv ready"

# Compile daemon/ into a single-file executable. Output: build/dist/brainloopd
# (~10 MB including pyobjc frameworks). The binary is architecture-specific
# to the host (ARM64 on Apple Silicon, x86_64 on Intel).
bundle-daemon: $(BUILD_VENV_PY)
	@echo "→ Building brainloopd with PyInstaller…"
	@cd "$(BUILD_DIR)" && "$(BUILD_PYINST)" --clean --noconfirm brainloopd.spec
	@ls -lh "$(DAEMON_BIN)"
	@mkdir -p "$(TAURI_RES_DIR)"
	@cp "$(DAEMON_BIN)" "$(TAURI_RES_DIR)/brainloopd"
	@cp "$(PROJ_DIR)/com.brainloop.agent.plist.template" "$(TAURI_RES_DIR)/"
	@echo "→ Copied brainloopd + plist template into $(TAURI_RES_DIR)"

# End-to-end bundle: daemon binary → Tauri release build → Brainloop.app + DMG.
bundle: bundle-daemon
	@echo "→ Building Tauri release bundle…"
	@cd "$(PROJ_DIR)/app" && cargo tauri build
	@echo ""
	@echo "✓ Bundle ready:"
	@ls "$(PROJ_DIR)/app/src-tauri/target/release/bundle/macos/"*.app 2>/dev/null || true
	@ls "$(PROJ_DIR)/app/src-tauri/target/release/bundle/dmg/"*.dmg 2>/dev/null || true

clean-bundle:
	@rm -rf "$(BUILD_DIR)/build" "$(BUILD_DIR)/dist" "$(TAURI_RES_DIR)"
	@echo "→ Cleaned bundle artifacts"
