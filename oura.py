"""
oura.py -- pull each morning's HRV, lowest resting heart rate, and sleep efficiency
from the Oura API.

Oura's v2 API is OAuth2-only (personal access tokens were retired in Dec 2025), so
one-time setup is: register a private OAuth app at https://cloud.ouraring.com
(Developer -> OAuth applications) with redirect URI

    http://localhost:8777/oura/callback

then paste its Client ID / Client Secret into Sleep Bandit's Oura panel and click
Connect. Tokens live in the settings table and refresh themselves; after that the
app fills in blank HRV / resting-HR cells automatically each time you open it.

Date mapping: a Sleep Bandit night is keyed by the EVENING's date, while an Oura
sleep session's `day` is the morning it ends. Night d therefore matches Oura day
d+1. We only use the session Oura itself marks as the night's main sleep
(type == "long_sleep"), so naps never fill a night.

Pure standard library, like the rest of the app.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import store

AUTH_URL = "https://cloud.ouraring.com/oauth/authorize"
TOKEN_URL = "https://api.ouraring.com/oauth/token"
SLEEP_URL = "https://api.ouraring.com/v2/usercollection/sleep"
SCOPES = "daily heartrate"
HTTP_TIMEOUT = 15          # seconds per request
SYNC_LOOKBACK_DAYS = 45    # how far back a sync looks for blank nights
SYNC_THROTTLE_MIN = 15     # auto-syncs closer together than this are skipped


class OuraError(Exception):
    """Anything that should surface to the UI as a readable message."""


def redirect_uri(port, base=None):
    """The OAuth redirect: localhost when running on your own machine, the site's
    public URL (SB_BASE_URL) when hosted. Must match the URI registered with Oura."""
    return f"{base.rstrip('/')}/oura/callback" if base else f"http://localhost:{port}/oura/callback"


def authorize_url(client_id, state, port, base=None):
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri(port, base),
        "scope": SCOPES,
        "state": state,
    })


def _http_json(url, data=None, headers=None):
    """GET (data=None) or form-POST a URL, return parsed JSON. Raises OuraError with
    the HTTP status and any error body on failure."""
    req = urllib.request.Request(
        url,
        data=(urllib.parse.urlencode(data).encode() if data is not None else None),
        headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:300]
        except Exception:
            pass
        raise OuraError(f"Oura API returned {e.code}: {body or e.reason}") from e
    except urllib.error.URLError as e:
        raise OuraError(f"Could not reach Oura: {e.reason}") from e


def _save_tokens(conn, tok):
    """Store an OAuth token response. Oura rotates refresh tokens, so always keep the
    newest one."""
    expires_at = datetime.now() + timedelta(seconds=int(tok.get("expires_in", 3600)))
    store.set_setting(conn, "oura_access_token", tok["access_token"])
    if tok.get("refresh_token"):
        store.set_setting(conn, "oura_refresh_token", tok["refresh_token"])
    store.set_setting(conn, "oura_expires_at", expires_at.isoformat(timespec="seconds"))


def exchange_code(conn, code, port, base=None):
    """Trade the callback's authorization code for tokens and store them."""
    tok = _http_json(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(port, base),
        "client_id": store.get_setting(conn, "oura_client_id", ""),
        "client_secret": store.get_setting(conn, "oura_client_secret", ""),
    })
    _save_tokens(conn, tok)


def _refresh(conn):
    refresh_token = store.get_setting(conn, "oura_refresh_token")
    if not refresh_token:
        raise OuraError("Not connected to Oura — click Connect to authorize.")
    try:
        tok = _http_json(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": store.get_setting(conn, "oura_client_id", ""),
            "client_secret": store.get_setting(conn, "oura_client_secret", ""),
        })
    except OuraError as e:
        # a dead refresh token means the authorization was revoked -> reconnect
        store.set_setting(conn, "oura_access_token", None)
        store.set_setting(conn, "oura_refresh_token", None)
        raise OuraError("Oura authorization expired — click Connect to re-authorize.") from e
    _save_tokens(conn, tok)
    return tok["access_token"]


def _access_token(conn):
    token = store.get_setting(conn, "oura_access_token")
    if not token:
        if store.get_setting(conn, "oura_refresh_token"):
            return _refresh(conn)
        raise OuraError("Not connected to Oura — click Connect to authorize.")
    expires_at = store.get_setting(conn, "oura_expires_at")
    if expires_at and datetime.fromisoformat(expires_at) < datetime.now() + timedelta(seconds=60):
        return _refresh(conn)
    return token


