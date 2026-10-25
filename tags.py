"""
tags.py -- night-shape tags for low-HRV nights.

Every night Oura records a within-night series: heart rate and HRV every five
minutes, a five-minute hypnogram, and movement every thirty seconds (pulled by
oura.py into store.night_series). For each night whose HRV came in below a
threshold (default 42) an LLM reads that raw series, side by side with what a
typical night of yours looks like, and assigns one or more *shape* tags --
"HR starts high", "HR slow to settle", "restless night", "late sleep onset", ...
The tagger reuses the shared vocabulary (tag_vocab) and may add a tag when none
fits; the vocabulary is seeded from an initial read of the recorded history.

Tags show on the Tracker between the resting-HR and total-sleep rows, and the
Reports flag any intervention whose nights carry a tag disproportionately often
(or rarely) -- see server.tag_associations.

Pure standard library, like the rest of the app: the Claude API is called over
raw HTTPS (urllib). The key is stored in the settings table (or read from
ANTHROPIC_API_KEY); nothing is sent anywhere unless tagging is switched on.
"""

import json
import math
import os
import threading
import traceback
import urllib.error
import urllib.request
from datetime import datetime

import store

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_THRESHOLD = 42.0
HTTP_TIMEOUT = 240          # seconds; one night is a single request
PER_PASS = 4                # nights tagged per background autosync pass
MAX_TAGS_PER_NIGHT = 4

# Seed vocabulary: drawn from a read of the first 713 recorded nights (242 below
# 42) -- see README "Night-shape tags". The tagger can extend it.
SEED_TAGS = [
    # heart-rate shape
    ("hr-starts-high", "HR starts high",
     "Heart rate over the first 30 minutes in bed is well above usual (typical median ~61 bpm, p90 ~66): the night begins aroused."),
    ("hr-slow-to-settle", "HR slow to settle",
     "Heart rate takes far longer than usual to come down near its overnight minimum (typical ~2 h; here 3.5 h or more, or the minimum only late in the night)."),
    ("hr-elevated-throughout", "HR elevated throughout",
     "The whole heart-rate curve sits high: the overnight minimum itself is well above usual (typical median 52 bpm, p90 54) even though its shape is normal."),
    ("hr-flat", "HR flat",
     "Heart rate barely dips or varies: small spread and little difference between the start, the minimum and the end of the night."),
    ("hr-rises-late", "HR rises late",
     "Heart rate climbs in the final hour or two well above the overnight minimum, as if sleep lightened early."),
    ("hr-spiky", "HR spiky",
     "Repeated sharp heart-rate excursions (several samples 8+ bpm above the night's mean) rather than one smooth curve."),
    # HRV profile
    ("hrv-suppressed-early", "HRV suppressed early",
     "The usual early-night HRV rise is missing: the first two hours peak well below usual (typical peak ~57 ms, p10 47) and the first half runs low, with recovery only later if at all."),
    ("hrv-low-throughout", "HRV low throughout",
     "HRV is uniformly low from the first hour to the last with no recovery phase -- the whole night, not one bad stretch."),
    ("hrv-collapses-late", "HRV collapses late",
     "HRV starts near normal and falls through the second half of the night."),
    # sleep structure
    ("restless-night", "Restless night",
     "Much more movement and more wake bouts than usual after falling asleep (movement in more than ~16% of 30-second epochs, 8+ wake bouts, or Oura restless periods above ~330)."),
    ("late-sleep-onset", "Late sleep onset",
     "Fell asleep much later by the clock than usual (sleep onset past about 2:30 am)."),
    ("long-latency", "Long latency",
     "A long stretch awake in bed before first falling asleep (40+ minutes)."),
    ("long-wake-bout", "Long wake bout",
     "At least one extended awakening of 25+ minutes in the middle of the night."),
    ("short-night", "Short night",
     "Total sleep well below usual (under about 6.3 hours): a short time in bed or an early final wake."),
    ("little-deep-sleep", "Little deep sleep",
     "Deep sleep well below usual (under about 55 minutes), the night dominated by light and REM."),
]

