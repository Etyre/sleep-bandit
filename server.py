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
import subprocess
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import auth
import bandit
import oura
import tags
import weather
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


def _code_version():
    """Identify the code that is running, for the planning audit trail: the git
    commit (short hash) plus "+dirty" if tracked source files differ from it (the
    database file is ignored -- it always differs). "unknown" outside a checkout."""
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        run = lambda *a: subprocess.run(["git", *a], cwd=here, capture_output=True,
                                        text=True, timeout=5)
        head = run("rev-parse", "--short", "HEAD")
        if head.returncode != 0:
            return "unknown"
        dirty = run("status", "--porcelain", "--", ".", ":!data").stdout.strip()
        return head.stdout.strip() + ("+dirty" if dirty else "")
    except Exception:
        return "unknown"


CODE_VERSION = _code_version()   # fixed for the life of the process (restart to pick up code)


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

# ---------------------------------------------------------------------------
# Baseline-drift adjustment (server-wide mode, toggled from the header switch).
# When on, every night gets, per metric, a local baseline: the mean of that
# metric over the nights within BASELINE_WINDOW_DAYS either side (the night
# itself excluded; backfilled Oura history counts, so the first experiment
# week has one too). The model takes it as a covariate (bandit.BASE_PRIOR_VAR
# explains why), the Reports averages are shown baseline-adjusted, and the
# nightly planner schedules from the same fit. A night with fewer than
# BASELINE_MIN_NIGHTS neighbours gets no baseline and sits at the centre.
# Setting key: baseline_adjust ("1" = on). Default off.
# ---------------------------------------------------------------------------
BASELINE_WINDOW_DAYS = 7
BASELINE_MIN_NIGHTS = 4
BASELINE_METRICS = ("hrv", "lrhr", "tst", "lat", "eff")


def recency_enabled(conn):
    return store.get_setting(conn, "recency_weighting") == "1"


def count_maximize_nights(conn):
    """Whether maximize-benefit nights (not randomized) feed the effect estimates.
    Default OFF (they are not evidence); the header switch (POST /api/count_maximize)
    turns counting on."""
    return store.get_setting(conn, "count_maximize_nights") == "1"


def baseline_enabled(conn):
    """Whether the local-baseline covariate is in force (see BASELINE_WINDOW_DAYS)."""
    return store.get_setting(conn, "baseline_adjust") == "1"


def analysis_modes(conn):
    """The server-wide analysis switches, as every payload reports them."""
    return {"recency": {"enabled": recency_enabled(conn),
                        "half_life_days": RECENCY_HALF_LIFE_DAYS},
            "count_maximize": count_maximize_nights(conn),
            "baseline": {"enabled": baseline_enabled(conn),
                         "window_days": BASELINE_WINDOW_DAYS,
                         "min_nights": BASELINE_MIN_NIGHTS}}


def attach_baselines(nights, series):
    """Give every analysis night a per-metric local baseline, n["base_<metric>"]:
    the mean of that metric over the recorded nights within BASELINE_WINDOW_DAYS
    either side, excluding the night itself; None with fewer than
    BASELINE_MIN_NIGHTS neighbours. series is store.metric_series (all recorded
    nights, so maximize nights and backfilled history count as neighbours even
    when they are not evidence). Mutates and returns the list."""
    import bisect
    by_metric = {}
    for m in BASELINE_METRICS:
        pts = sorted((d, float(v[m])) for d, v in series.items() if v.get(m) is not None)
        by_metric[m] = ([d for d, _ in pts], [v for _, v in pts])
    span = timedelta(days=BASELINE_WINDOW_DAYS)
    for n in nights:
        d0 = date.fromisoformat(n["date"])
        lo, hi = (d0 - span).isoformat(), (d0 + span).isoformat()
        for m in BASELINE_METRICS:
            dates, vals = by_metric[m]
            i, j = bisect.bisect_left(dates, lo), bisect.bisect_right(dates, hi)
            nb = [v for d, v in zip(dates[i:j], vals[i:j]) if d != n["date"]]
            n["base_" + m] = (sum(nb) / len(nb)) if len(nb) >= BASELINE_MIN_NIGHTS else None
    return nights


def _baseline_adjusted(triples):
    """[(w, value, baseline-or-None)] -> [(w, adjusted value)] in the same units:
    value - baseline + (weighted mean baseline), so the figures stay on the
    metric's own scale (an average HRV, not a deviation). Nights without a
    baseline are left as they are (= at the mean). Identity when no night has one."""
    have = [(w, b) for w, _, b in triples if b is not None]
    if not have:
        return [(w, v) for w, v, _ in triples]
    c = sum(w * b for w, b in have) / sum(w for w, _ in have)
    return [(w, (v - b + c) if b is not None else v) for w, v, b in triples]


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
    nights = store.analysis_nights(conn, count_maximize=count_maximize_nights(conn))
    if baseline_enabled(conn):
        attach_baselines(nights, store.metric_series(conn))
    return apply_recency(nights) if recency_enabled(conn) else nights


