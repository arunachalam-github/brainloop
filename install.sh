#!/usr/bin/env bash
# brainloop install.sh — manage the com.brainloop.agent LaunchAgent
set -euo pipefail

LABEL="com.brainloop.agent"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Application Support/brainloop"

cmd_install() {
    PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
    TEMPLATE="$PROJ_DIR/com.brainloop.agent.plist.template"

    if [[ ! -f "$TEMPLATE" ]]; then
        echo "Error: plist template not found at $TEMPLATE" >&2
        exit 1
    fi

    # Find Python 3.10+
    PYTHON3=""
    for candidate in python3 python3.12 python3.11 python3.10; do
        if path="$(command -v "$candidate" 2>/dev/null)"; then
            ver="$("$path" -c 'import sys; print(sys.version_info >= (3,10))' 2>/dev/null)"
            [[ "$ver" == "True" ]] && PYTHON3="$path" && break
        fi
    done
    if [[ -z "$PYTHON3" ]]; then
        echo "Error: Python 3.10+ not found. Install from https://www.python.org/" >&2
        exit 1
    fi
    echo "Using Python: $PYTHON3"

    # Install dependencies
    "$PYTHON3" -m pip install -r "$PROJ_DIR/requirements.txt" --quiet

    # Create log dir + generate plist from template
    mkdir -p "$LOG_DIR"
    sed -e "s|{{PYTHON3_PATH}}|$PYTHON3|g" \
        -e "s|{{PROJECT_DIR}}|$PROJ_DIR|g" \
        -e "s|{{HOME}}|$HOME|g" \
        "$TEMPLATE" > "$PLIST_DST"

    launchctl load "$PLIST_DST"
    echo "✓ Installed and loaded $LABEL"
    echo "  Logs: $LOG_DIR/"
    echo ""
    echo "Next steps:"
    echo "  1. System Settings → Privacy & Security → Accessibility → add python3"
    echo "  2. Chrome → View → Developer → Allow JavaScript from Apple Events"
}

cmd_uninstall() {
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl unload "$PLIST_DST" 2>/dev/null || true
    fi
    rm -f "$PLIST_DST"
    echo "Unloaded and removed $LABEL  (DB preserved in $LOG_DIR)"
}

cmd_restart() {
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    sleep 1
    launchctl load "$PLIST_DST"
    echo "Restarted $LABEL"
}

cmd_status() {
    echo "=== launchctl list ==="
    launchctl list "$LABEL" 2>/dev/null || echo "  (not loaded)"
    echo ""
    echo "=== last 20 log lines ==="
    tail -20 "$LOG_DIR/daemon.log" 2>/dev/null || echo "  (no log yet)"
    echo ""
    echo "=== last 10 error lines ==="
    tail -10 "$LOG_DIR/daemon-err.log" 2>/dev/null || echo "  (no errors)"
}

cmd_logs() {
    tail -f "$LOG_DIR/daemon.log"
}

case "${1:-}" in
    install)   cmd_install   ;;
    uninstall) cmd_uninstall ;;
    restart)   cmd_restart   ;;
    status)    cmd_status    ;;
    logs)      cmd_logs      ;;
    *)
        echo "Usage: $0 {install|uninstall|restart|status|logs}"
        exit 1
        ;;
esac
