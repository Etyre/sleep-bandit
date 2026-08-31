"""
auth.py -- password + cookie sessions for the hosted (public) mode. Standard library only.

The hosted site has two faces: the public root, which is read-only and never needs a
login, and one unlinked admin path that asks for a single password and then sets a
signed session cookie good for SESSION_DAYS. Nothing here is used in local mode.

  * Password: PBKDF2-HMAC-SHA256, 200k iterations, random salt. The HASH (not the
    password) lives in the server's environment as SB_PASSWORD_HASH; make one with
    `python3 auth.py` (it prompts, prints the hash).
  * Session cookie: "<expiry>.<hmac>" signed with a per-install secret kept in the
    settings table (minted on first use). No server-side session store is needed;
    rotating the secret logs every browser out.
  * Brute force: per-IP failure counter -- after MAX_FAILS wrong passwords the IP
    waits LOCK_SECONDS before it may try again.
"""

import base64
import hashlib
import hmac
import os
import secrets
import time

PBKDF2_ITERS = 200_000
SESSION_DAYS = 30
COOKIE_NAME = "sb_session"
MAX_FAILS = 5
LOCK_SECONDS = 300


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return "pbkdf2$%d$%s$%s" % (PBKDF2_ITERS, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password, stored):
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2":
            return False
        salt, want = base64.b64decode(salt_b64), base64.b64decode(dk_b64)
        got = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(got, want)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Sessions (signed, stateless)
# ---------------------------------------------------------------------------
def session_secret(conn):
    """The per-install signing secret, minted on first use and kept in settings."""
    import store
    s = store.get_setting(conn, "session_secret")
    if not s:
        s = secrets.token_urlsafe(32)
        store.set_setting(conn, "session_secret", s)
    return s.encode("utf-8")


def make_token(secret, days=SESSION_DAYS):
    exp = str(int(time.time()) + days * 86400)
    sig = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
    return exp + "." + sig


def check_token(secret, token):
    try:
        exp, sig = (token or "").split(".", 1)
        if int(exp) < time.time():
            return False
        want = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, sig)
    except (ValueError, AttributeError):
        return False


def cookie_from_header(header, name=COOKIE_NAME):
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


# ---------------------------------------------------------------------------
# Login rate limit (in-process; the site is one process)
# ---------------------------------------------------------------------------
_fails = {}   # ip -> [count, locked_until]


def login_allowed(ip):
    rec = _fails.get(ip)
    return not rec or rec[1] <= time.time()


def note_login(ip, ok):
    if ok:
        _fails.pop(ip, None)
        return
    rec = _fails.setdefault(ip, [0, 0.0])
    rec[0] += 1
    if rec[0] >= MAX_FAILS:
        rec[0] = 0
        rec[1] = time.time() + LOCK_SECONDS


if __name__ == "__main__":
    import getpass
    pw = getpass.getpass("Admin password: ")
    if pw != getpass.getpass("Again: "):
        raise SystemExit("Passwords differ.")
    print("SB_PASSWORD_HASH=" + hash_password(pw))
