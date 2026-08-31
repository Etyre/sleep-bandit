# Hosting Sleep Bandit at elissleepbandit.com

Two faces of one server process:

* **`https://elissleepbandit.com/`** — public, **read-only**. Everything the local app
  shows except the written notes (per-night notes and intervention notes). No editing
  controls, no export, no Oura panel, and **no link to the admin page**.
* **`https://elissleepbandit.com/<SB_ADMIN_PATH>`** — unlinked. Asks for the password,
  then it is the full app: log nights, tick cells, edit interventions, Oura, export.
  The session cookie lasts 30 days; "Log out" is in the header.

The server becomes the **source of truth**: it plans each night and syncs Oura on a
20-minute timer, so nothing depends on a browser being open.

## 1. Server

Any small Linux VPS (Ubuntu 24.04, 1 vCPU / 1 GB is plenty — the app is pure Python
stdlib + SQLite). DigitalOcean, Hetzner, Linode, etc. Note its public IP.

## 2. DNS

At your registrar, add **A** records for `elissleepbandit.com` and `www` pointing to the
server IP. Wait until `dig +short elissleepbandit.com` returns it (Caddy needs this to
get the HTTPS certificate).

## 3. Install

From this repo on your Mac (the DB is copied separately — it is never in git):

```bash
rsync -a --exclude data --exclude backups --exclude .git ./ root@SERVER_IP:/root/sleep-bandit/
ssh root@SERVER_IP 'cd /root/sleep-bandit && bash deploy/setup.sh'
```

Then on the server:

```bash
python3 /opt/sleep-bandit/auth.py           # prompts for the admin password, prints SB_PASSWORD_HASH=...
nano /etc/sleep-bandit.env                  # paste that line; set SB_ADMIN_PATH to something unguessable
```

Copy the database (take a fresh WAL-safe snapshot first so nothing mid-write is lost):

```bash
./backup.sh                                                  # on the Mac: writes backups/sleep_bandit-<stamp>.db
scp backups/sleep_bandit-<stamp>.db root@SERVER_IP:/opt/sleep-bandit/data/sleep_bandit.db
ssh root@SERVER_IP 'chown sleepbandit:sleepbandit /opt/sleep-bandit/data/sleep_bandit.db && systemctl start sleep-bandit sleep-bandit-backup.timer && systemctl status sleep-bandit --no-pager'
```

Open `https://elissleepbandit.com/` (read-only) and `https://elissleepbandit.com/<your admin path>`.

## 4. Oura

Oura's OAuth redirect must point at the site now. In the Oura developer portal edit the
app's redirect URI to `https://elissleepbandit.com/oura/callback`, then on the admin
page open **Oura → Save & reconnect**. (The setup text in the panel shows the right URI.)

## 5. After that

* **Your Mac copy** is now a backup / dev copy. Stop its LaunchAgent so two planners don't
  diverge: `launchctl bootout gui/$UID/com.elityre.sleep-bandit`. Or keep it for
  development only, on its own DB.
* **Updating the code**: rerun the `rsync` + `bash deploy/setup.sh` lines (setup is
  idempotent; it never touches `/etc/sleep-bandit.env` or the DB), then
  `systemctl restart sleep-bandit`.
* **Backups**: daily at 05:30 server time into `/opt/sleep-bandit/backups/` (newest 30
  kept). Pull one down with `scp` whenever you like.
* **Logs**: `journalctl -u sleep-bandit -f`.
* **Changing the password**: rerun `python3 auth.py`, update the env file, restart.
  To log every browser out, also delete the `session_secret` row from the `settings`
  table (`sqlite3 data/sleep_bandit.db "delete from settings where key='session_secret'"`).

## Security notes

* Only the admin can write; every write route returns 403 without a valid session.
  The public API responses have notes blanked server-side (not just hidden in the UI).
* Login is rate-limited (5 wrong tries → 5-minute lockout per IP) and the password is
  stored only as a PBKDF2 hash. The cookie is HttpOnly, Secure, SameSite=Strict.
* The DB holds your Oura OAuth tokens; it is readable only by the `sleepbandit` user.
