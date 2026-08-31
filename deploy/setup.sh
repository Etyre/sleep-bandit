#!/usr/bin/env bash
# One-shot server setup for Ubuntu 22.04/24.04. Run as root ON THE SERVER from the
# unpacked repo directory (see deploy/README.md):   sudo bash deploy/setup.sh
set -euo pipefail
APP=/opt/sleep-bandit
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "== packages"
apt-get update -qq
apt-get install -y -qq python3 sqlite3 debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null
if ! command -v caddy >/dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq && apt-get install -y -qq caddy >/dev/null
fi

echo "== user + files"
id -u sleepbandit >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin sleepbandit
mkdir -p "$APP/data" "$APP/backups"
for f in server.py store.py bandit.py oura.py auth.py public deploy; do
  rm -rf "$APP/$f"; cp -r "$SRC/$f" "$APP/$f"
done
chown -R sleepbandit:sleepbandit "$APP"
chmod 700 "$APP/data" "$APP/backups"

echo "== config"
if [ ! -f /etc/sleep-bandit.env ]; then
  cp "$SRC/deploy/sleep-bandit.env.example" /etc/sleep-bandit.env
  chmod 600 /etc/sleep-bandit.env
  echo "   wrote /etc/sleep-bandit.env -- EDIT IT (admin path + password hash) before starting"
fi
cp "$SRC/deploy/sleep-bandit.service" /etc/systemd/system/
cp "$SRC/deploy/sleep-bandit-backup.service" "$SRC/deploy/sleep-bandit-backup.timer" /etc/systemd/system/
cp "$SRC/deploy/Caddyfile" /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable sleep-bandit sleep-bandit-backup.timer caddy >/dev/null
systemctl restart caddy
echo
echo "Next:"
echo "  1. edit /etc/sleep-bandit.env  (SB_ADMIN_PATH, SB_PASSWORD_HASH from: python3 $APP/auth.py)"
echo "  2. copy your DB to $APP/data/sleep_bandit.db  (chown sleepbandit:sleepbandit)"
echo "  3. systemctl start sleep-bandit sleep-bandit-backup.timer && systemctl status sleep-bandit"
