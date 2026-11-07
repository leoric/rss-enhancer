#!/usr/bin/env bash
set -euo pipefail
LABEL="com.user.rssenhancer"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed the launch agent (feeds and state in ~/Library/Application Support/RssEnhancer are kept)."