def fetch_sleep(conn, start_date, end_date):
    """All sleep sessions with start_date <= day <= end_date, following pagination.
    Retries once through a token refresh on a 401."""
    token = _access_token(conn)
    docs, next_token, refreshed = [], None, False
    while True:
        params = {"start_date": start_date, "end_date": end_date}
        if next_token:
            params["next_token"] = next_token
        url = SLEEP_URL + "?" + urllib.parse.urlencode(params)
        try:
            page = _http_json(url, headers={"Authorization": "Bearer " + token})
        except OuraError as e:
            if "401" in str(e) and not refreshed:
                token, refreshed = _refresh(conn), True
                continue
            raise
        docs.extend(page.get("data", []))
        next_token = page.get("next_token")
        if not next_token:
            return docs


def _main_sleep_by_day(docs):
    """Oura day -> the night's main sleep session (type 'long_sleep'; if the night was
    split into several, the longest one)."""
    best = {}
    for doc in docs:
        if doc.get("type") != "long_sleep":
            continue
        day = doc.get("day")
        cur = best.get(day)
        if cur is None or (doc.get("total_sleep_duration") or 0) > (cur.get("total_sleep_duration") or 0):
            best[day] = doc
    return best


def _hrv_value(doc):
    """Night's HRV from a sleep doc. The doc's average_hrv is rounded to a whole number
    (it's what the Oura app displays); the raw 5-minute sample series is floats, so
    averaging it ourselves keeps a decimal of real precision. Falls back to average_hrv
    for docs without samples."""
    samples = [x for x in ((doc.get("hrv") or {}).get("items") or []) if x is not None]
    if samples:
        return round(sum(samples) / len(samples), 1)
    return doc.get("average_hrv")


def _lrhr_5min(doc):
    """Lowest resting HR on the 5-MINUTE basis: the min of the doc's 5-minute heart
    rate samples -- the same calculation the Oura app displays, and the basis of every
    hand-typed value, so it's what goes in the analyzed/displayed lrhr column. The
    API's 30-second-based lowest_heart_rate is stored separately (lrhr_30s); it's only
    used here as a fallback for docs without a sample series."""
    samples = [x for x in ((doc.get("heart_rate") or {}).get("items") or []) if x is not None]
    if samples:
        return round(min(samples), 1)
    return doc.get("lowest_heart_rate")


# Within-night series kept per night (store.night_series) for the night-shape
# tagger (tags.py): the 5-minute heart-rate and HRV samples, the 5-minute
# hypnogram, 30-second movement, and the summary numbers that frame them.
SERIES_FIELDS = ("bedtime_start", "bedtime_end", "latency", "awake_time", "time_in_bed",
                 "total_sleep_duration", "efficiency", "restless_periods",
                 "deep_sleep_duration", "light_sleep_duration", "rem_sleep_duration",
                 "average_heart_rate", "lowest_heart_rate", "average_hrv", "average_breath",
                 "sleep_phase_5_min", "movement_30_sec")


def _series_doc(doc):
    """The slice of an Oura sleep document the tagger needs."""
    out = {k: doc.get(k) for k in SERIES_FIELDS}
    for k in ("heart_rate", "hrv"):
        v = doc.get(k) or {}
        out[k] = {"interval": v.get("interval"), "items": v.get("items") or [],
                  "timestamp": v.get("timestamp")}
    return out


def _write_night(conn, night, doc, result):
    """Write one matched night's metrics (and its raw series) from its sleep doc,
    tallying filled/updated into result."""
    for metric, value in (("hrv", _hrv_value(doc)),
                          ("lrhr", _lrhr_5min(doc)),
                          ("lrhr_30s", doc.get("lowest_heart_rate")),
                          ("tst", _tst_hours(doc)),
                          ("lat", _latency_min(doc)),
                          ("eff", doc.get("efficiency"))):
        wrote = store.set_metric_from_oura(conn, night, metric, value)
        if wrote:
            result[wrote] += 1
    if store.set_night_series(conn, night, _series_doc(doc)):
        result["series"] = result.get("series", 0) + 1


