"""
server.py -- Sleep Bandit web app (pure Python standard library).

Run:  python3 server.py         then open http://localhost:8777

Spreadsheet UI: rows are interventions, columns are nights. Each night the app
decides (a simple coin flip, for now) whether each intervention is "on"; if so
it suggests the default dose and you tick a checkmark once you've done it.

The Bayesian bandit engine still lives in bandit.py for later; right now planning
is a plain coin flip per the current design.
"""

import json
import math
import os
import secrets
import statistics
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import auth
import bandit
import oura
import store

HOST = os.environ.get("SB_HOST", "127.0.0.1")
PORT = int(os.environ.get("SB_PORT", "8777"))
PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "public")
GRID_NIGHTS = 21          # how many night-columns to show (newest first)

# ---------------------------------------------------------------------------
# Hosted ("public") mode -- see deploy/README.md. Off by default: on your own
# machine everything is read/write with no login, exactly as before.
#   SB_PUBLIC=1            the root is a READ-ONLY view (no notes, no writes)
#   SB_ADMIN_PATH=...      the unlinked path that asks for the password and then
#                          gives full read/write (default "admin")
#   SB_PASSWORD_HASH=...   from `python3 auth.py`
#   SB_BASE_URL=https://.. the public URL (Oura's redirect goes back here)
#   SB_AUTOSYNC=1          plan tonight + Oura-sync in the background (default on
#                          when hosted, off locally where opening the app does it)
# ---------------------------------------------------------------------------
PUBLIC_MODE = os.environ.get("SB_PUBLIC", "") == "1"
ADMIN_PATH = "/" + os.environ.get("SB_ADMIN_PATH", "admin").strip("/")
BASE_URL = os.environ.get("SB_BASE_URL", "").rstrip("/") or None
PASSWORD_HASH = os.environ.get("SB_PASSWORD_HASH", "")
AUTOSYNC = os.environ.get("SB_AUTOSYNC", "1" if PUBLIC_MODE else "0") == "1"
AUTOSYNC_MINUTES = 20

# A "night" rolls over at 4 AM, not midnight. A routine done in the small hours
# (any time before 4 AM) belongs to the PREVIOUS evening's night -- that's how the
# user files it ("last night" stays last night whether the routine was done before
# or after midnight). So the current night's date is (now - 4h)'s calendar date.
NIGHT_ROLLOVER_HOUR = 4


def today_str():
    """The date of the CURRENT night. Rolls over at NIGHT_ROLLOVER_HOUR (4 AM), so
    e.g. 1:30 AM on the 12th still reads as the night of the 11th, and only at
    4:00 AM does it advance to the 12th."""
    return (datetime.now() - timedelta(hours=NIGHT_ROLLOVER_HOUR)).date().isoformat()


# ---------------------------------------------------------------------------
# Recency weighting (server-wide mode, toggled from the header switch).
# When on, every night's datapoint -- on and off arms alike -- is weighted by
# w = 0.5^(age / half-life): the newest night counts fully, a half-life-old
# night counts half, and so on. The weight scales BOTH the values (weighted
# means, weighted likelihood in the model) and the sample sizes (effective
# counts, shown as "adjusted"), and it drives the live bandit too: Thompson
# planning and the warm-up ramp see the weighted evidence. The early lock-in
# guard is DISABLED in this mode -- see ensure_tonight.
# With nightly logging, decay caps total effective evidence at about
# half-life / ln 2 (~1.44 x half-life) nights, however long the history.
# ---------------------------------------------------------------------------
RECENCY_HALF_LIFE_DAYS = 60.0


def recency_enabled(conn):
    return store.get_setting(conn, "recency_weighting") == "1"


def apply_recency(nights):
    """Attach a recency weight to every analysis night. Age is measured from the
    most recent night with any recorded metric (not the wall clock), so a logging
    gap doesn't silently decay the whole dataset. Mutates and returns the list."""
    if not nights:
        return nights
    anchor = date.fromisoformat(max(n["date"] for n in nights))
    for n in nights:
        age = (anchor - date.fromisoformat(n["date"])).days
        n["w"] = 0.5 ** (age / RECENCY_HALF_LIFE_DAYS)
    return nights


def analysis_nights_weighted(conn):
    """store.analysis_nights, with recency weights attached when the mode is on."""
    nights = store.analysis_nights(conn)
    return apply_recency(nights) if recency_enabled(conn) else nights


def _nn(x):
    """Display-friendly night count: exact int when integral (the unweighted
    case), else one decimal (a recency-adjusted effective count)."""
    return int(round(x)) if abs(x - round(x)) < 1e-9 else round(x, 1)