def _nn(x):
    """Display-friendly night count: exact int when integral (the unweighted
    case), else one decimal (a recency-adjusted effective count)."""
    return int(round(x)) if abs(x - round(x)) < 1e-9 else round(x, 1)


# ---------------------------------------------------------------------------
# Planning (Bayesian bandit / Thompson sampling)
# ---------------------------------------------------------------------------
def _n_experiment_nights(all_nights):
    """Nights that count as "logged" in the report headers: an HRV reading AND at
    least one intervention cell. Backfilled history (Oura nights imported from
    before the experiment, kept only to define the HRV baseline band) has readings
    but no cells, so it is excluded here just as it is from every fit."""
    return sum(1 for n in all_nights if n["hrv"] is not None and n.get("assigns"))


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
    dropped -- for this intervention only.

    In baseline-drift mode each night also carries "base", its local baseline for
    this metric (negated like the value when negate is set), or None if it has
    none; the engine takes it as a covariate (bandit.BASE_PRIOR_VAR)."""
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
        row = {"hrv": (-float(val) if negate else float(val)),
               "taken": {iv_id: dose}, "w": w}
        if ("base_" + metric) in n:
            b = n["base_" + metric]
            row["base"] = None if b is None else (-b if negate else b)
        nights.append(row)
    return nights, n_on, n_off


def _mode_flags(conn):
    """The analysis switches as a flat dict, for the planning audit trail."""
    return {"recency": recency_enabled(conn), "count_maximize": count_maximize_nights(conn),
            "baseline": baseline_enabled(conn)}


def _plan_context(conn_modes, iv, model, n_on, n_off, lockin_off, pri):
    """Provenance recorded with every planned cell (audit_log detail, op "assign"):
    which code and which switches planned it, the scheduling probability the
    Thompson draw was effectively made at (p_on_raw = P(helps) under the
    prior-inclusive fit; p_on = after the lock-in cap, exactly as _sched_info
    reports it), the cap and effective on/off counts in force, the prior used, and
    the fitted baseline coefficient (None when the baseline switch is off). The
    assignment itself stays frozen; this only annotates the audit row."""
    always_on = iv.get("kind") in ("choice", "choice_dose") and not iv.get("test_skip")
    if always_on:
        p_raw = p_used = 1.0
        cap = None
    else:
        p_raw = bandit.p_on_estimate(model["post"], model["cols"][iv["id"]]["on"])
        no_guard = lockin_off or bool(iv.get("no_lockin"))
        cap = None if no_guard else bandit.lockin_cap(n_on, n_off)
        p_used = p_raw if cap is None else min(p_raw, cap)
    return {"code": CODE_VERSION, "modes": conn_modes,
            "p_on_raw": round(p_raw, 4), "p_on": round(p_used, 4), "cap": cap,
            "no_lockin": bool(iv.get("no_lockin")) or bool(lockin_off),
            "n_on": _nn(n_on), "n_off": _nn(n_off), "prior": pri or None,
            "base_coef": (round(model["base_coef"], 4) if model.get("base_coef") is not None else None)}


def ensure_tonight(conn):
    """Make sure today's column exists and every active intervention has a cell.
    Each missing cell is filled by fitting that intervention's own model (on its
    compliant HRV nights) and drawing a Thompson-sampling choice. Additive:
    existing cells are left untouched so tonight's plan can't be re-rolled.
    Every cell's audit row carries its planning provenance (_plan_context)."""
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
        modes = _mode_flags(conn)
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
            ctx = _plan_context(modes, iv, model, n_on, n_off, lockin_off, pri)
            store.set_assignment(conn, d, iv["id"], plan.get(iv["id"], 0.0), context=ctx)
    return store.get_night(conn, d)


def maximize_tonight(conn, mode):
    """MAXIMIZE BENEFIT for tonight: no exploration, no randomization -- every active
    intervention found to help (posterior-mean effect > 0, prior-free like the
    Reports page) at its best-measured dose or option, everything else skipped.
    mode: "preview" returns the plan; "on" overrides tonight's unticked cells with
    it (each cell's randomized plan is parked in random_dose) and flags the night;
    "off" puts the parked randomized plan back exactly -- never a re-roll -- and
    clears the flag. Cells already ticked as done are left alone either way. This
    is the one sanctioned, audit-logged exception to the assignment freeze.
    A maximize night is not randomized; whether it feeds the effect estimates (and
    so this plan) is the count_maximize_nights setting."""
    ensure_tonight(conn)
    d = today_str()
    night = store.get_night(conn, d)
    cur = {a["intervention_id"]: a for a in night["assignments"]}
    if mode == "off":
        for iid in cur:
            store.restore_assignment(conn, d, iid)
        store.set_night_maximize(conn, d, False)
        return {"date": d, "plan": []}
    all_nights = analysis_nights_weighted(conn)
    modes = _mode_flags(conn)
    rows = []
    for iv in store.list_interventions(conn, active_only=True):
        nights, n_on, n_off = intervention_nights(iv["id"], all_nights, "hrv", category=iv.get("category"))
        seen = [n["taken"][iv["id"]] for n in nights if n["taken"].get(iv["id"], 0) > 0]
        model = bandit.fit_model([iv], nights)
        ex = bandit.plan_exploit(model["post"], model["cols"], [iv], measured={iv["id"]: seen})[iv["id"]]
        a = cur.get(iv["id"]) or {}
        rows.append({"id": iv["id"], "name": iv["name"], "kind": iv["kind"],
                     "dose": ex["dose"], "effect": ex["effect"], "on_effect": ex["on_effect"],
                     "current": a.get("assigned_dose"), "ticked": a.get("taken_dose") is not None})
        if mode == "on":
            # provenance like a planned cell, plus the exploit read-out it came from
            ctx = {"code": CODE_VERSION, "modes": modes, "n_on": _nn(n_on), "n_off": _nn(n_off),
                   "on_effect": round(ex["on_effect"], 4), "effect": round(ex["effect"], 4),
                   "base_coef": (round(model["base_coef"], 4) if model.get("base_coef") is not None else None)}
            store.override_assignment(conn, d, iv["id"], ex["dose"], context=ctx)
    if mode == "on":
        store.set_night_maximize(conn, d, True)
    return {"date": d, "plan": rows}


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


