#!/bin/bash
# Deploy / update the hosted Sleep Bandit from this Mac in one go.
#
#   deploy/push.sh SERVER_IP            first deploy, or push a code update (idempotent)
#
# First run: copies the code, runs deploy/setup.sh on the server, installs the prepared
# /etc/sleep-bandit.env from ~/.config/sleep-bandit/, uploads a fresh WAL-safe snapshot
# of the Mac DB (only if the server has no DB yet -- the server's DB is never
# overwritten), starts the service, records the server in deploy/server.conf and runs
# pull.sh once. Later runs: re-copy code, re-run setup (safe), restart the service.
# Never touches an existing /etc/sleep-bandit.env that already has a password hash.
set -euo pipefail

IP="${1:?usage: deploy/push.sh SERVER_IP}"
USER_="${SB_SSH_USER:-root}"
SERVER="$USER_@$IP"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/sleep-bandit
DOMAIN=elissleepbandit.com
ENV_SRC="$HOME/.config/sleep-bandit/sleep-bandit.env"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=25 -o StrictHostKeyChecking=accept-new"

step() { echo; echo "== $*"; }

step "ssh reachability ($SERVER)"
$SSH "$SERVER" 'echo "  ok: $(hostname) $(. /etc/os-release; echo $PRETTY_NAME)"'

step "copy code"
rsync -az --delete -e "$SSH" \
    --exclude data --exclude backups --exclude .git --exclude __pycache__ \
    --exclude deploy/server.conf --exclude '.claude' \
    "$DIR/" "$SERVER:/root/sleep-bandit/"

step "server setup (packages, caddy, user, units)"
$SSH "$SERVER" 'cd /root/sleep-bandit && bash deploy/setup.sh'

step "config"
if $SSH "$SERVER" 'grep -q "^SB_PASSWORD_HASH=.\+" /etc/sleep-bandit.env 2>/dev/null'; then
    echo "  /etc/sleep-bandit.env already configured on server -- left alone"
else
    [ -r "$ENV_SRC" ] || { echo "  missing $ENV_SRC (prepared env file) -- aborting"; exit 1; }
    $SSH "$SERVER" 'cat > /etc/sleep-bandit.env && chmod 600 /etc/sleep-bandit.env' < "$ENV_SRC"
    echo "  installed /etc/sleep-bandit.env"
fi

step "database"
if $SSH "$SERVER" "test -s $APP/data/sleep_bandit.db"; then
    echo "  server already has a DB -- not overwritten"
else
    "$DIR/backup.sh"
    SNAP="$(ls -1t "$DIR"/backups/sleep_bandit-*.db | head -1)"
    echo "  uploading $(basename "$SNAP") ($(du -h "$SNAP" | cut -f1))"
    $SSH "$SERVER" "cat > $APP/data/sleep_bandit.db && chown sleepbandit:sleepbandit $APP/data/sleep_bandit.db && chmod 600 $APP/data/sleep_bandit.db" < "$SNAP"
fi

step "start"
$SSH "$SERVER" 'systemctl restart sleep-bandit && systemctl start sleep-bandit-backup.timer && sleep 2 && systemctl is-active sleep-bandit && curl -s -o /dev/null -w "  app answers locally: HTTP %{http_code}\n" http://127.0.0.1:8777/'

step "record server for pull.sh"
echo "$SERVER" > "$DIR/deploy/server.conf"
echo "  deploy/server.conf = $SERVER"

step "DNS / HTTPS"
GOT="$(dig +short "$DOMAIN" | tr '\n' ' ')"
if [[ " $GOT" == *" $IP "* ]]; then
    echo "  $DOMAIN -> $IP (DNS ok)"
    for i in 1 2 3 4 5 6; do
        CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "https://$DOMAIN/" || true)"
        if [ "$CODE" = "200" ]; then echo "  https://$DOMAIN/ -> 200"; break; fi
        echo "  https not ready yet (got '$CODE'), waiting for the certificate... ($i/6)"; sleep 20
    done
else
    echo "  $DOMAIN currently resolves to: ${GOT:-nothing}"
    echo "  -> add A records @ and www -> $IP at the registrar; Caddy fetches the certificate itself once that propagates"
fi

step "first pull to the Mac"
"$DIR/pull.sh"

echo
echo "Done. Admin page: https://$DOMAIN/$(grep '^SB_ADMIN_PATH=' "$ENV_SRC" 2>/dev/null | cut -d= -f2)"
