#!/bin/bash
# Brainloop — one-time setup for downloaded DMG.
#
# macOS stamps every downloaded file with `com.apple.quarantine`. For apps
# signed with Apple Developer ID this is fine, but Brainloop is signed ad-hoc
# (no $99/year cert), so Gatekeeper refuses it with a misleading "damaged"
# message. Stripping the attribute once lets the app run normally forever.
#
# The recipient double-clicks this file. Done.

set -u

APP="/Applications/Brainloop.app"

cat <<'BANNER'

  brainloop — first-run setup
  ───────────────────────────

BANNER

if [ ! -d "$APP" ]; then
    cat <<EOF
  Brainloop.app isn't in /Applications yet.

  1. Drag Brainloop.app into the Applications folder in the DMG window.
  2. Then double-click this Install.command again.

EOF
    read -n 1 -s -r -p "  Press any key to close this window…"
    echo
    exit 1
fi

echo "  Found Brainloop at: $APP"
echo "  Removing macOS quarantine flag…"
/usr/bin/xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

cat <<'EOF'

  All set. Open Brainloop from Applications or Launchpad.

  What happens next: on first launch, Brainloop asks for Accessibility
  permission and installs a tiny background agent that records your
  activity locally. Nothing is uploaded. Your AI key stays on this Mac.

EOF

read -n 1 -s -r -p "  Press any key to close this window…"
echo
exit 0