# Bookkeeping for the background tagger (one at a time per process).
_STATUS = {"running": False, "done": 0, "total": 0, "error": None, "started": None,
           "last_date": None, "last_tags": None}
_LOCK = threading.Lock()


class TagError(Exception):
    """Readable failure for the UI."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def api_key(conn):
    return store.get_setting(conn, "anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY") or ""


def enabled(conn):
    return store.get_setting(conn, "tags_enabled") == "1"


def threshold(conn):
    v = store.get_setting(conn, "tags_threshold")
    try:
        return float(v) if v else DEFAULT_THRESHOLD
    except ValueError:
        return DEFAULT_THRESHOLD


def model_id(conn):
    return store.get_setting(conn, "tags_model") or DEFAULT_MODEL


def ensure_seed(conn):
    """Install the seed vocabulary into an empty vocab table."""
    if not store.tag_vocab(conn):
        for slug, label, desc in SEED_TAGS:
            store.upsert_tag_vocab(conn, slug, label, desc, "seed")


# ---------------------------------------------------------------------------
# Feature extraction from a stored series document
# ---------------------------------------------------------------------------
def _clean(items):
    return [float(x) for x in (items or []) if x is not None]


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _slope(xs, step_h):
    """Least-squares slope in units per hour over equally spaced samples."""
    n = len(xs)
    if n < 3:
        return None
    t = [i * step_h for i in range(n)]
    tm, xm = _mean(t), _mean(xs)
    den = sum((ti - tm) ** 2 for ti in t)
    return sum((ti - tm) * (xi - xm) for ti, xi in zip(t, xs)) / den if den else None


def _clock_hours(iso):
    """Local clock time as hours past midnight, with evening times negative so
    that 23:30 -> -0.5 and 01:08 -> 1.13 (keeps 'later' monotone across midnight)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    h = dt.hour + dt.minute / 60.0
    return h - 24.0 if h >= 15 else h