# ---------------------------------------------------------------------------
# Planning (Bayesian bandit / Thompson sampling)
# ---------------------------------------------------------------------------
def intervention_nights(iv_id, all_nights, metric, negate=False, category=None):
    """Nights that count for this intervention on the given metric, shaped for the
    bandit ({'hrv': y, 'taken': {iv_id: dose}}).

    Compliance rule (per the user): a night the intervention was assigned ON counts
    only if the checkbox was ticked; an un-ticked ON night is DROPPED (not a control).
    A night it was assigned OFF is always a control (we assume you skipped it).
    Returns (nights, n_on, n_off). For LRHR pass negate=True (lower is better ->
    negate so the engine's higher-is-better logic still applies).

    category: when given (one of store.CATEGORIES), nights the user flagged as
    excluded for that category are dropped entirely (neither on nor off). A cell the
    user excluded individually (e.g. assigned OFF but done anyway) is likewise
    dropped -- for this intervention only."""
    nights, n_on, n_off = [], 0.0, 0.0
    for n in all_nights:
        if category and n.get("excl_" + category):   # user excluded this night's data
            continue
        val = n.get(metric)
        if val is None:
            continue
        a = n["assigns"].get(iv_id)
        if a is None:
            continue
        if a.get("excluded"):   # cell flagged invalid (e.g. done despite assigned OFF)
            continue
        # recency weight (1.0 when the mode is off): the night's share of both the
        # likelihood and the effective on/off counts
        w = float(n.get("w", 1.0))
        assigned = a["assigned"] or 0
        taken = a["taken"]
        if assigned > 0:
            if taken is not None and taken > 0:  # ON and confirmed
                dose = float(assigned)
                n_on += w
            else:                                # ON but not confirmed -> drop
                continue
        else:                                    # OFF -> randomized control
            dose = 0.0
            n_off += w
        nights.append({"hrv": (-float(val) if negate else float(val)),
                       "taken": {iv_id: dose}, "w": w})
    return nights, n_on, n_off


def ensure_tonight(conn):
    """Make sure today's column exists and every active intervention has a cell.
    Each missing cell is filled by fitting that intervention's own model (on its
    compliant HRV nights) and drawing a Thompson-sampling choice. Additive:
    existing cells are left untouched so tonight's plan can't be re-rolled."""
    d = today_str()
    store.ensure_night(conn, d)
    night = store.get_night(conn, d)
    have = {a["intervention_id"] for a in night["assignments"]}
    missing = [iv for iv in store.list_interventions(conn, active_only=True)
               if iv["id"] not in have]
    if missing:
        all_nights = analysis_nights_weighted(conn)
        # Recency mode disables the early lock-in guard entirely: its night-count
        # tiers (15/30 on the smaller arm) are barely reachable under decayed
        # counts (an arm's effective count tops out near 1.44 x half-life x its
        # scheduling rate), so the clamp would rarely -- for a decisive
        # intervention, never -- release. Weighted mode already keeps the contrast
        # alive by design: old evidence fades, so P(helps) can't lock in for good.
        lockin_off = recency_enabled(conn)
        for iv in missing:
            if lockin_off:
                iv = dict(iv, no_lockin=True)
            nights, n_on, n_off = intervention_nights(iv["id"], all_nights, "hrv",
                                                      category=iv.get("category"))
            # doses actually recorded so far (with repeats) -- the points the dose-
            # response curve and the hill-climb are built from.
            seen = [n["taken"][iv["id"]] for n in nights if n["taken"].get(iv["id"], 0) > 0]
            # Scheduling uses the intervention's prior (if any): a believed-helpful
            # intervention is more likely to be assigned ON while data is thin. The
            # stored prior is a PROBABILITY; convert it to a prior mean on the effect.
            pri = bandit.scheduling_priors(iv)
            model = bandit.fit_model([iv], nights, priors=({iv["id"]: pri} if pri else None))
            plan = bandit.plan_night(model["post"], model["cols"], [iv],
                                     n_on={iv["id"]: n_on}, n_off={iv["id"]: n_off},
                                     measured={iv["id"]: seen})
            store.set_assignment(conn, d, iv["id"], plan.get(iv["id"], 0.0))
    return store.get_night(conn, d)


