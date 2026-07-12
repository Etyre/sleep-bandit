#!/bin/bash
# Lightweight, WAL-safe backup of the Sleep Bandit database.
#
# Uses `sqlite3 .backup` (NOT cp) so a consistent snapshot is taken even while the
# server is mid-write. Writes a timestamped copy into backups/ and keeps only the
# most recent $KEEP copies. Run daily by the com.elityre.sleep-bandit-backup
# LaunchAgent; safe to run by hand any time:  ./backup.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
DB="$DIR/data/sleep_bandit.db"
OUT="$DIR/backups"
KEEP=30

mkdir -p "$OUT"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$OUT/sleep_bandit-$STAMP.db"

/usr/bin/sqlite3 "$DB" ".backup '$DEST'"

# Prune: keep the $KEEP newest snapshots, delete the rest.
ls -1t "$OUT"/sleep_bandit-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
    rm -f "$old"
done

echo "$(date '+%Y-%m-%d %H:%M:%S')  backed up -> $DEST  (keeping newest $KEEP)"
