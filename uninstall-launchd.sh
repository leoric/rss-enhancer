#!/usr/bin/env bash
set -euo pipefail
for LABEL in com.user.rssenhancer com.user.mailrss; do
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
done
echo "Removed the launch agent (feeds and state in ~/Library/Application Support/RssEnhancer are kept)."
