#!/bin/bash
# Install (or re-install) Sleep Bandit's LaunchAgents on this Mac. Idempotent: copies
# the plists from deploy/mac/ into ~/Library/LaunchAgents and (re)loads each one by
# label with launchctl bootout + bootstrap -- never pkill, which can fail silently
# and leave the old code running.
#
#   ./install.sh            server + daily backup agents
#   ./install.sh --no-restart   copy the plists only; nothing is reloaded
#
# The pull agent (hosted-site mirror, deploy/README.md s6) is installed only when
# deploy/server.conf exists, because pull.sh does nothing without it.
#
# Restarting the server agent interrupts anyone using http://localhost:8777 for a
# second or two; the SQLite database is WAL-safe, nothing is lost.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$DIR/deploy/mac"
DEST="$HOME/Library/LaunchAgents"
UID_="$(id -u)"
RESTART=1
[ "${1:-}" = "--no-restart" ] && RESTART=0

mkdir -p "$DEST" "$HOME/Library/Logs" "$HOME/Library/Application Support/Sleep Bandit"
if [ ! -e "$HOME/Library/Application Support/Sleep Bandit/config.env" ]; then
    cp "$SRC/config.env.example" "$HOME/Library/Application Support/Sleep Bandit/config.env"
fi

AGENTS="com.elityre.sleep-bandit com.elityre.sleep-bandit-backup"
[ -r "$DIR/deploy/server.conf" ] && AGENTS="$AGENTS com.elityre.sleep-bandit-pull"

/usr/bin/python3 "$DIR/server.py" --test >/dev/null || { echo "self-test failed; not installing"; exit 1; }

for label in $AGENTS; do
    plist="$SRC/$label.plist"
    plutil -lint -s "$plist"
    cp "$plist" "$DEST/$label.plist"
    if [ "$RESTART" = 1 ]; then
        launchctl bootout "gui/$UID_/$label" 2>/dev/null || true
        launchctl bootstrap "gui/$UID_" "$DEST/$label.plist"
        echo "loaded $label"
    else
        echo "copied $label (not reloaded)"
    fi
done
[ "$RESTART" = 1 ] && launchctl print "gui/$UID_/com.elityre.sleep-bandit" | grep -E 'state|pid' | head -2
echo "logs: tail -f ~/Library/Logs/sleep-bandit.log"
