#!/usr/bin/env bash
# Install RssEnhancer as a per-user launchd agent (starts at login, restarts on crash).
# Secrets stay in config.toml; nothing sensitive is written to the plist.
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.user.rssenhancer"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="${PYTHON:-$(command -v python3)}"
# Same folder the program writes stats.log and rapid-mlx.log to (app.log_dir in config.toml)
LOGDIR="$(cd "$DIR" && "$PYTHON" -c 'from rssenhancer import common; print(common.LOG_DIR)' 2>/dev/null || echo "$HOME/Library/Logs/RssEnhancer")"
LOG="$LOGDIR/RssEnhancer.log"
OLD_LOG="$HOME/Library/Logs/RssEnhancer.log"  # where earlier versions logged

[ -f "$DIR/config.toml" ] || {
  echo "Nothing to run: create $DIR/config.toml (copy config.example.toml)"; exit 1; }
chmod 600 "$DIR/config.toml"
mkdir -p "$HOME/Library/LaunchAgents" "$LOGDIR"

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
[ -f "$OLD_LOG" ] && [ ! -e "$LOG" ] && mv "$OLD_LOG" "$LOG"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Logs: $LOGDIR (main log: $LOG)"
echo "Feeds:  http://127.0.0.1:${RSSENHANCER_PORT:-8787}/"
