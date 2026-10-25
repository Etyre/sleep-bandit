"""
weather.py -- outdoor temperature for each night, so a report can ask whether the
best Chili Pad / air-conditioning setting depends on the season.

Source: Open-Meteo (open-meteo.com), free, no key. Hourly 2 m air temperature for
the configured location (Berkeley, CA by default; SB_LAT / SB_LON override). The
archive endpoint covers everything up to a few days ago; the forecast endpoint's
`past_days` covers the most recent week, so the two together leave no gap.

A night d (evening date) gets:
  tnight  mean temperature from 21:00 on d to 06:00 on d+1   (the sleeping hours)
  tmin    minimum over the same window
  tmax    maximum over the daytime hours of d
all in °C in the database; the UI converts to the intervention's own unit.

Pure standard library, like the rest of the app. Failures are logged, never fatal.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import store

LAT = float(os.environ.get("SB_LAT", "37.8716"))
LON = float(os.environ.get("SB_LON", "-122.2728"))
TZ = os.environ.get("SB_TZ", "America/Los_Angeles")
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT = 25
CHUNK_DAYS = 180            # archive request size
RECENT_DAYS = 7             # the forecast endpoint's past_days window we rely on
SYNC_THROTTLE_HOURS = 6     # auto-syncs closer together than this are skipped
NIGHT_START_H, NIGHT_END_H = 21, 6


class WeatherError(Exception):
    pass


def _get(url, params):
    try:
        with urllib.request.urlopen(url + "?" + urllib.parse.urlencode(params), timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise WeatherError(f"Open-Meteo returned {e.code}") from e
    except urllib.error.URLError as e:
        raise WeatherError(f"Could not reach Open-Meteo: {e.reason}") from e


def _hourly(start, end):
    """{ 'YYYY-MM-DDTHH:00': temp } for start <= day <= end (ISO strings), from the
    archive for older days and the forecast endpoint for the last week."""
    out = {}
    today = date.today()
    recent_from = today - timedelta(days=RECENT_DAYS - 1)
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    # archive part (anything before the recent window)
    a_end = min(e, recent_from - timedelta(days=1))
    cur = s
    while cur <= a_end:
        chunk_end = min(a_end, cur + timedelta(days=CHUNK_DAYS - 1))
        d = _get(ARCHIVE_URL, {"latitude": LAT, "longitude": LON, "start_date": cur.isoformat(),
                               "end_date": chunk_end.isoformat(), "hourly": "temperature_2m", "timezone": TZ})
        h = d.get("hourly") or {}
        out.update({t: v for t, v in zip(h.get("time", []), h.get("temperature_2m", [])) if v is not None})
        cur = chunk_end + timedelta(days=1)
    # recent part
    if e >= recent_from:
        d = _get(FORECAST_URL, {"latitude": LAT, "longitude": LON, "past_days": RECENT_DAYS,
                                "forecast_days": 1, "hourly": "temperature_2m", "timezone": TZ})
        h = d.get("hourly") or {}
        out.update({t: v for t, v in zip(h.get("time", []), h.get("temperature_2m", [])) if v is not None})
    return out


def night_stats(hourly, d):
    """(tnight, tmin, tmax) for evening d from an hourly dict; None where the hours
    are missing (e.g. a night that hasn't finished)."""
    nxt = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
    night = [hourly.get(f"{d}T{h:02d}:00") for h in range(NIGHT_START_H, 24)] + \
            [hourly.get(f"{nxt}T{h:02d}:00") for h in range(0, NIGHT_END_H + 1)]
    night = [v for v in night if v is not None]
    day = [v for v in (hourly.get(f"{d}T{h:02d}:00") for h in range(0, 24)) if v is not None]
    if len(night) < 7:
        return None, None, None
    return (round(sum(night) / len(night), 1), round(min(night), 1),
            round(max(day), 1) if day else None)


def sync(conn, current_night, force=False):
    """Fill weather for every recorded night that lacks it (up to last night; tonight
    hasn't happened). Throttled unless forced. Returns a summary dict."""
    last = store.get_setting(conn, "weather_last_sync")
    if last and not force:
        if datetime.now() - datetime.fromisoformat(last) < timedelta(hours=SYNC_THROTTLE_HOURS):
            return {"skipped": True}
    missing = store.nights_missing_weather(conn, "2000-01-01", current_night)
    result = {"missing": len(missing), "filled": 0}
    if missing:
        try:
            hourly = _hourly(missing[0], (date.fromisoformat(missing[-1]) + timedelta(days=1)).isoformat())
        except WeatherError as e:
            result["error"] = str(e)
            return result
        for d in missing:
            tn, tmin, tmax = night_stats(hourly, d)
            if tn is None:
                continue
            store.set_weather(conn, d, tn, tmin, tmax)
            result["filled"] += 1
        conn.commit()
    store.set_setting(conn, "weather_last_sync", datetime.now().isoformat(timespec="seconds"))
    return result
