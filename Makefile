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
LABEL        := com.brainloop.agent
TEMPLATE     := $(PROJ_DIR)/com.brainloop.agent.plist.template
PLIST_DST    := $(HOME)/Library/LaunchAgents/$(LABEL).plist

.PHONY: help install uninstall reinstall restart status logs sync clean clean-cache clean-all venv

help:
	@echo "Brainloop — available targets:"
	@echo "  make install      Build venv, sync source, load LaunchAgent"
	@echo "  make reinstall    Sync source, regenerate plist, relaunch daemon"
	@echo "  make uninstall    Stop daemon, remove LaunchAgent (runtime data kept)"
	@echo "  make restart      Restart the daemon"
	@echo "  make sync         Copy daemon/ source to runtime dir (no restart)"
	@echo "  make status       Show LaunchAgent state + recent log lines"
	@echo "  make logs         Tail the live log (Ctrl-C to stop)"
	@echo "  make clean-cache  Wipe __pycache__/ under runtime source"
	@echo "  make clean        Remove the runtime venv (run uninstall first)"
	@echo "  make clean-all    Remove venv + synced source + plist (DB kept)"

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
