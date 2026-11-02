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

One command from this repo on the Mac (the DB is copied separately — it is never in git):

```bash
deploy/push.sh SERVER_IP
```

It copies the code, runs `deploy/setup.sh` on the server (packages, Caddy, the
`sleepbandit` user, systemd units), installs the prepared `/etc/sleep-bandit.env`
(admin path + password hash, kept at `~/.config/sleep-bandit/` on the Mac, with the
password in `admin-password.txt` there), uploads a fresh WAL-safe snapshot of the Mac's
DB if the server has none yet, starts the service, writes the server address into
`deploy/server.conf` for `pull.sh`, checks DNS/HTTPS, and pulls the first mirror copy
back down. Re-running it later pushes a code update and restarts the service; it never
overwrites the server's DB or an env file that already has a password hash.

By hand instead, the steps it automates:

```bash
rsync -a --exclude data --exclude backups --exclude .git ./ root@SERVER_IP:/root/sleep-bandit/
ssh root@SERVER_IP 'cd /root/sleep-bandit && bash deploy/setup.sh'
python3 /opt/sleep-bandit/auth.py           # on the server: prompts for the admin password, prints SB_PASSWORD_HASH=...
nano /etc/sleep-bandit.env                  # paste that line; set SB_ADMIN_PATH to something unguessable
./backup.sh                                                  # on the Mac: writes backups/sleep_bandit-<stamp>.db
scp backups/sleep_bandit-<stamp>.db root@SERVER_IP:/opt/sleep-bandit/data/sleep_bandit.db
ssh root@SERVER_IP 'chown sleepbandit:sleepbandit /opt/sleep-bandit/data/sleep_bandit.db && systemctl start sleep-bandit sleep-bandit-backup.timer && systemctl status sleep-bandit --no-pager'
```

Open `https://elissleepbandit.com/` (read-only) and `https://elissleepbandit.com/<your admin path>`.

## 4. Oura

Oura's OAuth redirect must point at the site now. In the Oura developer portal edit the
app's redirect URI to `https://elissleepbandit.com/oura/callback`, then on the admin
page open **Oura → Save & reconnect**. (The setup text in the panel shows the right URI.)

## 5. On your phone

The site is also an installable app (a PWA): same code, no app store. Once the site is
up, open it on the phone and add it to the home screen:

* **iPhone** — in Safari, open the page, tap **Share → Add to Home Screen**.
* **Android** — in Chrome, open the page, tap **⋮ → Install app** (or *Add to Home screen*).

The icon opens the **Tonight** view: tonight's plan as a checklist, one card per
intervention. Install the **admin page** (`https://elissleepbandit.com/<SB_ADMIN_PATH>`)
to be able to tick things off from the phone — you sign in once inside the installed
app (it keeps its own 30-day cookie, separate from Safari's). Installing the public root
instead gives a read-only copy of the plan.

With no signal the app still shows the last plan it loaded (marked "Offline"); ticks
need a connection. The phone needs the real site: `localhost` on the Mac isn't
reachable from it, so this only works once step 3 is done.

## 6. After that

* **A whole copy of the data stays on the Mac.** `pull.sh` (run every 6 hours while the
  Mac is awake by the `com.elityre.sleep-bandit-pull` LaunchAgent, plist in `deploy/`)
  takes a WAL-safe snapshot *on the server*, streams it down, checks its integrity,
  keeps the newest 60 in `backups/server-<stamp>.db`, and refreshes `data/sleep_bandit.db`
  so the local app is always ready to take over. Install the agent once with
  `cp deploy/com.elityre.sleep-bandit-pull.plist ~/Library/LaunchAgents/ && launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.elityre.sleep-bandit-pull.plist`;
  log in `backups/pull.log`. **If the server is ever lost**: `launchctl bootstrap gui/$UID
  ~/Library/LaunchAgents/com.elityre.sleep-bandit.plist` and the Mac copy is live at
  `http://localhost:8777` with data at most 6 hours old.
* **Your Mac copy** is now a standby. Stop its LaunchAgent so two planners don't
  diverge (and so `pull.sh` can refresh the standby DB, which it skips while a local
  server holds port 8777): `launchctl bootout gui/$UID/com.elityre.sleep-bandit`. For
  development, run `server.py` on another port with `SB_DB_PATH` pointing at a copy.
* **Updating the code**: `deploy/push.sh SERVER_IP` again (setup is idempotent; it
  never touches `/etc/sleep-bandit.env` or the DB).
* **Backups on the server**: daily at 05:30 server time into `/opt/sleep-bandit/backups/`
  (newest 30 kept), in addition to the Mac mirror above.
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