def reconcile_tonight_bounds(conn, iid, before=None):
    """After an intervention's dose settings change, bring TONIGHT's cell for it in
    line with the new settings -- but only if the cell is still pending: assigned ON,
    not ticked as taken, on a night that is still 'planned'. Past nights, OFF cells
    and anything already marked taken are never touched, and the on/off decision is
    never re-rolled. The dose is reset to the (clamped) new standard dose when the
    standard dose itself changed, or when the old dose falls outside the new
    [min_dose, max_dose]; otherwise it's left as planned. `before` is the
    intervention dict prior to the edit (if known). Returns the new dose or None."""
    iv = store.get_intervention(conn, iid)
    if not iv or iv.get("kind") != "dose":
        return None
    d = today_str()
    night = store.get_night(conn, d)
    if not night or night.get("status") != "planned":
        return None
    cell = next((a for a in night["assignments"] if a["intervention_id"] == iid), None)
    if not cell or (cell["taken_dose"] or 0) > 0 or not (cell["assigned_dose"] or 0) > 0:
        return None
    lo, hi = bandit._bounds(iv)
    old = float(cell["assigned_dose"])
    std_changed = before is not None and float(before.get("default_dose") or 0) != float(iv.get("default_dose") or 0)
    if bandit.is_open(old):
        # tonight is the open-ended arm: fine as long as that arm still exists
        if bandit.has_open_arm(iv):
            return None
    elif bandit._in_bounds(old, lo, hi) and not std_changed:
        return None
    new = bandit._clamp(float(iv.get("default_dose") or old), lo, hi)
    if float(new) == old:
        return None
    store._audit(conn, "clamp_assign", d, iid, old_dose=old, new_dose=float(new),
                 min_dose=lo, max_dose=hi,
                 reason=("open_arm_removed" if bandit.is_open(old) else
                         "standard_changed" if std_changed else "out_of_bounds"))
    store.force_assignment(conn, d, iid, new, taken_dose=cell["taken_dose"])
    return new

# ---------------------------------------------------------------------------
# Grid-shaped state
# ---------------------------------------------------------------------------
def public_scrub(payload):
    """What the read-only public view must not show: the written notes (per night
    and per intervention). Everything else is shown as-is."""
    for n in payload.get("nights", []) or []:
        if "note" in n:
            n["note"] = ""
    for iv in payload.get("interventions", []) or []:
        if "note" in iv:
            iv["note"] = ""
    return payload


def state_payload(conn):
    ensure_tonight(conn)
    d_today = today_str()
    ivs = store.list_interventions(conn)  # active first, then paused
    nights_rows = store.recent_nights(conn, GRID_NIGHTS)  # newest first, incl. today

    nights, cells = [], {}
    for n in nights_rows:
        nights.append({
            "date": n["date"],
            "hrv": n["hrv"],
            "lrhr": n["lrhr"],
            "tst": n["tst"],
            "lat": n["lat"],
            "eff": n["eff"],
            "hrv_src": n["hrv_src"],
            "lrhr_src": n["lrhr_src"],
            "tst_src": n["tst_src"],
            "lat_src": n["lat_src"],
            "eff_src": n["eff_src"],
            "status": n["status"],
            "note": n["note"],
            "excl_behavioral": bool(n["excl_behavioral"]),
            "excl_supplement": bool(n["excl_supplement"]),
            "excl_environmental": bool(n["excl_environmental"]),
            "is_today": n["date"] == d_today,
        })
        cmap = {}
        for a in n["assignments"]:
            assigned = a["assigned_dose"]
            taken = a["taken_dose"]
            cmap[a["intervention_id"]] = {
                "assigned_dose": assigned,
                "done": taken is not None and taken > 0 and assigned > 0,
                "excluded": bool(a["excluded"]),
                "input_value": a["input_value"],
            }
        cells[n["date"]] = cmap

    return {"today": d_today, "interventions": ivs, "nights": nights, "cells": cells,
            "recency": {"enabled": recency_enabled(conn),
                        "half_life_days": RECENCY_HALF_LIFE_DAYS}}


def _readout(iv, all_nights, metric, negate):
    """Fit this one intervention on its compliant nights for one metric and return
    its read-out (with on/off night counts). Empty dict if it has no valid nights."""
    nights, n_on, n_off = intervention_nights(iv["id"], all_nights, metric, negate,
                                              category=iv.get("category"))
    r = bandit.analyze([iv], nights)["readouts"].get(iv["id"], {}) if nights else {}
    r = dict(r)
    r["n_on"], r["n_off"] = _nn(n_on), _nn(n_off)
    # distinct non-zero doses actually tried -- you can't recommend a "best dose"
    # among doses you've never compared, so the UI gates the recommendation on this.
    r["n_doses_seen"] = len({round(n["taken"][iv["id"]], 6)
                             for n in nights if n["taken"].get(iv["id"], 0) > 0})
    # Raw (non-modeled) average of the metric on ON- vs OFF-nights, in native units,
    # recency-weighted when the mode is on. intervention_nights stores the value as
    # +val (HRV) or -val (LRHR, negated for the engine), so undo the negation here
    # to report the real HRV / resting-HR numbers.
    iid = iv["id"]

    def wmean(pairs):
        s = sum(w for w, _ in pairs)
        return (sum(w * v for w, v in pairs) / s) if s > 0 else None

    on_vals = [(n.get("w", 1.0), -n["hrv"] if negate else n["hrv"])
               for n in nights if n["taken"].get(iid, 0) > 0]
    off_vals = [(n.get("w", 1.0), -n["hrv"] if negate else n["hrv"])
                for n in nights if n["taken"].get(iid, 0) == 0]
    r["on_mean"] = wmean(on_vals)
    r["off_mean"] = wmean(off_vals)
    # Same raw averages per dose/option (an always-on choice has no off baseline,
    # so its report shows these instead of a modeled effect).
    by_dose = {}
    for n in nights:
        dose = n["taken"].get(iid, 0)
        if dose > 0:
            by_dose.setdefault(round(dose, 6), []).append(
                (n.get("w", 1.0), -n["hrv"] if negate else n["hrv"]))
    for row in r.get("doses", []):
        vals = by_dose.get(round(row["dose"], 6), [])
        row["raw_mean"] = wmean(vals)
        row["n_nights"] = _nn(sum(w for w, _ in vals))
    return r