def reconcile_tonight_options(conn, iid):
    """After a multiple-choice intervention's options change, drop TONIGHT's cell
    for it if the cell prescribes an option that no longer exists (removed /
    retired) -- but only while the cell is still pending: not ticked as taken, on a
    night still 'planned'. The caller's ensure_tonight then plans the cell afresh
    from the options that remain. This is the one case where a planned cell is
    re-rolled: the prescribed option can't be done any more. Returns True if the
    cell was dropped."""
    iv = store.get_intervention(conn, iid)
    if not iv or iv.get("kind") not in ("choice", "choice_dose"):
        return False
    d = today_str()
    night = store.get_night(conn, d)
    if not night or night.get("status") != "planned":
        return False
    cell = next((a for a in night["assignments"] if a["intervention_id"] == iid), None)
    if not cell or cell["taken_dose"] is not None or not (cell["assigned_dose"] or 0) > 0:
        return False
    level = float(cell["assigned_dose"])
    oid = bandit.combo_decode(level)[0] if iv["kind"] == "choice_dose" else int(round(level))
    if any(int(o["id"]) == oid for o in bandit.live_options(iv)):
        return False
    store._audit(conn, "replan_assign", d, iid, old_dose=level, reason="option_removed")
    return store.delete_assignment(conn, d, iid)

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
    tags_by_date = store.night_tags(conn)
    vocab_rank = {v["slug"]: i for i, v in enumerate(store.tag_vocab(conn))}

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
            "maximize": bool(n["maximize"]),
            "is_today": n["date"] == d_today,
            "tags": _vocab_order((tags_by_date.get(n["date"]) or {}).get("tags", []), vocab_rank),
            "tags_summary": (tags_by_date.get(n["date"]) or {}).get("summary", ""),
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
            "tags": tags_info(conn), **analysis_modes(conn)}


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
    # to report the real HRV / resting-HR numbers. In baseline-drift mode the
    # averages are baseline-adjusted (value - its local baseline + the mean
    # baseline), still on the metric's own scale.
    iid = iv["id"]

    def wmean(pairs):
        s = sum(w for w, _ in pairs)
        return (sum(w * v for w, v in pairs) / s) if s > 0 else None

    sgn = -1.0 if negate else 1.0
    adj = _baseline_adjusted([(n.get("w", 1.0), sgn * n["hrv"],
                               (None if n.get("base") is None else sgn * n["base"]))
                              for n in nights])
    doses = [n["taken"].get(iid, 0) for n in nights]
    on_vals = [wv for wv, d in zip(adj, doses) if d > 0]
    off_vals = [wv for wv, d in zip(adj, doses) if d == 0]
    r["on_mean"] = wmean(on_vals)
    r["off_mean"] = wmean(off_vals)
    # Same raw averages per dose/option (an always-on choice has no off baseline,
    # so its report shows these instead of a modeled effect).
    by_dose = {}
    for wv, dose in zip(adj, doses):
        if dose > 0:
            by_dose.setdefault(round(dose, 6), []).append(wv)
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
    While the smaller arm is thin the value is capped by tier (bandit.LOCKIN_TIERS:
    <15 nights -> at most 90%, <30 -> at most 95%; no floor on the low side), exactly
    as the planner does -- unless
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


def _vocab_order(slugs, rank):
    """Tags always listed in vocabulary order (the order tags were first defined),
    so the same tag sits in the same place on every night and every card."""
    return sorted(slugs, key=lambda s: (rank.get(s, len(rank)), s))


# ---------------------------------------------------------------------------
# Setting vs. outdoor temperature (temperature-dosed interventions: Chili Pad, AC)
# ---------------------------------------------------------------------------
SEASON_MIN_POINTS = 6      # fewer nights with weather than this: no section
SEASON_MIN_CELL = 2        # a bin's best setting must have at least this many nights