def features(doc):
    """Numeric descriptors of one night's shape. Every value may be None when the
    underlying series is missing."""
    f = {}
    hr_raw = (doc.get("heart_rate") or {}).get("items") or []
    hrv_raw = (doc.get("hrv") or {}).get("items") or []
    step_h = ((doc.get("heart_rate") or {}).get("interval") or 300.0) / 3600.0
    hr = [(i * step_h, float(x)) for i, x in enumerate(hr_raw) if x is not None]
    hrv = [(i * step_h, float(x)) for i, x in enumerate(hrv_raw) if x is not None]
    tib_h = (doc.get("time_in_bed") or 0) / 3600.0
    f["time_in_bed_h"] = round(tib_h, 2) if tib_h else None
    f["total_sleep_h"] = round((doc.get("total_sleep_duration") or 0) / 3600.0, 2) if doc.get("total_sleep_duration") else None
    f["latency_min"] = round((doc.get("latency") or 0) / 60.0, 1) if doc.get("latency") is not None else None
    f["awake_min"] = round((doc.get("awake_time") or 0) / 60.0, 1) if doc.get("awake_time") is not None else None
    f["efficiency"] = doc.get("efficiency")
    f["restless_periods"] = doc.get("restless_periods")
    f["deep_min"] = round((doc.get("deep_sleep_duration") or 0) / 60.0) if doc.get("deep_sleep_duration") is not None else None
    f["rem_min"] = round((doc.get("rem_sleep_duration") or 0) / 60.0) if doc.get("rem_sleep_duration") is not None else None
    f["light_min"] = round((doc.get("light_sleep_duration") or 0) / 60.0) if doc.get("light_sleep_duration") is not None else None
    f["bedtime_clock"] = _clock_hours(doc.get("bedtime_start"))
    f["waketime_clock"] = _clock_hours(doc.get("bedtime_end"))
    f["avg_breath"] = doc.get("average_breath")

    # heart rate
    if hr:
        v = [x for _, x in hr]
        f["hr_first30"] = round(_mean(v[:6]), 1)
        f["hr_first_hour"] = round(_mean(v[:12]), 1)
        f["hr_last_hour"] = round(_mean(v[-12:]), 1)
        f["hr_mean"] = round(_mean(v), 1)
        f["hr_min"] = min(v)
        f["hr_std"] = round(_std(v), 2) if len(v) > 2 else None
        imin = min(range(len(v)), key=lambda i: v[i])
        f["hr_min_at_h"] = round(hr[imin][0], 2)
        f["hr_min_at_frac"] = round(hr[imin][0] / tib_h, 2) if tib_h else None
        f["hr_start_minus_min"] = round(f["hr_first30"] - f["hr_min"], 1)
        f["hr_end_minus_min"] = round(f["hr_last_hour"] - f["hr_min"], 1)
        half = max(3, len(v) // 2)
        f["hr_first_half_mean"] = round(_mean(v[:half]), 1)
        f["hr_second_half_mean"] = round(_mean(v[half:]) if v[half:] else _mean(v), 1)
        f["hr_first_half_slope_per_h"] = round(_slope(v[:half], step_h), 2) if half >= 3 else None
        # how long until HR first gets within 3 bpm of its overnight minimum
        settle = next((t for t, x in hr if x <= f["hr_min"] + 3), None)
        f["hr_settle_h"] = round(settle, 2) if settle is not None else None
        # excursions: samples at least 8 bpm above the overnight mean
        f["hr_spikes"] = sum(1 for x in v if x >= f["hr_mean"] + 8)
    # HRV
    if hrv:
        v = [x for _, x in hrv]
        f["hrv_first_hour"] = round(_mean(v[:12]), 1)
        f["hrv_last_hour"] = round(_mean(v[-12:]), 1)
        f["hrv_mean"] = round(_mean(v), 1)
        f["hrv_max"] = max(v)
        f["hrv_std"] = round(_std(v), 2) if len(v) > 2 else None
        imax = max(range(len(v)), key=lambda i: v[i])
        f["hrv_max_at_frac"] = round(hrv[imax][0] / tib_h, 2) if tib_h else None
        half = max(3, len(v) // 2)
        f["hrv_first_half_mean"] = round(_mean(v[:half]), 1)
        f["hrv_second_half_mean"] = round(_mean(v[half:]) if v[half:] else _mean(v), 1)
        f["hrv_slope_per_h"] = round(_slope(v, step_h), 2)
        f["hrv_first2h_max"] = max(v[:24]) if v[:24] else None
    # hypnogram (5-minute): 1 deep, 2 light, 3 REM, 4 awake
    hyp = doc.get("sleep_phase_5_min") or ""
    if hyp:
        onset = next((i for i, c in enumerate(hyp) if c in "123"), None)
        if onset is not None:
            after = hyp[onset:]
            f["onset_h"] = round(onset * 5 / 60.0, 2)
            f["onset_clock"] = (round(f["bedtime_clock"] + f["onset_h"], 2)
                                if f["bedtime_clock"] is not None else None)
            # last sleep epoch -> final wake
            last_sleep = max(i for i, c in enumerate(hyp) if c in "123")
            core = hyp[onset:last_sleep + 1]
            f["waso_min"] = core.count("4") * 5
            runs, cur = [], 0
            for c in core:
                if c == "4":
                    cur += 1
                elif cur:
                    runs.append(cur); cur = 0
            f["wake_bouts"] = len(runs)
            f["longest_wake_min"] = max(runs) * 5 if runs else 0
            f["long_wake_bouts"] = sum(1 for r in runs if r >= 3)
            f["tail_awake_min"] = (len(hyp) - 1 - last_sleep) * 5
            f["deep_frac_of_sleep"] = round(core.count("1") / max(1, len(core) - core.count("4")), 2)
    # movement (30-second epochs, 1 = still ... 4 = lots)
    mov = doc.get("movement_30_sec") or ""
    if mov:
        digits = [int(c) for c in mov if c.isdigit()]
        if digits:
            f["move_frac_any"] = round(sum(1 for d in digits if d >= 2) / len(digits), 3)
            f["move_frac_high"] = round(sum(1 for d in digits if d >= 3) / len(digits), 3)
            f["move_mean"] = round(_mean(digits), 3)
    return f


# Features shown to the tagger with reference percentiles, in display order.
FEATURE_LABELS = [
    ("bedtime_clock", "bedtime (clock h, 0 = midnight)"),
    ("onset_clock", "sleep onset (clock h)"),
    ("latency_min", "sleep latency (min)"),
    ("time_in_bed_h", "time in bed (h)"),
    ("total_sleep_h", "total sleep (h)"),
    ("efficiency", "efficiency (%)"),
    ("awake_min", "awake time (min)"),
    ("waso_min", "wake after onset (min)"),
    ("wake_bouts", "wake bouts after onset"),
    ("longest_wake_min", "longest wake bout (min)"),
    ("tail_awake_min", "awake at end before rising (min)"),
    ("restless_periods", "restless periods (Oura count)"),
    ("move_frac_any", "fraction of 30s epochs with movement"),
    ("move_frac_high", "fraction of 30s epochs with heavy movement"),
    ("deep_min", "deep sleep (min)"),
    ("rem_min", "REM sleep (min)"),
    ("light_min", "light sleep (min)"),
    ("hr_first30", "HR first 30 min (bpm)"),
    ("hr_first_hour", "HR first hour (bpm)"),
    ("hr_mean", "HR mean (bpm)"),
    ("hr_min", "HR minimum (bpm)"),
    ("hr_min_at_frac", "when HR hits its minimum (fraction of night)"),
    ("hr_settle_h", "hours until HR within 3 bpm of its minimum"),
    ("hr_start_minus_min", "HR start minus minimum (bpm)"),
    ("hr_end_minus_min", "HR last hour minus minimum (bpm)"),
    ("hr_first_half_slope_per_h", "HR slope over first half (bpm/h)"),
    ("hr_std", "HR spread (sd, bpm)"),
    ("hr_spikes", "HR samples 8+ bpm above the night mean"),
    ("hrv_first_hour", "HRV first hour (ms)"),
    ("hrv_first2h_max", "HRV peak in first 2 h (ms)"),
    ("hrv_first_half_mean", "HRV first half (ms)"),
    ("hrv_second_half_mean", "HRV second half (ms)"),
    ("hrv_last_hour", "HRV last hour (ms)"),
    ("hrv_max", "HRV peak (ms)"),
    ("hrv_max_at_frac", "when HRV peaks (fraction of night)"),
    ("hrv_slope_per_h", "HRV slope (ms/h)"),
    ("hrv_std", "HRV spread (sd, ms)"),
    ("avg_breath", "breathing rate (per min)"),
]


def _pct(sorted_xs, q):
    if not sorted_xs:
        return None
    k = (len(sorted_xs) - 1) * q
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (k - lo)


def reference(conn, thresh):
    """Per-feature p10 / p50 / p90 over typical nights (HRV at or above the
    threshold) and over low nights, from every stored series."""
    docs = store.all_night_series(conn)
    typical, low = {}, {}
    n_typ = n_low = 0
    for d, doc in docs.items():
        h = doc.get("_hrv")
        if h is None:
            continue
        f = features(doc)
        bucket = typical if h >= thresh else low
        if h >= thresh:
            n_typ += 1
        else:
            n_low += 1
        for k, v in f.items():
            if isinstance(v, (int, float)):
                bucket.setdefault(k, []).append(float(v))
    out = {"n_typical": n_typ, "n_low": n_low, "typical": {}, "low": {}}
    for name, bucket in (("typical", typical), ("low", low)):
        for k, xs in bucket.items():
            xs.sort()
            out[name][k] = (round(_pct(xs, .1), 2), round(_pct(xs, .5), 2), round(_pct(xs, .9), 2))
    return out


# ---------------------------------------------------------------------------
# Rendering a night for the tagger
# ---------------------------------------------------------------------------
PHASE = {"1": "deep", "2": "light", "3": "REM", "4": "awake"}


def _fmt(v):
    if v is None:
        return "—"
    return f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def _position(v, p):
    """Where a value sits against (p10, p50, p90): a short word for the tagger."""
    if v is None or p is None:
        return ""
    lo, mid, hi = p
    if v > hi:
        return "ABOVE p90"
    if v < lo:
        return "BELOW p10"
    return "above median" if v > mid else "below median"


def reference_text(ref):
    lines = [f"Reference from your own history: {ref['n_typical']} typical nights (HRV at or above "
             f"the threshold) and {ref['n_low']} low nights. Columns: p10 / median / p90 of typical "
             f"nights, then of low nights."]
    for key, label in FEATURE_LABELS:
        t, l = ref["typical"].get(key), ref["low"].get(key)
        if not t:
            continue
        lines.append(f"- {label}: typical {_fmt(t[0])} / {_fmt(t[1])} / {_fmt(t[2])}"
                     + (f"; low {_fmt(l[0])} / {_fmt(l[1])} / {_fmt(l[2])}" if l else ""))
    return "\n".join(lines)


def night_text(d, doc, hrv_value, ref):
    """The one night, as the tagger sees it: summary features against the typical
    reference, then the full 5-minute series."""
    f = features(doc)
    lines = [f"Night of {d} (evening date). Overnight HRV: {hrv_value} ms.",
             f"Bedtime {doc.get('bedtime_start')} to {doc.get('bedtime_end')}.", "",
             "Summary features (value, and where it sits among your typical nights):"]
    for key, label in FEATURE_LABELS:
        v = f.get(key)
        if v is None:
            continue
        lines.append(f"- {label}: {_fmt(v)}  [{_position(v, ref['typical'].get(key))}]")
    lines += ["", "Five-minute series from bedtime (t = hours in bed; HR bpm; HRV ms; sleep phase; "
              "movement = mean of the ten 30-second movement scores in that window, 1 still .. 4 heavy):"]
    hr = (doc.get("heart_rate") or {}).get("items") or []
    hv = (doc.get("hrv") or {}).get("items") or []
    hyp = doc.get("sleep_phase_5_min") or ""
    mov = doc.get("movement_30_sec") or ""
    n = max(len(hr), len(hv), len(hyp))
    for i in range(n):
        t = i * 5 / 60.0
        hr_i = hr[i] if i < len(hr) else None
        hv_i = hv[i] if i < len(hv) else None
        ph = PHASE.get(hyp[i], "?") if i < len(hyp) else "?"
        chunk = [int(c) for c in mov[i * 10:(i + 1) * 10] if c.isdigit()]
        mv = f"{_mean(chunk):.1f}" if chunk else "—"
        lines.append(f"t={t:4.2f}h  HR {_fmt(hr_i):>4}  HRV {_fmt(hv_i):>4}  {ph:<5}  move {mv}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The Claude call
# ---------------------------------------------------------------------------
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "One sentence (under 160 characters) on what made this night's shape unusual."},
        "tags": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string", "description": "kebab-case id; an existing slug when one fits"},
                    "label": {"type": "string", "description": "short display label, 2-5 words"},
                    "description": {"type": "string",
                                    "description": "what pattern in the data earns this tag (one sentence); for an existing slug, repeat its description"},
                },
                "required": ["slug", "label", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "tags"],
    "additionalProperties": False,
}