def _p_scheduled(iv, all_nights):
    """Scheduling probability actually used (clamped). See _sched_info."""
    return _sched_info(iv, all_nights)["p_scheduled"]


def _sched_info(iv, all_nights, lockin_off=False):
    """Probability tonight's planner assigns this intervention ON. The on/off draw takes
    it iff a sampled 'does it help' effect > 0, so this is P(alpha > 0) under the
    PRIOR-INCLUSIVE posterior on the HRV nights (scheduling uses the prior). With no
    prior it equals the measured P(helps); with a prior it starts higher and asymptotes
    to the measured value as nights accumulate. A choice with no 'skip' arm is always
    scheduled. Kept SEPARATE from the efficacy read-outs, which stay prior-free.
    While the smaller arm is thin the value is clamped by tier (bandit.LOCKIN_TIERS:
    <15 nights -> [10%, 90%], <30 -> [5%, 95%]), exactly as the planner does -- unless
    the intervention's no_lockin override is set, or lockin_off says the guard is
    globally disabled (recency mode: decayed counts can't reach the tiers, and
    fading evidence keeps the contrast alive anyway), in which case the raw value
    is used and no tier is reported.
    Returns {p_scheduled (clamped), p_scheduled_raw, sched_cap (the tier's cap if the
    clamp changed the value, else None), sched_tier (the tier cap in force for these
    arm sizes whether or not it bit -- None once both arms are big enough),
    sched_n_on, sched_n_off}."""
    if iv.get("kind") in ("choice", "choice_dose") and not iv.get("test_skip"):
        return {"p_scheduled": 1.0, "p_scheduled_raw": 1.0, "sched_cap": None,
                "sched_tier": None, "sched_n_on": None, "sched_n_off": None}
    nights, n_on, n_off = intervention_nights(iv["id"], all_nights, "hrv", category=iv.get("category"))
    pri = bandit.scheduling_priors(iv)
    res = bandit.analyze([iv], nights, priors=({iv["id"]: pri} if pri else None))
    raw = res["readouts"].get(iv["id"], {}).get("p_helps")
    tier = None if lockin_off else bandit.lockin_cap(n_on, n_off)
    if lockin_off or iv.get("no_lockin"):
        p, capped = raw, False
    else:
        p = bandit.clamp_p_on(raw, n_on, n_off)
        capped = raw is not None and p != raw
    return {"p_scheduled": p, "p_scheduled_raw": raw,
            "sched_cap": tier if capped else None, "sched_tier": tier,
            "sched_n_on": _nn(n_on), "sched_n_off": _nn(n_off)}


# ---------------------------------------------------------------------------
# HRV readiness trend (training guidance, separate from the bandit)
# ---------------------------------------------------------------------------
# Nightly RMSSD is log-transformed (x = ln HRV) because its night-to-night wobble
# is right-skewed; in log space it's roughly symmetric. The training band is
# mu +/- 0.5*sigma of x over a rolling window -- the 0.5 is Hopkins' "smallest
# worthwhile change" convention (what the trials used, not derivable). Decision
# rule: 7-day rolling mean of x at/above the lower bound -> intervals; below ->
# Z2 or rest. All numbers are exponentiated back to ms before leaving the server.
HRV_BAND_COEF = 0.5        # Hopkins' SWC coefficient on the log-SD
HRV_BASELINE_DAYS = 60     # band is refit over this rolling window (tracks fitness)
HRV_BASELINE_MIN = 28      # ~4 weeks of nights before the band is shown at all
HRV_AVG_DAYS = 7           # the readiness signal: rolling mean of ln(HRV)
HRV_AVG_MIN = 3            # nights required inside the 7-day window
HRV_DEFAULT_DAYS = 60      # the card opens on this many days; a toggle shows it all
HRV_HISTORY_START = "2025-01-01"   # where the "all" view begins; nights backfilled
                                   # from before this only feed the rolling windows