def is_temperature_dosed(iv):
    return iv.get("kind") == "dose" and "°" in (iv.get("unit") or "")


def _solve(A, b):
    """Gaussian elimination with partial pivoting; None if singular."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        if abs(M[piv][c]) < 1e-12:
            return None
        M[c], M[piv] = M[piv], M[c]
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] for i in range(n)]


def _ols(X, y):
    """Plain least squares: (coefs, standard errors, df) or None."""
    p = len(X[0]); n = len(y)
    if n <= p + 1:
        return None
    XtX = [[sum(X[k][i] * X[k][j] for k in range(n)) for j in range(p)] for i in range(p)]
    Xty = [sum(X[k][i] * y[k] for k in range(n)) for i in range(p)]
    beta = _solve(XtX, Xty)
    if beta is None:
        return None
    resid = [y[k] - sum(X[k][i] * beta[i] for i in range(p)) for k in range(n)]
    s2 = sum(r * r for r in resid) / (n - p)
    # diagonal of (XtX)^-1 via solving for unit vectors
    se = []
    for i in range(p):
        e = [1.0 if j == i else 0.0 for j in range(p)]
        col = _solve(XtX, e)
        if col is None:
            return None
        se.append(math.sqrt(max(0.0, s2 * col[i])))
    return beta, se, n - p


def season_payload(iv, all_nights, wx):
    """For a temperature-dosed intervention: every counted night as (outdoor night
    temperature, setting, HRV), the nights binned by outdoor temperature with the
    per-setting average in each bin, and a dose x outdoor interaction fit. Outdoor
    temperatures come back in the intervention's own unit. HRV is baseline-adjusted
    when that mode is on (same adjustment as the read-outs). None if too thin."""
    to_f = "f" in (iv.get("unit") or "").lower()
    conv = (lambda c: c * 9.0 / 5.0 + 32.0) if to_f else (lambda c: c)
    cat = iv.get("category")
    rows = []
    for n in all_nights:
        if cat and n.get("excl_" + cat):
            continue
        if n.get("hrv") is None:
            continue
        a = n["assigns"].get(iv["id"])
        if a is None or a.get("excluded"):
            continue
        assigned = a["assigned"] or 0
        if assigned > 0:
            if not (a["taken"] is not None and a["taken"] > 0):
                continue
            dose = float(assigned)
        else:
            dose = 0.0
        w = wx.get(n["date"])
        if not w or w.get("tnight") is None:
            continue
        rows.append({"date": n["date"], "dose": dose, "hrv": float(n["hrv"]),
                     "base": n.get("base_hrv"), "out": conv(float(w["tnight"]))})
    if len(rows) < SEASON_MIN_POINTS:
        return None
    # baseline adjustment (common centre, as in _readout)
    bs = [r["base"] for r in rows if r["base"] is not None]
    if bs and all(r["base"] is not None for r in rows):
        c = sum(bs) / len(bs)
        for r in rows:
            r["hrv"] = r["hrv"] - r["base"] + c
    points = [{"date": r["date"], "dose": r["dose"], "hrv": round(r["hrv"], 1), "out": round(r["out"], 1)}
              for r in rows]
    # outdoor-temperature bins: terciles when there are enough nights, else halves
    outs = sorted(r["out"] for r in rows)
    k = 3 if len(rows) >= 12 else 2
    cuts = [outs[int(len(outs) * i / k)] for i in range(1, k)]
    def bin_of(o):
        return sum(1 for ccut in cuts if o >= ccut)
    deg = "°F" if to_f else "°C"
    labels = []
    for i in range(k):
        if i == 0:
            labels.append(f"below {cuts[0]:.1f}{deg}")
        elif i == k - 1:
            labels.append(f"{cuts[-1]:.1f}{deg} and up")
        else:
            labels.append(f"{cuts[i-1]:.1f}–{cuts[i]:.1f}{deg}")
    doses = sorted({r["dose"] for r in rows})
    bins = []
    for i in range(k):
        inb = [r for r in rows if bin_of(r["out"]) == i]
        cells = {}
        for d in doses:
            v = [r["hrv"] for r in inb if r["dose"] == d]
            if v:
                cells[repr(d)] = {"mean": round(sum(v) / len(v), 1), "n": len(v)}
        best = None
        cand = [(c["mean"], d) for d, c in cells.items() if c["n"] >= SEASON_MIN_CELL]
        if cand:
            best = max(cand)[1]
        bins.append({"label": labels[i], "n": len(inb), "cells": cells, "best": best,
                     "out_mean": round(sum(r["out"] for r in inb) / len(inb), 1) if inb else None})
    # interaction: HRV ~ a + b*dose + c*out + d*dose*out on the ON nights only
    on = [r for r in rows if r["dose"] > 0]
    inter = None
    if len(on) >= 10 and len({r["dose"] for r in on}) >= 2:
        om = sum(r["out"] for r in on) / len(on)
        dm = sum(r["dose"] for r in on) / len(on)
        X = [[1.0, r["dose"] - dm, r["out"] - om, (r["dose"] - dm) * (r["out"] - om)] for r in on]
        fit = _ols(X, [r["hrv"] for r in on])
        if fit:
            beta, se, df = fit
            inter = {"coef": round(beta[3], 3), "se": round(se[3], 3), "n": len(on),
                     "dose_slope": round(beta[1], 2), "dose_slope_se": round(se[1], 2),
                     "out_slope": round(beta[2], 2), "out_slope_se": round(se[2], 2)}
    return {"deg": deg, "points": points, "bins": bins, "doses": doses,
            "dose_counts": {repr(d): sum(1 for r in rows if r["dose"] == d) for d in doses},
            "interaction": inter, "n": len(rows)}


def tags_info(conn):
    """What every payload carries about night-shape tags: whether tagging is on,
    the HRV threshold, and the vocabulary (slug -> label/description)."""
    return {"enabled": tags.enabled(conn), "threshold": tags.threshold(conn),
            "vocab": {v["slug"]: {"label": v["label"], "description": v["description"]}
                      for v in store.tag_vocab(conn)}}


def _fisher_two_sided(a, b, c, d):
    """Two-sided Fisher exact p-value for the 2x2 table [[a, b], [c, d]]: the sum of
    the hypergeometric probabilities of every table (same margins) no more likely
    than the observed one. Exact, stdlib only."""
    n1, n2, k = a + b, c + d, a + c
    n = n1 + n2
    if n == 0 or k == 0 or k == n or n1 == 0 or n2 == 0:
        return 1.0
    denom = math.comb(n, k)
    def p_of(x):
        return math.comb(n1, x) * math.comb(n2, k - x) / denom
    p_obs = p_of(a)
    lo, hi = max(0, k - n2), min(k, n1)
    return min(1.0, sum(p for p in (p_of(x) for x in range(lo, hi + 1)) if p <= p_obs * (1 + 1e-9)))


TAG_FLAG_P = 0.05        # flag an intervention-tag association at this two-sided p
TAG_FLAG_MIN_TAGGED = 3  # ... and only when at least this many of its nights carry the tag


def tag_associations(iv, all_nights, tags_by_date, vocab):
    """Does this intervention's presence go with a kind of night? For every tag,
    compare how often the intervention's confirmed-on nights carry it with how
    often its assigned-off nights do (same nights that count for its HRV read-out:
    category exclusions and cell exclusions respected; recency weights ignored --
    these are raw counts). Fisher's exact test; flagged in either direction.

    Nights above the HRV threshold are never tagged, so an intervention that
    lowers HRV outright will tend to pick up every tag -- the per-flag counts
    among LOW nights only are included so the reader can tell "more bad nights"
    from "a different kind of bad night"."""
    on_dates, off_dates = [], []
    cat = iv.get("category")
    for n in all_nights:
        if cat and n.get("excl_" + cat):
            continue
        if n.get("hrv") is None:
            continue
        st = _on_state(n, iv["id"])
        if st == 1:
            on_dates.append(n["date"])
        elif st == 0:
            off_dates.append(n["date"])
    if not on_dates or not off_dates:
        return []
    low_on = [d for d in on_dates if d in tags_by_date]
    low_off = [d for d in off_dates if d in tags_by_date]
    out = []
    for slug, meta in vocab.items():
        a = sum(1 for d in on_dates if slug in tags_by_date.get(d, {}).get("tags", []))
        c = sum(1 for d in off_dates if slug in tags_by_date.get(d, {}).get("tags", []))
        if a + c < TAG_FLAG_MIN_TAGGED:
            continue
        b, dd = len(on_dates) - a, len(off_dates) - c
        p = _fisher_two_sided(a, b, c, dd)
        if p >= TAG_FLAG_P:
            continue
        r_on, r_off = a / len(on_dates), c / len(off_dates)
        out.append({
            "slug": slug, "label": meta["label"], "direction": "more" if r_on > r_off else "less",
            "on_tag": a, "on_n": len(on_dates), "off_tag": c, "off_n": len(off_dates),
            "low_on_tag": a, "low_on_n": len(low_on), "low_off_tag": c, "low_off_n": len(low_off),
            "ratio": (r_on / r_off) if r_off > 0 else None, "p": round(p, 4),
        })
    rank = {slug: i for i, slug in enumerate(vocab)}   # vocab is insertion-ordered
    out.sort(key=lambda f: rank.get(f["slug"], len(rank)))
    return out


def reports_payload(conn):
    """Per-intervention efficacy read-outs for BOTH metrics (HRV: higher better;
    LRHR: lower better). Each intervention is fit on its own compliant nights.

    Efficacy read-outs are PRIOR-FREE (purely measured). Separately, p_scheduled is the
    prior-INCLUSIVE probability the planner schedules the intervention tonight."""
    ivs = store.list_interventions(conn)
    all_nights = analysis_nights_weighted(conn)
    recency_on = recency_enabled(conn)   # recency mode also disables the lock-in guard
    n_hrv = _n_experiment_nights(all_nights)
    tinfo = tags_info(conn)
    tags_by_date = store.night_tags(conn)
    wx = store.weather_series(conn)
    out = []
    for iv in ivs:
        out.append({
            **iv,
            "tag_flags": tag_associations(iv, all_nights, tags_by_date, tinfo["vocab"]),
            "season": season_payload(iv, all_nights, wx) if is_temperature_dosed(iv) else None,
            "readout": _readout(iv, all_nights, "hrv", negate=False),
            "readout_lrhr": _readout(iv, all_nights, "lrhr", negate=True),
            "readout_tst": _readout(iv, all_nights, "tst", negate=False),
            "readout_lat": _readout(iv, all_nights, "lat", negate=True),
            "readout_eff": _readout(iv, all_nights, "eff", negate=False),
            "observational": _observational_rows(iv, all_nights),
            **_sched_info(iv, all_nights, lockin_off=recency_on),
        })
    return {"n_completed": n_hrv, "interventions": out,
            "hrv_trend": hrv_trend_payload(conn), "tags": tinfo,
            **analysis_modes(conn)}


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
        out.append(_pair_row(n, metric, negate, sa, sb))
    return out


def _pair_row(n, metric, negate, sa, sb):
    """One night shaped for bandit.pair_interaction, carrying its local baseline
    (negated like the value) in baseline-drift mode."""
    row = {"y": (-float(n[metric]) if negate else float(n[metric])), "a": sa, "b": sb,
           "w": float(n.get("w", 1.0))}
    if ("base_" + metric) in n:
        b = n["base_" + metric]
        row["base"] = None if b is None else (-b if negate else b)
    return row


# ---------------------------------------------------------------------------
# Observational behavior states (Reports > Interactions).
# Some hypotheses are about what ACTUALLY happened, not what was assigned --
# e.g. "do Ketone Esters only help on nights I'm fasting?", where it doesn't
# matter whether the fast was randomized or chosen (a "Huel only if hungry"
# night that ended as a fast counts as a fast). Each state derives a per-night
# yes / no / unknown from its source choice intervention: a confirmed option
# decides it where the option pins the behavior, and the recorded completion
# button (assignments.input_value) decides it where the option doesn't -- the
# recorded button, when present, always wins. Unconfirmed nights, and nights
# whose option leaves the behavior open with nothing recorded, are unknown and
# drop out. These states are paired against every intervention on the
# Interactions page, flagged "observed": the state side is NOT randomized, so
# those rows are correlational -- a flag is a lead, not a causal verdict.
# Option / button ids below are the stable ids in "Food and fasting":
#   options: 1 = fast (no eating 4h), 2 = Huel/Soylent, 3 = banana (ate,
#            not Huel, not a meal-state signal), 4 = Huel only if hungry
#            (behavior open -> button);
#   buttons: 1 = "Huel/Soylent", 2 = "Fast", 3 = "Full meal" (meaning: a full
#            meal close to bedtime -- the button label is kept short for the cell).
BEHAVIOR_STATES = [
    {"key": "fasted", "name": "Fasting (actual)", "iv_name": "Food and fasting",
     "yes_options": (1,), "no_options": (2, 3), "yes_inputs": ("2",), "no_inputs": ("1", "3")},
    {"key": "huel", "name": "Huel/Soylent before bed (actual)", "iv_name": "Food and fasting",
     "yes_options": (2,), "no_options": (1, 3), "yes_inputs": ("1",), "no_inputs": ("2", "3")},
    # A real meal near bedtime only ever comes from the recorded button; the
    # protocol options themselves all preclude one (fast / Huel / banana), and
    # banana deliberately stays out of the yes-side -- it's a snack, not a meal.
    {"key": "meal", "name": "Full meal near bedtime (actual)", "iv_name": "Food and fasting",
     "yes_options": (), "no_options": (1, 2), "yes_inputs": ("3",), "no_inputs": ("1", "2")},
]


def _behavior_state(night, iv_id, spec):
    """1 / 0 / None: did this behavior actually happen on this night?"""
    a = night["assigns"].get(iv_id)
    if not a or a.get("excluded"):
        return None
    taken = a.get("taken")
    if taken is None or taken <= 0:          # option not confirmed -> unknown
        return None
    val = a.get("input")
    if val is not None:                       # recorded button is ground truth
        if str(val) in spec["yes_inputs"]:
            return 1
        if str(val) in spec["no_inputs"]:
            return 0
    opt = int(round(taken))
    if opt in spec["yes_options"]:
        return 1
    if opt in spec["no_options"]:
        return 0
    return None


def _pair_nights_state(iv_a, src_iv, spec, all_nights, metric, negate):
    """Nights where intervention A is determined (randomized on/off) AND the
    behavior state is known -- shaped for bandit.pair_interaction."""
    out = []
    for n in all_nights:
        val = n.get(metric)
        if val is None:
            continue
        if n.get("excl_" + (iv_a.get("category") or "")) or n.get("excl_" + (src_iv.get("category") or "")):
            continue
        sa = _on_state(n, iv_a["id"])
        sb = _behavior_state(n, src_iv["id"], spec)
        if sa is None or sb is None:
            continue
        out.append(_pair_row(n, metric, negate, sa, sb))
    return out


def _observational_rows(iv, all_nights):
    """Raw per-behavior averages for the intervention's observational states
    (BEHAVIOR_STATES): nights grouped by what ACTUALLY happened, whether it was
    assigned or chosen. Purely descriptive and correlational -- shown in the
    report card's observed section, never modeled. Respects the category
    exclusion and recency weights. None if the intervention has no states."""
    specs = [s for s in BEHAVIOR_STATES if s["iv_name"] == iv["name"]]
    if not specs:
        return None
    cat = iv.get("category")
    # metric -> state key -> [(w, val)] : each night lands in at most one state
    pts = {"hrv": {s["key"]: [] for s in specs}, "lrhr": {s["key"]: [] for s in specs}}
    for n in all_nights:
        if cat and n.get("excl_" + cat):
            continue
        for s in specs:
            if _behavior_state(n, iv["id"], s) != 1:
                continue
            w = float(n.get("w", 1.0))
            for m in pts:
                if n.get(m) is not None:
                    pts[m][s["key"]].append((w, float(n[m]), n.get("base_" + m)))
    # baseline-drift mode: adjust each metric's values across all its states at
    # once (one common centre), so the states stay comparable with each other
    for m in pts:
        flat = [(k, t) for k, v in pts[m].items() for t in v]
        adj = _baseline_adjusted([t for _, t in flat])
        pts[m] = {k: [] for k in pts[m]}
        for (k, _), wv in zip(flat, adj):
            pts[m][k].append(wv)
    # P(best) among the states: chance this behavior has the highest average HRV
    # (lowest resting HR), given the night-to-night noise. Ranks averages only --
    # correlational, like the rest of the section.
    p_hrv = bandit.group_best(pts["hrv"])
    p_lrhr = bandit.group_best(pts["lrhr"], negate=True)
    out = []
    for s in specs:
        row = {"name": s["name"], "key": s["key"]}
        for m in ("hrv", "lrhr"):
            v = pts[m][s["key"]]
            sw = sum(w for w, _ in v)
            row[m + "_mean"] = (sum(w * x for w, x in v) / sw) if sw else None
            row[m + "_n"] = _nn(sw)
        row["hrv_p_best"] = p_hrv.get(s["key"])
        row["lrhr_p_best"] = p_lrhr.get(s["key"])
        out.append(row)
    return out


def interactions_payload(conn):
    """Every pair of (non-archived) interventions that has shared at least one night
    with both on, with its interaction read-out on HRV (primary) and resting HR.
    Pairs that have never co-occurred are only counted."""
    ivs = store.list_interventions(conn)
    all_nights = analysis_nights_weighted(conn)
    n_hrv = _n_experiment_nights(all_nights)
    pairs, no_overlap = [], 0
    brief = lambda iv: {"id": iv["id"], "name": iv["name"], "category": iv["category"],
                        "kind": iv["kind"], "active": iv["active"]}
    for i in range(len(ivs)):
        for j in range(i + 1, len(ivs)):
            a, b = ivs[i], ivs[j]
            r_hrv = bandit.pair_interaction(_pair_nights(a, b, all_nights, "hrv", False))
            r_lrhr = bandit.pair_interaction(_pair_nights(a, b, all_nights, "lrhr", True))
            if not ((r_hrv and r_hrv["n_both"]) or (r_lrhr and r_lrhr["n_both"])):
                no_overlap += 1
                continue
            pairs.append({"a": brief(a), "b": brief(b), "hrv": r_hrv, "lrhr": r_lrhr})
    # observational behavior states x every intervention except their own source
    # (that pair is confounded by construction). The randomized rows above are
    # untouched -- these are ADDITIONAL, correlational rows.
    for spec in BEHAVIOR_STATES:
        src = next((x for x in ivs if x["name"] == spec["iv_name"]), None)
        if not src:
            continue
        for a in ivs:
            if a["id"] == src["id"]:
                continue
            r_hrv = bandit.pair_interaction(_pair_nights_state(a, src, spec, all_nights, "hrv", False))
            r_lrhr = bandit.pair_interaction(_pair_nights_state(a, src, spec, all_nights, "lrhr", True))
            if not ((r_hrv and r_hrv["n_both"]) or (r_lrhr and r_lrhr["n_both"])):
                no_overlap += 1
                continue
            pairs.append({"a": brief(a),
                          "b": {"id": "state:" + spec["key"], "name": spec["name"],
                                "category": src["category"], "kind": "state", "active": True},
                          "observational": True, "hrv": r_hrv, "lrhr": r_lrhr})
    return {"n_completed": n_hrv, "n_interventions": len(ivs), "pairs": pairs,
            "n_pairs_no_overlap": no_overlap,
            **analysis_modes(conn)}


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
            if path == "/api/tags/status":
                return self._send_json(tags.status(conn))
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
                # Stale-tab guard: the client says which night it is showing as
                # Tonight. If that has rolled over since the page loaded, the tick
                # would land on the wrong night, so refuse it and let the client
                # refresh. Ticks on older nights (deliberate backfill) still pass:
                # only the tab's idea of tonight is checked, not the ticked date.
                ct = body.get("client_tonight")
                if ct and str(ct) != today_str():
                    return self._send_json({"error": "stale", "tonight": today_str()}, 409)
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

            if path == "/api/maximize":
                mode = body.get("mode", "preview")
                if mode not in ("preview", "on", "off"):
                    return self._send_json({"error": "bad mode"}, 400)
                res = maximize_tonight(conn, mode)
                if mode != "preview":
                    res["state"] = state_payload(conn)
                return self._send_json(res)

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

            if path == "/api/baseline":
                # server-wide: condition every fit on each night's local baseline
                # (see BASELINE_WINDOW_DAYS). Drives the Reports numbers AND the
                # live bandit, so it lives in the DB, not the browser.
                on = bool(body.get("enabled"))
                store.set_setting(conn, "baseline_adjust", "1" if on else None)
                store._audit(conn, "set_baseline_adjust", enabled=on)
                conn.commit()
                return self._send_json(reports_payload(conn))

            if path == "/api/count_maximize":
                # server-wide: do maximize-benefit (non-randomized) nights count as
                # evidence? Drives the Reports numbers AND the live bandit.
                on = bool(body.get("enabled"))
                store.set_setting(conn, "count_maximize_nights", "1" if on else None)
                store._audit(conn, "set_count_maximize", enabled=on)
                conn.commit()
                return self._send_json(reports_payload(conn))

            if path == "/api/oura/config":
                store.set_setting(conn, "oura_client_id", (body.get("client_id") or "").strip() or None)
                secret = (body.get("client_secret") or "").strip()
                if secret:   # blank means "keep the stored secret"
                    store.set_setting(conn, "oura_client_secret", secret)
                return self._send_json(oura.status(conn))

            if path == "/api/oura/sync":
                res = oura.sync(conn, today_str(), force=bool(body.get("force")))
                try:
                    weather.sync(conn, today_str())
                except Exception:
                    traceback.print_exc()
                if tags.enabled(conn) and tags.api_key(conn) \
                        and store.nights_to_tag(conn, tags.threshold(conn)):
                    tags.run_pending_async(limit=tags.PER_PASS)
                return self._send_json(res)

            if path == "/api/oura/disconnect":
                for k in ("oura_access_token", "oura_refresh_token", "oura_expires_at",
                          "oura_state"):
                    store.set_setting(conn, k, None)
                store._audit(conn, "oura_disconnect")
                conn.commit()
                return self._send_json(oura.status(conn))

            # ---- night-shape tags (tags.py) ----
            if path == "/api/tags/config":
                if "api_key" in body:
                    key = (body.get("api_key") or "").strip()
                    if key:   # blank means "keep the stored key"
                        store.set_setting(conn, "anthropic_api_key", key)
                if "threshold" in body:
                    try:
                        store.set_setting(conn, "tags_threshold", str(float(body["threshold"])))
                    except (TypeError, ValueError):
                        return self._send_json({"error": "Threshold must be a number."}, 400)
                if "enabled" in body:
                    on = bool(body.get("enabled"))
                    if on and not tags.api_key(conn):
                        return self._send_json({"error": "Paste an API key before switching tagging on."}, 400)
                    store.set_setting(conn, "tags_enabled", "1" if on else "0")
                    store._audit(conn, "set_tags_enabled", enabled=on)
                if body.get("forget_key"):
                    store.set_setting(conn, "anthropic_api_key", None)
                    store.set_setting(conn, "tags_enabled", "0")
                st = tags.status(conn)
                if st["enabled"] and st["pending"]:
                    tags.run_pending_async()
                    st["worker"] = tags.worker_status()
                return self._send_json(st)

            if path == "/api/tags/run":
                # tag every pending low night in the background; the panel polls status
                if not tags.api_key(conn):
                    return self._send_json({"error": "No API key saved."}, 400)
                if tags.status(conn)["counts"]["low"] > tags.status(conn)["counts"]["low_with_series"]:
                    oura.backfill_series(conn)
                tags.run_pending_async(limit=body.get("limit"))
                return self._send_json(tags.status(conn))

            if path == "/api/tags/series":
                # fetch the raw series for every recorded night that lacks one
                try:
                    return self._send_json(oura.backfill_series(conn))
                except oura.OuraError as e:
                    return self._send_json({"error": str(e)}, 400)

            if path == "/api/tags/clear":
                n = store.clear_night_tags(conn)
                return self._send_json({"cleared": n, **tags.status(conn)})

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
                reconcile_tonight_options(conn, iid)
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
                weather.sync(conn, today_str())
                if tags.enabled(conn) and tags.api_key(conn) \
                        and store.nights_to_tag(conn, tags.threshold(conn)):
                    tags.run_pending_async(limit=tags.PER_PASS)
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
