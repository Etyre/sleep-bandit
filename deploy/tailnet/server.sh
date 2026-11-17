#!/usr/bin/env bash
# Sleep Bandit on the private server that runs the executive-assistant agent. Run from the Mac.
#   deploy/tailnet/server.sh deploy        ship HEAD (code only) and restart it there
#   deploy/tailnet/server.sh move [--yes]  one time: stop it on this Mac, copy the database, start it on the server
#   deploy/tailnet/server.sh home [--yes]  the reverse: stop it there, bring the database back, start it here
#   deploy/tailnet/server.sh backup        take a backup on the server now (a daily one is scheduled by `move`)
#   deploy/tailnet/server.sh fetch         copy the server's newest backup into backups/ on this Mac
#   deploy/tailnet/server.sh status | logs
# Only one copy may run: both would refresh the same Oura token and invalidate each other's.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SERVER="${SB_SERVER:-atlas-server}"
ROOT=/opt/sleep-bandit
SHARED="${SB_SHARED:-$HOME/Documents/agent-coding/shared}"
LABEL=com.elityre.sleep-bandit
COMPOSE="docker compose -f $ROOT/app/deploy/tailnet/docker-compose.yml --env-file $ROOT/env"
r() { ssh "$SERVER" "$@"; }
say() { printf '\n== %s\n' "$*"; }
die() { echo "sleep-bandit server: $*" >&2; exit 1; }
mac_running() { launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; }
srv_running() { [ -n "$(r "docker ps -q -f name=^bandit$ -f status=running" 2>/dev/null)" ]; }
mac_tz() { readlink /etc/localtime | sed 's|.*/zoneinfo/||'; }

deploy() {
  [ -f "$SHARED/llm_contact.py" ] || die "agent-coding/shared not found at $SHARED"
  stage="$(mktemp -d)"
  git -C "$REPO" archive HEAD | tar -x -C "$stage"          # data/ and backups/ are not tracked, so never shipped
  mkdir -p "$stage/shared" "$stage/data" "$stage/backups"   # mount points inside the read-only code folder
  cp "$SHARED/llm_contact.py" "$SHARED/llm-system-prompt-preamble.md" "$SHARED/contact_eli_tool.json" "$stage/shared/"
  git -C "$REPO" log -1 --format='%h %s' > "$stage/VERSION"
  r "mkdir -p $ROOT/app $ROOT/data $ROOT/backups '/opt/atlas/data/home/Library/Application Support/LLMInbox' && chown 1000:1000 $ROOT/data $ROOT/backups '/opt/atlas/data/home/Library' '/opt/atlas/data/home/Library/Application Support' '/opt/atlas/data/home/Library/Application Support/LLMInbox' && { [ -f $ROOT/env ] || echo SB_TZ=$(mac_tz) > $ROOT/env; }"
  rsync -az --delete "$stage/" "$SERVER:$ROOT/app/"; rm -rf "$stage"
  # mktemp makes the staging folder private, and rsync copies that; the container's user 1000 must be able to read the code
  r "chmod -R a+rX $ROOT/app"
  r "$COMPOSE build -q"
  if srv_running; then r "$COMPOSE up -d --force-recreate" && echo "restarted on the server: $(r cat $ROOT/app/VERSION)"
  else echo "shipped $(r cat $ROOT/app/VERSION). Not started: it has not been moved to the server ($0 move)."; fi
}

case "${1:-}" in
deploy) deploy ;;
move)
  if [ "${2:-}" != "--yes" ]; then
    echo "This will: stop Sleep Bandit on this Mac (and its daily backup job), copy its database to $SERVER,"
    echo "start it there with Oura syncing by itself, and open it to your Tailscale devices on port 8777."
    echo "Run again with --yes. '$0 home --yes' undoes it."; exit 0
  fi
  srv_running && die "it is already running on the server"
  deploy
  say "stopping Sleep Bandit on this Mac"
  "$REPO/backup.sh" >/dev/null 2>&1 || true               # one more local backup first
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$LABEL-backup" 2>/dev/null || true
  launchctl disable "gui/$(id -u)/$LABEL"; launchctl disable "gui/$(id -u)/$LABEL-backup"
  say "copying the database"
  snap="$(mktemp -d)/sleep_bandit.db"
  /usr/bin/sqlite3 "$REPO/data/sleep_bandit.db" ".backup '$snap'"
  rsync -az "$snap" "$SERVER:$ROOT/data/sleep_bandit.db"; rm -f "$snap"
  r "chown -R 1000:1000 $ROOT/data && chmod 700 $ROOT/data"
  say "starting it on the server"
  r "$COMPOSE up -d"
  r "tailscale serve --bg --tcp=8777 tcp://localhost:8777 >/dev/null"
  # a daily backup at 05:30 server time, kept 30 deep in $ROOT/backups (deploy/backup.py)
  r "echo '30 5 * * * root docker exec bandit python deploy/backup.py >> /var/log/sleep-bandit-backup.log 2>&1' > /etc/cron.d/sleep-bandit-backup"
  for _ in $(seq 1 20); do r "curl -fsS -o /dev/null -m 4 http://127.0.0.1:8777/" 2>/dev/null && ok=1 && break; sleep 2; done
  if [ -z "${ok:-}" ]; then
    echo "!! it did not answer on the server; starting it on this Mac again"; r "$COMPOSE down" || true
    launchctl enable "gui/$(id -u)/$LABEL"; launchctl enable "gui/$(id -u)/$LABEL-backup"; "$REPO/install.sh"; exit 1
  fi
  echo "Sleep Bandit is running on the server: http://$(r "tailscale status --json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))'):8777"
  ;;
home)
  if [ "${2:-}" != "--yes" ]; then echo "This will stop Sleep Bandit on $SERVER, copy its database back here and start it on this Mac. Run again with --yes."; exit 0; fi
  srv_running || die "it is not running on the server"
  r "docker exec bandit python deploy/backup.py" >/dev/null
  r "$COMPOSE down"
  newest="$(r "ls -t $ROOT/backups/sleep_bandit-*.db | head -1")"
  cp "$REPO/data/sleep_bandit.db" "$REPO/backups/before-coming-home-$(date +%Y%m%d-%H%M%S).db"
  rm -f "$REPO/data/sleep_bandit.db-wal" "$REPO/data/sleep_bandit.db-shm"
  rsync -az "$SERVER:$newest" "$REPO/data/sleep_bandit.db"
  launchctl enable "gui/$(id -u)/$LABEL"; launchctl enable "gui/$(id -u)/$LABEL-backup"
  "$REPO/install.sh"
  echo "Sleep Bandit is back on this Mac: http://localhost:8777"
  ;;
backup) r "docker exec bandit python deploy/backup.py" ;;
fetch)
  newest="$(r "ls -t $ROOT/backups/sleep_bandit-*.db | head -1")"
  rsync -az "$SERVER:$newest" "$REPO/backups/server-$(basename "$newest")" && echo "fetched $(basename "$newest") into backups/"
  ;;
status)
  if srv_running; then echo "on the server ($(r cat $ROOT/app/VERSION)); time zone $(r cat $ROOT/env)"; elif mac_running; then echo "on this Mac"; else echo "not running anywhere"; fi ;;
logs) r "docker logs --tail 100 -f bandit" ;;
*) sed -n '2,10p' "$0"; exit 1 ;;
esac