def hrv_trend_payload(conn):
    """Per-day series for the readiness card: nightly HRV, 7-day rolling mean, and
    the rolling mu +/- 0.5*sigma band, all in ms. None until any HRV exists."""
    x_by_date = {d: math.log(v) for d, v in store.hrv_series(conn) if v and v > 0}
    if not x_by_date:
        return None
    hrv_by_date = {d: round(math.exp(x), 1) for d, x in x_by_date.items()}
    end = date.fromisoformat(max(x_by_date))
    start = max(date.fromisoformat(HRV_HISTORY_START), date.fromisoformat(min(x_by_date)))

    def window(day, n_days):
        """ln-HRV values recorded in the n_days ending on (and including) day."""
        return [x_by_date[k] for j in range(n_days)
                if (k := (day - timedelta(days=j)).isoformat()) in x_by_date]

    days, latest = [], None
    for i in range((end - start).days + 1):
        day = start + timedelta(days=i)
        iso = day.isoformat()
        row = {"date": iso, "hrv": hrv_by_date.get(iso),
               "avg7": None, "lo": None, "mid": None, "hi": None}
        w7 = window(day, HRV_AVG_DAYS)
        if len(w7) >= HRV_AVG_MIN:
            row["avg7"] = round(math.exp(statistics.mean(w7)), 1)
        base = window(day, HRV_BASELINE_DAYS)
        if len(base) >= HRV_BASELINE_MIN:
            mu, sd = statistics.mean(base), statistics.stdev(base)
            row["lo"] = round(math.exp(mu - HRV_BAND_COEF * sd), 1)
            row["mid"] = round(math.exp(mu), 1)
            row["hi"] = round(math.exp(mu + HRV_BAND_COEF * sd), 1)
        if row["avg7"] is not None and row["lo"] is not None:
            latest = {"date": iso, "avg7": row["avg7"], "mid": row["mid"], "lo": row["lo"],
                      "status": "intervals" if row["avg7"] >= row["lo"] else "recover"}
        days.append(row)
    return {"days": days, "latest": latest, "n_nights": len(x_by_date),
            "min_nights": HRV_BASELINE_MIN, "window_days": HRV_BASELINE_DAYS,
            "avg_days": HRV_AVG_DAYS, "default_days": HRV_DEFAULT_DAYS,
            "app_start": store.first_assigned_night(conn)}


def reports_payload(conn):
    """Per-intervention efficacy read-outs for BOTH metrics (HRV: higher better;
    LRHR: lower better). Each intervention is fit on its own compliant nights.

    Efficacy read-outs are PRIOR-FREE (purely measured). Separately, p_scheduled is the
    prior-INCLUSIVE probability the planner schedules the intervention tonight."""
    ivs = store.list_interventions(conn)
    all_nights = analysis_nights_weighted(conn)
    recency_on = recency_enabled(conn)   # recency mode also disables the lock-in guard
    n_hrv = sum(1 for n in all_nights if n["hrv"] is not None)
    out = []
    for iv in ivs:
        out.append({
            **iv,
            "readout": _readout(iv, all_nights, "hrv", negate=False),
            "readout_lrhr": _readout(iv, all_nights, "lrhr", negate=True),
            "readout_tst": _readout(iv, all_nights, "tst", negate=False),
            "readout_lat": _readout(iv, all_nights, "lat", negate=True),
            "readout_eff": _readout(iv, all_nights, "eff", negate=False),
            **_sched_info(iv, all_nights, lockin_off=recency_on),
        })
    return {"n_completed": n_hrv, "interventions": out,
            "hrv_trend": hrv_trend_payload(conn),
            "recency": {"enabled": recency_enabled(conn),
                        "half_life_days": RECENCY_HALF_LIFE_DAYS}}


# ---------------------------------------------------------------------------
# Pairwise interactions (Reports > Interactions)
# ---------------------------------------------------------------------------
def _on_state(night, iv_id):
    """1 = confirmed on, 0 = assigned off (control), None = undetermined (not assigned
    that night, or assigned on but never ticked -- dropped, same rule as elsewhere)."""
    a = night["assigns"].get(iv_id)
    if a is None or a.get("excluded"):
        return None
    if (a["assigned"] or 0) > 0:
        return 1 if (a["taken"] is not None and a["taken"] > 0) else None
    return 0


