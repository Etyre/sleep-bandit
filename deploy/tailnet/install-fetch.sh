#!/usr/bin/env bash
# Installs (or with --uninstall removes) the LaunchAgent that copies a fresh backup from the server to this Mac
# every day at 15:00 and at login. Idempotent. Log: ~/Library/Logs/sleep-bandit-fetch.log
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd -P)"
LABEL=com.elityre.sleep-bandit-fetch
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break; sleep 1; done
if [ "${1:-}" = "--uninstall" ]; then rm -f "$PLIST"; echo "removed $LABEL"; exit 0; fi
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" "$REPO/deploy/tailnet/$LABEL.plist" > "$PLIST"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL; log: $HOME/Library/Logs/sleep-bandit-fetch.log"
