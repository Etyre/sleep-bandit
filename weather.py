"""
weather.py -- outdoor temperature for each night, where it was slept, so a report can
ask whether the best Chili Pad / air-conditioning setting depends on the season.

Source: Open-Meteo (open-meteo.com), free, no key. Hourly 2 m air temperature at the
night's city (Tracker dropdown; store.effective_cities carries a choice forward to
later nights). The archive endpoint covers everything up to a few days ago; the
forecast endpoint's `past_days` covers the most recent week, so the two together
leave no gap. New cities are geocoded through Open-Meteo's geocoding API.

A night d (evening date) gets, in the city's own time zone:
  tnight  mean temperature over the hours actually asleep -- Oura's bedtime start to
          bedtime end for that night (store.night_series); 21:00 on d to 06:00 on
          d+1 when no sleep record exists yet (src 'clock', redone once it does)
  tmin    minimum over the same window
  tmax    maximum over the daytime hours of d
all in °C in the database; the UI converts to the intervention's own unit.

Pure standard library, like the rest of the app. Failures are logged, never fatal.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import store

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
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


def _hourly(city, start, end):
    """{ 'YYYY-MM-DDTHH:00': temp } at `city` ({name, lat, lon, tz}) for start <= day
    <= end (ISO strings): the archive for older days, the forecast endpoint for the
    last week."""
    base = {"latitude": city["lat"], "longitude": city["lon"], "hourly": "temperature_2m", "timezone": city["tz"]}
    out = {}
    today = date.today()
    recent_from = today - timedelta(days=RECENT_DAYS - 1)
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    a_end = min(e, recent_from - timedelta(days=1))
    cur = s
    while cur <= a_end:
        chunk_end = min(a_end, cur + timedelta(days=CHUNK_DAYS - 1))
        d = _get(ARCHIVE_URL, {**base, "start_date": cur.isoformat(), "end_date": chunk_end.isoformat()})
        h = d.get("hourly") or {}
        out.update({t: v for t, v in zip(h.get("time", []), h.get("temperature_2m", [])) if v is not None})
        cur = chunk_end + timedelta(days=1)
    if e >= recent_from:
        d = _get(FORECAST_URL, {**base, "past_days": RECENT_DAYS, "forecast_days": 1})
        h = d.get("hourly") or {}
        out.update({t: v for t, v in zip(h.get("time", []), h.get("temperature_2m", [])) if v is not None})
    return out


def geocode(name):
    """Best match for a place name: {name, lat, lon, tz}. Raises WeatherError if none."""
    d = _get(GEOCODE_URL, {"name": name, "count": 1, "language": "en", "format": "json"})
    hits = d.get("results") or []
    if not hits:
        raise WeatherError(f"No place called “{name}” found.")
    h = hits[0]
    label = h.get("name") or name
    return {"name": label, "lat": h["latitude"], "lon": h["longitude"], "tz": h.get("timezone") or "UTC",
            "admin": h.get("admin1") or "", "country": h.get("country") or ""}


def _local_window(window, tz):
    """(start, end) naive local datetimes in the city's zone from Oura's ISO
    timestamps (which carry the ring's own offset); None if unparsable."""
    try:
        z = ZoneInfo(tz)
        s = datetime.fromisoformat(window[0]).astimezone(z).replace(tzinfo=None)
        e = datetime.fromisoformat(window[1]).astimezone(z).replace(tzinfo=None)
    except (ValueError, TypeError, KeyError):
        return None
    return (s, e) if e > s else None


def night_stats(hourly, d, window=None, tz="UTC"):
    """(tnight, tmin, tmax, src) for evening d from an hourly dict. With `window`
    (Oura bedtime start/end) the night covers every hourly sample within the sleep
    period, padded half an hour each side so a short night still has samples; else
    the 21:00-06:00 clock fallback. None values where the hours are missing."""
    loc = _local_window(window, tz) if window else None
    if loc:
        s, e = loc[0] - timedelta(minutes=30), loc[1] + timedelta(minutes=30)
        t = s.replace(minute=0, second=0, microsecond=0)
        keys = []
        while t <= e:
            if t >= s:
                keys.append(t.strftime("%Y-%m-%dT%H:00"))
            t += timedelta(hours=1)
        src = "sleep"
    else:
        nxt = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
        keys = [f"{d}T{h:02d}:00" for h in range(NIGHT_START_H, 24)] + \
               [f"{nxt}T{h:02d}:00" for h in range(0, NIGHT_END_H + 1)]
        src = "clock"
    night = [hourly.get(k) for k in keys]
    night = [v for v in night if v is not None]
    day = [v for v in (hourly.get(f"{d}T{h:02d}:00") for h in range(0, 24)) if v is not None]
    if len(night) < max(2, len(keys) // 2):
        return None, None, None, src
    return (round(sum(night) / len(night), 1), round(min(night), 1),
            round(max(day), 1) if day else None, src)


def sync(conn, current_night, force=False):
    """Fill weather for every recorded night that lacks it at the city it was slept in
    (up to last night; tonight hasn't happened). Throttled unless forced. Returns
    a summary dict."""
    last = store.get_setting(conn, "weather_last_sync")
    if last and not force:
        if datetime.now() - datetime.fromisoformat(last) < timedelta(hours=SYNC_THROTTLE_HOURS):
            return {"skipped": True}
    missing = store.nights_missing_weather(conn, current_night)
    result = {"missing": len(missing), "filled": 0}
    cities = {c["name"]: c for c in store.list_cities(conn)}
    windows = store.sleep_windows(conn)
    by_city = {}
    for d, c in missing:
        by_city.setdefault(c, []).append(d)
    for cname, dates in by_city.items():
        city = cities.get(cname)
        if not city:
            continue
        try:   # a day either side: a late wake or an early bedtime crosses the date
            hourly = _hourly(city, (date.fromisoformat(dates[0]) - timedelta(days=1)).isoformat(),
                             (date.fromisoformat(dates[-1]) + timedelta(days=1)).isoformat())
        except WeatherError as e:
            result["error"] = str(e)
            continue
        for d in dates:
            tn, tmin, tmax, src = night_stats(hourly, d, windows.get(d), city["tz"])
            if tn is None:
                continue
            store.set_weather(conn, d, cname, tn, tmin, tmax, src)
            result["filled"] += 1
    conn.commit()
    if "error" not in result:
        store.set_setting(conn, "weather_last_sync", datetime.now().isoformat(timespec="seconds"))
    return result