def system_prompt(vocab, ref, thresh):
    vocab_lines = "\n".join(f"- {v['slug']} — {v['label']}: {v['description']}" for v in vocab) or "- (none yet)"
    return (
        "You read one person's overnight wearable data (Oura ring) and describe the *shape* of a "
        f"night whose overnight HRV came in below {_fmt(thresh)} ms -- a low night for this person. "
        "The aim is to sort low nights into recurring kinds, so that later analysis can ask whether "
        "a given intervention produces one kind of bad night more than another.\n\n"
        "Assign one to " + str(MAX_TAGS_PER_NIGHT) + " tags that describe what is unusual about the "
        "night compared with this person's typical nights. Prefer tags from the vocabulary below; "
        "reuse a slug exactly when it fits. Add a new tag only when no existing one captures a clear, "
        "recurring-looking pattern you can point to in the data -- give it a kebab-case slug, a short "
        "label and a one-sentence description of the data pattern. Do not invent causes (illness, "
        "alcohol, stress): tag only what the series shows. If nothing stands out apart from HRV "
        "being low, use the tag that best describes the HRV profile itself.\n\n"
        "Current vocabulary:\n" + vocab_lines + "\n\n" + reference_text(ref)
    )


def _post(key, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(API_URL, data=data, headers={
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": API_VERSION,
        "anthropic-beta": "server-side-fallback-2026-07-01",
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        txt = ""
        try:
            txt = e.read().decode()[:400]
        except Exception:
            pass
        if e.code in (401, 403):
            raise TagError("The API key was rejected — check it in the Night tags panel.") from e
        raise TagError(f"Claude API returned {e.code}: {txt or e.reason}") from e
    except urllib.error.URLError as e:
        raise TagError(f"Could not reach the Claude API: {e.reason}") from e


def classify(key, model, system, user_text):
    """One request; returns (parsed JSON output, usage dict)."""
    body = {
        "model": model,
        "max_tokens": 4000,
        "fallbacks": "default",
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user_text}],
        "output_config": {"effort": "medium",
                          "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    }
    resp = _post(key, body)
    if resp.get("stop_reason") == "refusal":
        det = resp.get("stop_details") or {}
        raise TagError("The model declined this request" + (f" ({det.get('category')})" if det.get("category") else "") + ".")
    if resp.get("stop_reason") == "max_tokens":
        raise TagError("The model's answer was cut off (max_tokens).")
    text = next((b.get("text") for b in resp.get("content", []) if b.get("type") == "text"), None)
    if not text:
        raise TagError("The model returned no text.")
    try:
        return json.loads(text), resp.get("usage") or {}
    except ValueError as e:
        raise TagError("The model's answer was not valid JSON.") from e


def _slugify(s):
    out = "".join(c if c.isalnum() else "-" for c in (s or "").lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:48]


def tag_night(conn, d, key=None, model=None, ref=None, thresh=None):
    """Tag one night. Writes the tags (and any new vocabulary) and returns the
    stored record. Raises TagError on API trouble."""
    key = key or api_key(conn)
    if not key:
        raise TagError("No API key — paste one in the Night tags panel.")
    model = model or model_id(conn)
    thresh = threshold(conn) if thresh is None else thresh
    ensure_seed(conn)
    doc = store.get_night_series(conn, d)
    if not doc:
        raise TagError(f"No raw series stored for {d}.")
    night = store.get_night(conn, d)
    hrv_value = night["hrv"] if night else None
    ref = ref or reference(conn, thresh)
    vocab = store.tag_vocab(conn)
    known = {v["slug"] for v in vocab}
    out, usage = classify(key, model, system_prompt(vocab, ref, thresh), night_text(d, doc, hrv_value, ref))
    slugs = []
    for t in (out.get("tags") or [])[:MAX_TAGS_PER_NIGHT]:
        slug = _slugify(t.get("slug"))
        if not slug:
            continue
        if slug not in known:
            store.upsert_tag_vocab(conn, slug, (t.get("label") or slug).strip()[:60],
                                   (t.get("description") or "").strip()[:300], model)
            known.add(slug)
        if slug not in slugs:
            slugs.append(slug)
    rank = {v["slug"]: i for i, v in enumerate(store.tag_vocab(conn))}
    slugs.sort(key=lambda s: rank.get(s, len(rank)))   # stored in vocabulary order
    summary = (out.get("summary") or "").strip()[:200]
    store.set_night_tags(conn, d, slugs, summary, model, hrv_value)
    store._audit(conn, "tag_usage", d, input=usage.get("input_tokens"), output=usage.get("output_tokens"),
                 cache_read=usage.get("cache_read_input_tokens"), cache_write=usage.get("cache_creation_input_tokens"))
    conn.commit()
    return {"date": d, "tags": slugs, "summary": summary, "model": model}


def tag_pending(conn, limit=PER_PASS, progress=None):
    """Tag up to `limit` untagged low-HRV nights (newest first). Stops at the first
    API error so a bad key doesn't burn through every night. Returns
    {"done": n, "remaining": n, "error": msg|None}."""
    thresh = threshold(conn)
    todo = store.nights_to_tag(conn, thresh)
    key, model = api_key(conn), model_id(conn)
    ref = reference(conn, thresh) if todo else None
    done, err = 0, None
    for d in todo[:limit] if limit else todo:
        try:
            rec = tag_night(conn, d, key=key, model=model, ref=ref, thresh=thresh)
            done += 1
            if progress:
                progress(rec)
        except TagError as e:
            err = str(e)
            break
    return {"done": done, "remaining": len(todo) - done, "error": err}


# ---------------------------------------------------------------------------
# Background worker (so the HTTP request that starts a run returns at once)
# ---------------------------------------------------------------------------
def worker_status():
    with _LOCK:
        return dict(_STATUS)


def _run(limit):
    conn = store.connect()
    try:
        thresh = threshold(conn)
        todo = store.nights_to_tag(conn, thresh)
        with _LOCK:
            _STATUS.update(total=min(len(todo), limit) if limit else len(todo), done=0)

        def progress(rec):
            with _LOCK:
                _STATUS["done"] += 1
                _STATUS["last_date"] = rec["date"]
                _STATUS["last_tags"] = rec["tags"]
        res = tag_pending(conn, limit=limit, progress=progress)
        with _LOCK:
            _STATUS["error"] = res["error"]
    except Exception as e:
        traceback.print_exc()
        with _LOCK:
            _STATUS["error"] = str(e)
    finally:
        conn.close()
        with _LOCK:
            _STATUS["running"] = False


def run_pending_async(limit=None):
    """Start tagging every pending night in a background thread (no-op if a run is
    already going). Returns the worker status."""
    with _LOCK:
        if not _STATUS["running"]:
            _STATUS.update(running=True, done=0, total=0, error=None, last_date=None,
                           last_tags=None, started=datetime.now().isoformat(timespec="seconds"))
            threading.Thread(target=_run, args=(limit,), name="tagger", daemon=True).start()
        return dict(_STATUS)


# ---------------------------------------------------------------------------
# What the UI needs
# ---------------------------------------------------------------------------
def status(conn):
    thresh = threshold(conn)
    ensure_seed(conn)
    counts = store.tag_counts(conn, thresh)
    use = {}
    for rec in store.night_tags(conn).values():
        for s in rec["tags"]:
            use[s] = use.get(s, 0) + 1
    vocab = [{**v, "n": use.get(v["slug"], 0)} for v in store.tag_vocab(conn)]
    return {
        "enabled": enabled(conn),
        "configured": bool(api_key(conn)),
        "key_from_env": not store.get_setting(conn, "anthropic_api_key") and bool(os.environ.get("ANTHROPIC_API_KEY")),
        "threshold": thresh,
        "model": model_id(conn),
        "pending": len(store.nights_to_tag(conn, thresh)),
        "counts": counts,
        "vocab": vocab,
        "worker": worker_status(),
    }
