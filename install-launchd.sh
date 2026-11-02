#!/usr/bin/env bash
# Install RssEnhancer as a per-user launchd agent (starts at login, restarts on crash).
# Secrets stay in .env; nothing sensitive is written to the plist.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.user.rssenhancer"
OLD_LABEL="com.user.mailrss"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/RssEnhancer.log"
PYTHON="${PYTHON:-$(command -v python3)}"

[ -f "$DIR/.env" ] || [ -f "$DIR/config.json" ] || {
  echo "Nothing to run: create $DIR/.env (copy env.example) and/or $DIR/config.json (copy config.example.json)"; exit 1; }
[ ! -f "$DIR/.env" ] || chmod 600 "$DIR/.env"
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

# the pre-rename agent would fight over the same mailbox and port
if [ -f "$HOME/Library/LaunchAgents/$OLD_LABEL.plist" ]; then
  launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
  echo "Removed the old $OLD_LABEL agent."
fi

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PYTHON</string><string>-m</string><string>rssenhancer</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST_EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Log: $LOG"
echo "Feeds:  http://127.0.0.1:${RSSENHANCER_PORT:-8787}/"