def _pair_nights(iv_a, iv_b, all_nights, metric, negate):
    """Nights where BOTH interventions are determined (on or off) for this metric,
    honoring each one's category exclusion. Shaped for bandit.pair_interaction."""
    out = []
    for n in all_nights:
        val = n.get(metric)
        if val is None:
            continue
        if n.get("excl_" + (iv_a.get("category") or "")) or n.get("excl_" + (iv_b.get("category") or "")):
            continue
        sa, sb = _on_state(n, iv_a["id"]), _on_state(n, iv_b["id"])
        if sa is None or sb is None:
            continue
        out.append({"y": (-float(val) if negate else float(val)), "a": sa, "b": sb,
                    "w": float(n.get("w", 1.0))})
    return out


def interactions_payload(conn):
    """Every pair of (non-archived) interventions that has shared at least one night
    with both on, with its interaction read-out on HRV (primary) and resting HR.
    Pairs that have never co-occurred are only counted."""
    ivs = store.list_interventions(conn)
    all_nights = analysis_nights_weighted(conn)
    n_hrv = sum(1 for n in all_nights if n["hrv"] is not None)
    pairs, no_overlap = [], 0
    for i in range(len(ivs)):
        for j in range(i + 1, len(ivs)):
            a, b = ivs[i], ivs[j]
            r_hrv = bandit.pair_interaction(_pair_nights(a, b, all_nights, "hrv", False))
            r_lrhr = bandit.pair_interaction(_pair_nights(a, b, all_nights, "lrhr", True))
            if not ((r_hrv and r_hrv["n_both"]) or (r_lrhr and r_lrhr["n_both"])):
                no_overlap += 1
                continue
            brief = lambda iv: {"id": iv["id"], "name": iv["name"], "category": iv["category"],
                                "kind": iv["kind"], "active": iv["active"]}
            pairs.append({"a": brief(a), "b": brief(b), "hrv": r_hrv, "lrhr": r_lrhr})
    return {"n_completed": n_hrv, "n_interventions": len(ivs), "pairs": pairs,
            "n_pairs_no_overlap": no_overlap,
            "recency": {"enabled": recency_enabled(conn),
                        "half_life_days": RECENCY_HALF_LIFE_DAYS}}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "SleepBandit/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- hosted-mode helpers -------------------------------------------------
    def _client_ip(self):
        # behind the reverse proxy (Caddy) the real client is in X-Forwarded-For
        xff = self.headers.get("X-Forwarded-For", "")
        return (xff.split(",")[0].strip() if xff else self.client_address[0])

    def _session_ok(self):
        if not PUBLIC_MODE:
            return True
        tok = auth.cookie_from_header(self.headers.get("Cookie"))
        if not tok:
            return False
        conn = store.connect()
        try:
            return auth.check_token(auth.session_secret(conn), tok)
        finally:
            conn.close()

    def _can_write(self):
        return (not PUBLIC_MODE) or self._session_ok()

    def _forbid(self):
        return self._send_json({"error": "read-only"}, 403)

    def _send_page(self, mode):
        """index.html with the runtime config injected: mode is 'local' (your own
        machine), 'public' (read-only), 'login' (admin path, not signed in) or 'admin'."""
        try:
            with open(os.path.join(PUBLIC_DIR, "index.html"), "rb") as f:
                html = f.read().decode("utf-8")
        except FileNotFoundError:
            return self.send_error(404)
        base = (ADMIN_PATH if mode in ("login", "admin") else "/")
        cfg = {"mode": mode, "base": base, "hosted": PUBLIC_MODE,
               "redirect_uri": oura.redirect_uri(PORT, BASE_URL)}
        html = html.replace("<!--SB_CONFIG-->", "<script>window.SB=" + json.dumps(cfg) + "</script>")
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _set_session_cookie(self, token, clear=False):
        secure = "; Secure" if (BASE_URL or "").startswith("https") else ""
        if clear:
            val = f"{auth.COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}"
        else:
            val = (f"{auth.COOKIE_NAME}={token}; Path=/; Max-Age={auth.SESSION_DAYS * 86400}; "
                   f"HttpOnly; SameSite=Strict{secure}")
        self.send_header("Set-Cookie", val)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send_page("public" if PUBLIC_MODE else "local")
        if PUBLIC_MODE and path == ADMIN_PATH:
            return self._send_page("admin" if self._session_ok() else "login")
        statics = {
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "application/javascript; charset=utf-8"),
        }
        if path in statics:
            name, ctype = statics[path]
            return self._send_file(os.path.join(PUBLIC_DIR, name), ctype)

        writer = self._can_write()          # hosted: only a signed-in admin
        scrub = (lambda p: p) if writer else public_scrub
        conn = store.connect()
        try:
            if path == "/api/state":
                return self._send_json(scrub(state_payload(conn)))
            if path == "/api/reports":
                return self._send_json(scrub(reports_payload(conn)))
            if path == "/api/interactions":
                return self._send_json(scrub(interactions_payload(conn)))
            if path.startswith("/api/") or path.startswith("/oura/"):
                if not writer:
                    return self._forbid()
            if path == "/api/export":
                return self._send_json({
                    "interventions": store.list_interventions(conn),
                    "nights": store.recent_nights(conn, 100000),
                })
            if path == "/api/oura/status":
                return self._send_json(oura.status(conn))
            if path == "/oura/connect":
                # start the OAuth dance: remember a CSRF state, hand off to Oura
                client_id = store.get_setting(conn, "oura_client_id")
                if not client_id:
                    return self._send_json({"error": "Save your Oura client ID and secret first."}, 400)
                state = secrets.token_urlsafe(16)
                store.set_setting(conn, "oura_state", state)
                return self._redirect(oura.authorize_url(client_id, state, PORT, BASE_URL))
            if path == "/oura/callback":
                home = (ADMIN_PATH if PUBLIC_MODE else "/")
                q = parse_qs(urlparse(self.path).query)
                want = store.get_setting(conn, "oura_state")
                store.set_setting(conn, "oura_state", None)
                if q.get("error"):
                    return self._redirect(home + "#oura=denied")
                if not want or q.get("state", [None])[0] != want or not q.get("code"):
                    return self._redirect(home + "#oura=error")
                try:
                    oura.exchange_code(conn, q["code"][0], PORT, BASE_URL)
                except oura.OuraError:
                    return self._redirect(home + "#oura=error")
                store._audit(conn, "oura_connect")
                conn.commit()
                return self._redirect(home + "#oura=connected")
        finally:
            conn.close()
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        if PUBLIC_MODE and path == "/api/login":
            ip = self._client_ip()
            if not auth.login_allowed(ip):
                return self._send_json({"error": "Too many attempts — try again in a few minutes."}, 429)
            ok = bool(PASSWORD_HASH) and auth.verify_password(str(body.get("password", "")), PASSWORD_HASH)
            auth.note_login(ip, ok)
            if not ok:
                time.sleep(0.5)      # blunt, cheap slowdown for guessing
                return self._send_json({"error": "Wrong password."}, 401)
            conn = store.connect()
            try:
                token = auth.make_token(auth.session_secret(conn))
                store._audit(conn, "admin_login")
                conn.commit()
            finally:
                conn.close()
            payload = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._set_session_cookie(token)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        if PUBLIC_MODE and path == "/api/logout":
            payload = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._set_session_cookie("", clear=True)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            return self.wfile.write(payload)
        if not self._can_write():
            return self._forbid()
        conn = store.connect()
        try:
            if path == "/api/interventions":
                store.create_intervention(
                    conn,
                    name=body.get("name", "").strip() or "Untitled",
                    unit=body.get("unit", ""),
                    default_dose=body.get("default_dose", 1) or 1,
                    unit_dose=body.get("unit_dose", 0) or 0,
                    dose_levels=_clean_levels(body.get("dose_levels")),
                    note=body.get("note", ""),
                    kind=body.get("kind", "dose"),
                    options=body.get("options"),
                    category=body.get("category", "supplement"),
                    dose_noun=body.get("dose_noun", ""),
                    explore_doses=body.get("explore_doses", True),
                    delay_explore=body.get("delay_explore", True),
                    open_arm=body.get("open_arm", False),
                    open_arm_label=body.get("open_arm_label", ""),
                    test_skip=body.get("test_skip", False),
                    prior_helps=body.get("prior_helps", 0) or 0,
                    min_dose=body.get("min_dose"),
                    max_dose=body.get("max_dose"),
                    noun_amount=body.get("noun_amount"),
                    input_type=body.get("input_type", "checkbox"),
                    input_options=body.get("input_options"),
                    input_label=body.get("input_label", ""),
                )
                ensure_tonight(conn)
                return self._send_json(state_payload(conn), 201)

            if path == "/api/cell":
                iid = int(body["intervention_id"])
                # completion detail for non-checkbox input types, validated against
                # the intervention's settings: radio -> one of its option ids;
                # quantity -> a number as typed, or '-' (done but not measured).
                # Anything else is dropped (the tick itself still goes through).
                val = body.get("input_value")
                if val is not None:
                    val = str(val).strip()
                    iv = store.get_intervention(conn, iid) or {}
                    if iv.get("input_type") == "radio":
                        if not any(str(o["id"]) == val for o in iv.get("input_options", [])):
                            val = None
                    elif iv.get("input_type") == "quantity":
                        if val != "-":
                            try:
                                float(val)
                            except ValueError:
                                val = None
                    else:
                        val = None
                store.set_taken(conn, body["date"], iid, bool(body.get("done")),
                                input_value=val)
                return self._send_json(state_payload(conn))

            if path == "/api/metric":
                store.set_metric(conn, body["date"], body.get("metric", "hrv"),
                                 body.get("value"))
                return self._send_json(state_payload(conn))

            if path == "/api/note":
                store.set_note(conn, body["date"], body.get("note", ""))
                return self._send_json(state_payload(conn))

            if path == "/api/exclude":
                store.set_exclusion(conn, body["date"], body.get("category", ""),
                                    bool(body.get("excluded")))
                return self._send_json(state_payload(conn))

            if path == "/api/exclude_cell":
                store.set_cell_exclusion(conn, body["date"],
                                         int(body["intervention_id"]),
                                         bool(body.get("excluded")))
                return self._send_json(state_payload(conn))

            if path == "/api/recency":
                # server-wide mode: recency weighting drives the Reports numbers
                # AND the live bandit, so it lives in the DB, not the browser
                on = bool(body.get("enabled"))
                store.set_setting(conn, "recency_weighting", "1" if on else None)
                store._audit(conn, "set_recency", enabled=on)
                conn.commit()
                return self._send_json(reports_payload(conn))

            if path == "/api/oura/config":
                store.set_setting(conn, "oura_client_id", (body.get("client_id") or "").strip() or None)
                secret = (body.get("client_secret") or "").strip()
                if secret:   # blank means "keep the stored secret"
                    store.set_setting(conn, "oura_client_secret", secret)
                return self._send_json(oura.status(conn))

            if path == "/api/oura/sync":
                return self._send_json(oura.sync(conn, today_str(),
                                                 force=bool(body.get("force"))))

            if path == "/api/oura/disconnect":
                for k in ("oura_access_token", "oura_refresh_token", "oura_expires_at",
                          "oura_state"):
                    store.set_setting(conn, k, None)
                store._audit(conn, "oura_disconnect")
                conn.commit()
                return self._send_json(oura.status(conn))
        finally:
            conn.close()
        self.send_error(404)

    def do_PUT(self):
        path = urlparse(self.path).path
        body = self._read_body()
        if not self._can_write():
            return self._forbid()
        conn = store.connect()
        try:
            if path.startswith("/api/interventions/"):
                iid = int(path.rsplit("/", 1)[-1])
                fields = {k: body[k] for k in
                          ("name", "unit", "default_dose", "unit_dose", "dose_noun", "note",
                           "active", "kind", "options", "category", "explore_doses", "delay_explore",
                           "open_arm", "open_arm_label", "test_skip",
                           "prior_helps", "min_dose", "max_dose", "noun_amount", "no_lockin",
                           "input_type", "input_options", "input_label")
                          if k in body}
                if "dose_levels" in body:
                    fields["dose_levels"] = _clean_levels(body["dose_levels"])
                before = store.get_intervention(conn, iid)
                store.update_intervention(conn, iid, **fields)
                # Pausing only stops FUTURE suggestions; a night that's already been
                # planned is never re-rolled. The one exception: if the standard dose
                # or min/max bounds changed and tonight's ON cell is still pending,
                # its dose follows the new settings so the plan can't show a stale or
                # impossible dose.
                reconcile_tonight_bounds(conn, iid, before)
                ensure_tonight(conn)
                return self._send_json(state_payload(conn))
        finally:
            conn.close()
        self.send_error(404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if not self._can_write():
            return self._forbid()
        conn = store.connect()
        try:
            if path.startswith("/api/interventions/"):
                store.delete_intervention(conn, int(path.rsplit("/", 1)[-1]))
                return self._send_json(state_payload(conn))
        finally:
            conn.close()
        self.send_error(404)


def _clean_levels(levels):
    if not levels:
        return None
    out = []
    for x in levels:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if v > 0:
            out.append(int(v) if v.is_integer() else v)
    return sorted(set(out)) or None


def _autosync_loop():
    """Hosted mode has no 'someone opened the app' moment to plan tonight and pull
    Oura's numbers, so do both on a timer. Every pass: create tonight's plan if it
    isn't there yet, then (if Oura is connected) sync. Errors are logged, never fatal."""
    while True:
        try:
            conn = store.connect()
            try:
                ensure_tonight(conn)
                if oura.status(conn).get("connected"):
                    oura.sync(conn, today_str())
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()
        time.sleep(AUTOSYNC_MINUTES * 60)


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    if PUBLIC_MODE:
        if not PASSWORD_HASH:
            print("WARNING: SB_PUBLIC=1 but SB_PASSWORD_HASH is unset -- the admin page cannot be used.")
        print(f"Sleep Bandit (hosted, read-only root; admin at {ADMIN_PATH}) on {HOST}:{PORT}")
    else:
        print("Sleep Bandit is running.")
        print(f"  Open http://localhost:{PORT} in your browser.")
        print("  Press Ctrl+C to stop.")
    if AUTOSYNC:
        threading.Thread(target=_autosync_loop, name="autosync", daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
