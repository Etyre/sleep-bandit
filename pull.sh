#!/bin/bash
# Pull a fresh, WAL-safe snapshot of the HOSTED database down to this Mac, so a whole
# copy of the data always lives here in case the server is ever lost.
#
#   * takes `sqlite3 .backup` ON THE SERVER (consistent even mid-write), streams it
#     over ssh, checks its integrity, and keeps it as backups/server-<stamp>.db
#     (newest $KEEP kept);
#   * refreshes the standby copy data/sleep_bandit.db -- the file the local app opens
#     -- unless a local server is currently using it (port 8777 busy), so the Mac copy
#     can take over at once with `launchctl bootstrap` of the server LaunchAgent.
#
# Server address (user@host) comes from deploy/server.conf (gitignored). Without that
# file the script exits 0 doing nothing, so the LaunchAgent can be installed before the
# server exists. Run every 6 h by com.elityre.sleep-bandit-pull; by hand:  ./pull.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
CONF="$DIR/deploy/server.conf"
OUT="$DIR/backups"
LOCAL_DB="$DIR/data/sleep_bandit.db"
REMOTE_DB="/opt/sleep-bandit/data/sleep_bandit.db"
KEEP=60
PORT="${SB_PORT:-8777}"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*"; }

[ -r "$CONF" ] || { log "no $CONF -- server not set up yet, nothing to pull"; exit 0; }
SERVER="$(grep -v '^\s*#' "$CONF" | grep -m1 .)"
[ -n "$SERVER" ] || { log "deploy/server.conf is empty"; exit 1; }

mkdir -p "$OUT"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$OUT/server-$STAMP.db"
TMP="$DEST.part"
trap 'rm -f "$TMP"' EXIT

# Snapshot on the server into a temp file, stream it back, clean up there.
ssh -o BatchMode=yes -o ConnectTimeout=25 "$SERVER" \
    'set -e; T=$(mktemp); trap "rm -f \$T" EXIT; sqlite3 '"$REMOTE_DB"' ".backup $T"; cat "$T"' \
    > "$TMP"

[ -s "$TMP" ] || { log "pull from $SERVER produced an empty file"; exit 1; }
CHECK="$(/usr/bin/sqlite3 "$TMP" 'pragma integrity_check;')"
[ "$CHECK" = "ok" ] || { log "integrity_check failed: $CHECK"; exit 1; }
NIGHTS="$(/usr/bin/sqlite3 "$TMP" 'select count(*) from nights;')"
mv "$TMP" "$DEST"
trap - EXIT

# Prune: keep the $KEEP newest server snapshots.
ls -1t "$OUT"/server-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f "$old"
done

# Refresh the standby copy unless a local server holds the port (then it owns the DB).
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    log "pulled $DEST ($NIGHTS nights); local server on :$PORT is running, standby copy left alone"
else
    cp "$DEST" "$LOCAL_DB.new"
    rm -f "$LOCAL_DB-wal" "$LOCAL_DB-shm"
    mv -f "$LOCAL_DB.new" "$LOCAL_DB"
    log "pulled $DEST ($NIGHTS nights) and refreshed standby copy $LOCAL_DB"
fi