def sync(conn, current_night, force=False):
    """Bring recent past nights' HRV / resting-HR in line with Oura: blank cells are
    filled, and hand-typed values are replaced when Oura's (more precise) number
    differs -- the audit log keeps what was overwritten. Returns a summary dict;
    errors come back as {"error": msg} rather than raising."""
    last = store.get_setting(conn, "oura_last_sync")
    if last and not force:
        age = datetime.now() - datetime.fromisoformat(last)
        if age < timedelta(minutes=SYNC_THROTTLE_MIN):
            prev = store.get_setting(conn, "oura_last_result")
            out = json.loads(prev) if prev else {"checked": 0, "filled": 0, "updated": 0}
            out["skipped"] = True
            return out

    since = (date.fromisoformat(current_night) - timedelta(days=SYNC_LOOKBACK_DAYS)).isoformat()
    nights = store.nights_for_oura_sync(conn, since, current_night)
    result = {"checked": len(nights), "filled": 0, "updated": 0}
    if nights:
        try:
            # night d's sleep is filed under Oura day d+1. NOTE: the sleep endpoint's
            # end_date is EXCLUSIVE (verified empirically -- a start_date==end_date
            # query returns nothing), so reach one day past the newest night's day.
            oura_start = (date.fromisoformat(nights[0]["date"]) + timedelta(days=1)).isoformat()
            oura_end = (date.fromisoformat(nights[-1]["date"]) + timedelta(days=2)).isoformat()
            by_day = _main_sleep_by_day(fetch_sleep(conn, oura_start, oura_end))
        except OuraError as e:
            result["error"] = str(e)
            store.set_setting(conn, "oura_last_result", json.dumps(result))
            return result
        for night in nights:
            doc = by_day.get((date.fromisoformat(night["date"]) + timedelta(days=1)).isoformat())
            if not doc:
                continue
            _write_night(conn, night["date"], doc, result)
    store.set_setting(conn, "oura_last_sync", datetime.now().isoformat(timespec="seconds"))
    store.set_setting(conn, "oura_last_result", json.dumps(result))
    return result


def _tst_hours(doc):
    """Total sleep time in hours (two decimals) from the sleep doc's
    total_sleep_duration (seconds); None if absent."""
    s = doc.get("total_sleep_duration")
    return round(float(s) / 3600.0, 2) if s else None


def _latency_min(doc):
    """Sleep-onset latency in minutes (one decimal) from the sleep doc's latency
    (seconds); None if absent."""
    s = doc.get("latency")
    return round(float(s) / 60.0, 1) if s is not None else None


def backfill(conn, first_night, last_night):
    """One-shot historical import: pull Oura's main-sleep HRV / resting HR for every
    night in [first_night, last_night] (YYYY-MM-DD, evening-keyed) and write it in,
    creating night rows as needed. Unlike sync() there's no throttle and no lookback
    window -- this exists to seed history (e.g. so the HRV-readiness baseline band is
    defined from the start of the chart). Idempotent: already-matching nights no-op."""
    oura_start = (date.fromisoformat(first_night) + timedelta(days=1)).isoformat()
    oura_end = (date.fromisoformat(last_night) + timedelta(days=2)).isoformat()  # exclusive
    by_day = _main_sleep_by_day(fetch_sleep(conn, oura_start, oura_end))
    result = {"nights_found": len(by_day), "filled": 0, "updated": 0}
    for day, doc in sorted(by_day.items()):
        night = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        _write_night(conn, night, doc, result)
    return result


SERIES_CHUNK_DAYS = 60


def backfill_series(conn):
    """Fetch the raw within-night series for every recorded night that doesn't have
    one yet (the metrics themselves are left alone). Pulls Oura in 60-day chunks
    spanning the missing dates. Returns {"missing": n, "fetched": n}."""
    missing = store.nights_missing_series(conn)
    result = {"missing": len(missing), "fetched": 0}
    if not missing:
        return result
    want = set(missing)
    start = date.fromisoformat(missing[0])
    last = date.fromisoformat(missing[-1])
    while start <= last:
        end = min(start + timedelta(days=SERIES_CHUNK_DAYS - 1), last)
        by_day = _main_sleep_by_day(fetch_sleep(
            conn, (start + timedelta(days=1)).isoformat(), (end + timedelta(days=2)).isoformat()))
        for day, doc in by_day.items():
            night = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
            if night in want and store.set_night_series(conn, night, _series_doc(doc)):
                result["fetched"] += 1
        start = end + timedelta(days=1)
    return result


def status(conn):
    """What the UI needs to render the Oura panel. Never includes the client secret."""
    prev = store.get_setting(conn, "oura_last_result")
    return {
        "configured": bool(store.get_setting(conn, "oura_client_id")
                           and store.get_setting(conn, "oura_client_secret")),
        "connected": bool(store.get_setting(conn, "oura_refresh_token")
                          or store.get_setting(conn, "oura_access_token")),
        "client_id": store.get_setting(conn, "oura_client_id", ""),
        "last_sync": store.get_setting(conn, "oura_last_sync"),
        "last_result": json.loads(prev) if prev else None,
    }
