"""
store.py -- SQLite persistence for Sleep Bandit.

Everything lives in a single file (data/sleep_bandit.db) which you can copy to
back up. sqlite3 is part of the Python standard library, so there is nothing to
install.
"""

import json
import os
import sqlite3
from datetime import date, datetime

# SB_DB_PATH overrides the location (used by tests and optionally by the hosted setup)
DB_PATH = os.environ.get("SB_DB_PATH") or os.path.join(os.path.dirname(__file__), "data", "sleep_bandit.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS interventions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    unit         TEXT NOT NULL DEFAULT '',
    default_dose REAL NOT NULL DEFAULT 1,       -- "standard dose": the starting point
    unit_dose    REAL NOT NULL DEFAULT 0,       -- smallest increment between doses
    dose_noun    TEXT NOT NULL DEFAULT '',      -- countable name for doses (e.g. "pill")
    noun_amount  REAL,                          -- amount (in `unit`) per one dose_noun; NULL = unit_dose
    explore_doses INTEGER NOT NULL DEFAULT 1,   -- 0 = only ever use the standard dose
    delay_explore INTEGER NOT NULL DEFAULT 1,   -- 1 = hold the standard dose until enough on-nights to judge efficacy
    min_dose     REAL,                          -- optional floor for explored doses (NULL = none)
    max_dose     REAL,                          -- optional ceiling for explored doses (NULL = none)
    open_arm     INTEGER NOT NULL DEFAULT 0,    -- 1 = also test an open-ended arm above the max
    open_arm_label TEXT NOT NULL DEFAULT '',    -- how that arm reads (e.g. 'no timer')
    dose_levels  TEXT NOT NULL DEFAULT '[]',   -- JSON array of candidate ON doses
    active       INTEGER NOT NULL DEFAULT 1,
    note         TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'dose',  -- 'dose' | 'choice' | 'boolean' | 'choice_dose'
    options      TEXT NOT NULL DEFAULT '[]',    -- JSON array of choice labels (choice kind)
    category     TEXT NOT NULL DEFAULT 'supplement',  -- see CATEGORIES
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nights (
    date       TEXT PRIMARY KEY,               -- YYYY-MM-DD (the evening)
    hrv        REAL,                           -- NULL until recorded next morning
    lrhr       REAL,                           -- lowest resting heart rate (lower is better)
    status     TEXT NOT NULL DEFAULT 'planned',-- 'planned' | 'completed'
    note       TEXT NOT NULL DEFAULT '',
    excl_behavioral INTEGER NOT NULL DEFAULT 0, -- drop this night from BEHAVIORAL analysis
    excl_supplement INTEGER NOT NULL DEFAULT 0, -- drop this night from SUPPLEMENT analysis
    excl_environmental INTEGER NOT NULL DEFAULT 0, -- drop this night from ENVIRONMENTAL analysis
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    night_date      TEXT NOT NULL,
    intervention_id INTEGER NOT NULL,
    assigned_dose   REAL NOT NULL,             -- 0 = "off" (skip tonight)
    taken_dose      REAL,                      -- what you actually did; NULL until recorded
    excluded        INTEGER NOT NULL DEFAULT 0,-- drop THIS cell from analysis (e.g. did it despite OFF)
    PRIMARY KEY (night_date, intervention_id)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,             -- EXACT local wall-clock time of the write (ms)
    op              TEXT NOT NULL,             -- what happened: assign, assign_blocked, set_taken, ...
    night_date      TEXT,                      -- the night it touched (NULL for intervention-level ops)
    intervention_id INTEGER,                   -- the intervention it touched (NULL if n/a)
    detail          TEXT NOT NULL DEFAULT ''   -- JSON: before/after values and context
);
"""


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _now_exact():
    """Exact wall-clock timestamp (millisecond precision) for the audit log."""
    return datetime.now().isoformat(timespec="milliseconds")


def _audit(conn, op, night_date=None, intervention_id=None, **detail):
    """Append one row to the immutable audit log recording the exact date+time of a
    database write. Never commits on its own -- it rides the caller's transaction, so
    the log entry and the change it describes commit together (or not at all). The
    audit_log is append-only: nothing in the app ever updates or deletes from it, so
    it's the durable trail for reconstructing what changed and when."""
    conn.execute(
        "INSERT INTO audit_log (ts, op, night_date, intervention_id, detail) VALUES (?,?,?,?,?)",
        (_now_exact(), op,
         (str(night_date) if night_date is not None else None),
         (int(intervention_id) if intervention_id is not None else None),
         json.dumps(detail, default=str) if detail else ""))


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # migrate older DBs that predate the kind/options columns
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(interventions)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE interventions ADD COLUMN kind TEXT NOT NULL DEFAULT 'dose'")
    if "options" not in cols:
        conn.execute("ALTER TABLE interventions ADD COLUMN options TEXT NOT NULL DEFAULT '[]'")
    if "unit_dose" not in cols:
        conn.execute("ALTER TABLE interventions ADD COLUMN unit_dose REAL NOT NULL DEFAULT 0")
    if "category" not in cols:
        conn.execute("ALTER TABLE interventions ADD COLUMN category TEXT NOT NULL DEFAULT 'supplement'")
    if "dose_noun" not in cols:
        conn.execute("ALTER TABLE interventions ADD COLUMN dose_noun TEXT NOT NULL DEFAULT ''")
    if "explore_doses" not in cols:
        conn.execute("ALTER TABLE interventions ADD COLUMN explore_doses INTEGER NOT NULL DEFAULT 1")
    if "delay_explore" not in cols:
        # per-intervention: delay dose exploration until the warm-up has enough on-nights
        conn.execute("ALTER TABLE interventions ADD COLUMN delay_explore INTEGER NOT NULL DEFAULT 1")
    if "open_arm" not in cols:
        # open-ended arm above the max dose (recorded as bandit.OPEN_ARM_DOSE)
        conn.execute("ALTER TABLE interventions ADD COLUMN open_arm INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE interventions ADD COLUMN open_arm_label TEXT NOT NULL DEFAULT ''")
    if "noun_amount" not in cols:
        # how much of `unit` one dose_noun is (e.g. 50 mg per cap); NULL falls back to unit_dose
        conn.execute("ALTER TABLE interventions ADD COLUMN noun_amount REAL")
    if "min_dose" not in cols:
        # optional bounds on dose exploration (NULL = unbounded on that side)
        conn.execute("ALTER TABLE interventions ADD COLUMN min_dose REAL")
        conn.execute("ALTER TABLE interventions ADD COLUMN max_dose REAL")
    if "archived" not in cols:
        # soft-delete flag: archived interventions are hidden but their data is kept
        conn.execute("ALTER TABLE interventions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    if "test_skip" not in cols:
        # choice interventions only: also test the "skip" (null) option, giving control
        # nights so the average effect of doing ANY option vs nothing is measurable
        conn.execute("ALTER TABLE interventions ADD COLUMN test_skip INTEGER NOT NULL DEFAULT 0")
    if "prior_helps" not in cols:
        # optional per-intervention PRIOR PROBABILITY that the intervention helps, in
        # [0,1]. 0 = unset and 0.5 = neutral both mean "no prior" (the default). It is
        # used for SCHEDULING only (converted to a prior mean on the effect via
        # bandit.prior_mean_for_prob); it never enters the efficacy numbers and washes
        # out as real nights accumulate.
        conn.execute("ALTER TABLE interventions ADD COLUMN prior_helps REAL NOT NULL DEFAULT 0")
    if "no_lockin" not in cols:
        # per-intervention override: skip the early lock-in scheduling guard
        # (bandit.LOCKIN_TIERS), letting Thompson sampling run unclamped even while
        # an arm is thin. Toggled from the Reports page cap indicator; reversible
        # and audit-logged. Scheduling only -- efficacy read-outs are unaffected.
        conn.execute("ALTER TABLE interventions ADD COLUMN no_lockin INTEGER NOT NULL DEFAULT 0")
    if "input_type" not in cols:
        # how a night's completion is recorded for this intervention: 'checkbox'
        # (the default tick), 'radio' (one of several labeled mutually exclusive
        # buttons -- clicking any of them marks the night done) or 'quantity' (a
        # number, or '-' for done-but-not-measured). Radio labels live in
        # input_options (same stable-id shape as choice options); the quantity's
        # label in input_label. What was recorded is kept per cell in
        # assignments.input_value for later analysis; completion itself still
        # drives taken_dose exactly as the checkbox does.
        conn.execute("ALTER TABLE interventions ADD COLUMN input_type TEXT NOT NULL DEFAULT 'checkbox'")
        conn.execute("ALTER TABLE interventions ADD COLUMN input_options TEXT NOT NULL DEFAULT '[]'")
        conn.execute("ALTER TABLE interventions ADD COLUMN input_label TEXT NOT NULL DEFAULT ''")
    ncols = {r["name"] for r in conn.execute("PRAGMA table_info(nights)")}
    if "lrhr" not in ncols:
        conn.execute("ALTER TABLE nights ADD COLUMN lrhr REAL")
    if "excl_behavioral" not in ncols:
        # per-night, per-category data exclusion: when set, that night's outcome is
        # dropped from the analysis (and planning) of interventions in that category.
        # The assignment record itself is never touched -- exclusion is a separate,
        # reversible, audit-logged flag.
        conn.execute("ALTER TABLE nights ADD COLUMN excl_behavioral INTEGER NOT NULL DEFAULT 0")
    if "excl_supplement" not in ncols:
        conn.execute("ALTER TABLE nights ADD COLUMN excl_supplement INTEGER NOT NULL DEFAULT 0")
    if "excl_environmental" not in ncols:
        # third category (room temperature, light, noise ...) split out of 'behavioral'
        conn.execute("ALTER TABLE nights ADD COLUMN excl_environmental INTEGER NOT NULL DEFAULT 0")
    if "hrv_src" not in ncols:
        # where each metric's current value came from: 'manual' (typed in) or 'oura'
        # (synced). NULL on legacy rows means manual.
        conn.execute("ALTER TABLE nights ADD COLUMN hrv_src TEXT")
        conn.execute("ALTER TABLE nights ADD COLUMN lrhr_src TEXT")
    if "eff" not in ncols:
        # sleep efficiency (percent asleep of time in bed, higher is better) --
        # Oura's `efficiency` field, whole percent; tracked as a third outcome metric
        conn.execute("ALTER TABLE nights ADD COLUMN eff REAL")
        conn.execute("ALTER TABLE nights ADD COLUMN eff_src TEXT")
    if "tst" not in ncols:
        # total sleep time in HOURS (Oura's total_sleep_duration / 3600, higher is
        # better) -- a fourth outcome metric, listed above sleep efficiency
        conn.execute("ALTER TABLE nights ADD COLUMN tst REAL")
        conn.execute("ALTER TABLE nights ADD COLUMN tst_src TEXT")
    if "lat" not in ncols:
        # sleep-onset latency in MINUTES (Oura's latency / 60, lower is better)
        conn.execute("ALTER TABLE nights ADD COLUMN lat REAL")
        conn.execute("ALTER TABLE nights ADD COLUMN lat_src TEXT")
    if "lrhr_30s" not in ncols:
        # Oura reports lowest sleeping HR two ways: from 5-minute aggregate samples
        # (what its app displays -- the basis of every hand-typed lrhr here) and from
        # raw 30-second samples (the API's lowest_heart_rate field, usually a beat or
        # two lower). lrhr stays on the 5-minute basis so the series is consistent;
        # the 30-second reading is kept alongside for the record/export.
        conn.execute("ALTER TABLE nights ADD COLUMN lrhr_30s REAL")
    if "maximize" not in ncols:
        # 1 = this night's randomized plan was replaced by the maximize-benefit plan
        conn.execute("ALTER TABLE nights ADD COLUMN maximize INTEGER NOT NULL DEFAULT 0")
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(assignments)")}
    if "random_dose" not in acols:
        # the randomized plan a maximize-benefit override replaced (NULL otherwise),
        # so switching the override off restores it exactly -- no re-roll
        conn.execute("ALTER TABLE assignments ADD COLUMN random_dose REAL")
    if "excluded" not in acols:
        # per-cell exclusion: this ONE (night, intervention) pair stops counting as on
        # OR off -- for the rare night the intervention was assigned OFF but was done
        # anyway (no longer a valid control), or the cell's data is otherwise invalid.
        # Reversible and audit-logged; the frozen assignment record is never touched.
        conn.execute("ALTER TABLE assignments ADD COLUMN excluded INTEGER NOT NULL DEFAULT 0")
    if "input_value" not in acols:
        # the recorded completion detail for interventions with a non-checkbox
        # input type: a radio option's stable id (as text), or the quantity as
        # typed ('-' = done but not measured). NULL when nothing is recorded.
        conn.execute("ALTER TABLE assignments ADD COLUMN input_value TEXT")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Interventions
# ---------------------------------------------------------------------------
def _iv_to_dict(row):
    return {
        "id": row["id"],
        "name": row["name"],
        "unit": row["unit"],
        "default_dose": row["default_dose"],
        "unit_dose": row["unit_dose"],
        "dose_noun": row["dose_noun"],
        "noun_amount": row["noun_amount"],
        "explore_doses": bool(row["explore_doses"]),
        "delay_explore": bool(row["delay_explore"]),
        "min_dose": row["min_dose"],
        "max_dose": row["max_dose"],
        "open_arm": bool(row["open_arm"]),
        "open_arm_label": row["open_arm_label"] or "",
        "dose_levels": json.loads(row["dose_levels"]),
        "active": bool(row["active"]),
        "note": row["note"],
        "kind": row["kind"],
        "options": _read_options(row["options"]),
        "category": row["category"],
        "test_skip": bool(row["test_skip"]),
        "prior_helps": row["prior_helps"],
        "no_lockin": bool(row["no_lockin"]),
        "input_type": row["input_type"],
        "input_options": _read_options(row["input_options"]),
        "input_label": row["input_label"] or "",
    }


def list_interventions(conn, active_only=False):
    # archived (soft-deleted) interventions never show; their data stays in the DB.
    q = "SELECT * FROM interventions WHERE archived = 0"
    if active_only:
        q += " AND active = 1"
    q += " ORDER BY active DESC, name COLLATE NOCASE"
    return [_iv_to_dict(r) for r in conn.execute(q)]


def get_intervention(conn, iid):
    r = conn.execute("SELECT * FROM interventions WHERE id=?", (iid,)).fetchone()
    return _iv_to_dict(r) if r else None


def _opt_dose(v):
    """Parse an optional dose bound: None/''/invalid/<=0 -> None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def dose_levels_from(standard, unit_dose, explore=True, min_dose=None, max_dose=None):
    """Candidate doses to explore: the standard dose, plus/minus one unit-dose
    step (dropping non-positive and anything outside the optional min/max bounds).
    If no unit dose is given, fall back to +/-50%. When explore is False, the only
    candidate is the standard dose itself."""
    s = float(standard)
    if not explore:
        return [int(s) if s.is_integer() else s]
    u = float(unit_dose or 0)
    if u > 0:
        raw = {round(s - u, 6), round(s, 6), round(s + u, 6)}
    elif s > 1:
        raw = {round(0.5 * s, 6), round(s, 6), round(1.5 * s, 6)}
    else:                       # on/off behaviour
        raw = {s}
    lo, hi = _opt_dose(min_dose), _opt_dose(max_dose)
    tidy = sorted(x for x in raw if x > 0
                  and (lo is None or x >= lo) and (hi is None or x <= hi)) or [s]
    return [int(x) if float(x).is_integer() else x for x in tidy]


def _read_options(raw):
    """Normalize stored options to [{id, label}]. Old rows stored plain strings;
    map those to 1-based ids so their history (which used the index) still lines up."""
    out = []
    for i, o in enumerate(json.loads(raw)):
        if isinstance(o, dict) and "label" in o:
            d = {"id": int(o.get("id", i + 1)), "label": str(o["label"])}
            if _opt_prior(o.get("prior")) is not None:
                d["prior"] = _opt_prior(o.get("prior"))
            out.append(d)
        else:
            out.append({"id": i + 1, "label": str(o)})
    return out


def _opt_prior(v):
    """An option's optional prior probability of being best: a float in (0, 1], else
    None (unset). Stored as a fraction; the form edits it as a percent."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 0 < f <= 1 else None


def _norm_choice_options(options):
    """Assign a stable id to every option (keeping existing ids, minting new ones
    for options without one). The id is what history/analysis keys on, so an
    option survives renames and reordering."""
    options = options or []
    used = [int(o["id"]) for o in options if isinstance(o, dict) and o.get("id") is not None]
    nxt = (max(used) + 1) if used else 1
    out = []
    for o in options:
        if isinstance(o, dict):
            label, oid, prior = str(o.get("label", "")).strip(), o.get("id"), _opt_prior(o.get("prior"))
        else:
            label, oid, prior = str(o).strip(), None, None
        if not label:
            continue
        if oid is None:
            oid = nxt
            nxt += 1
        d = {"id": int(oid), "label": label}
        if prior is not None:
            d["prior"] = prior
        out.append(d)
    return out


# Intervention kinds. 'choice_dose' = multiple choice + dose: the app picks an option
# AND a dose each night (shared dose scale; every option x dose pair is its own arm).
KINDS = ("dose", "choice", "boolean", "choice_dose")

# Intervention categories, in display order. Each has a per-night exclusion column
# on `nights` named excl_<category>. Unknown values fall back to 'supplement'.
CATEGORIES = ("supplement", "behavioral", "environmental")


def _norm_category(category):
    return category if category in CATEGORIES else "supplement"


# Completion input types: how a night's cell is marked done. 'checkbox' is the
# default tick; 'radio' offers labeled mutually exclusive buttons (any one = done);
# 'quantity' takes a number (or '-' for done-but-not-measured). The recorded value
# is stored on the cell (assignments.input_value) for later analysis.
INPUT_TYPES = ("checkbox", "radio", "quantity")


def _norm_input(input_type, input_options, input_label):
    """Normalize an intervention's completion-input settings. Radio options reuse
    the stable-id machinery of choice options (history keys on the id, so a button
    survives renames); a radio with no options falls back to a checkbox."""
    t = input_type if input_type in INPUT_TYPES else "checkbox"
    opts = _norm_choice_options(input_options) if t == "radio" else []
    if t == "radio" and not opts:
        t = "checkbox"
    lab = (input_label or "").strip() if t == "quantity" else ""
    return t, opts, lab


# Prior probability that an intervention helps is capped: beyond this the scheduler
# would keep it on so often that discovery (needing OFF nights for contrast) stalls.
PRIOR_HELPS_MAX = 0.80


def _clamp_prior(p):
    """Normalize a stored prior probability to [0, PRIOR_HELPS_MAX]. 0 = unset/neutral."""
    try:
        return min(PRIOR_HELPS_MAX, max(0.0, float(p or 0)))
    except (TypeError, ValueError):
        return 0.0


def create_intervention(conn, name, unit="", default_dose=1.0, unit_dose=0.0,
                        dose_levels=None, note="", kind="dose", options=None,
                        category="supplement", dose_noun="", explore_doses=True,
                        test_skip=False, prior_helps=0.0, min_dose=None, max_dose=None,
                        noun_amount=None, delay_explore=True, open_arm=False, open_arm_label="",
                        input_type="checkbox", input_options=None, input_label=""):
    kind = kind if kind in KINDS else "dose"
    explore_doses = bool(explore_doses)
    delay_explore = bool(delay_explore)
    min_dose, max_dose, noun_amount = _opt_dose(min_dose), _opt_dose(max_dose), _opt_dose(noun_amount)
    # the open-ended arm needs a max to sit above (and exploration to reach it)
    open_arm = bool(open_arm) and max_dose is not None and explore_doses
    open_arm_label = (open_arm_label or "").strip()
    if kind not in ("dose", "choice_dose"):
        min_dose = max_dose = noun_amount = None
    if kind != "dose":
        open_arm = False                       # the open-ended arm is dose-only
    category = _norm_category(category)
    if kind == "choice":
        options = _norm_choice_options(options)
        # each option is an "arm"; its stable id is the dose level
        dose_levels = [o["id"] for o in options]
        unit, default_dose, unit_dose, dose_noun = "", 1.0, 0.0, ""
    elif kind == "boolean":
        # a plain yes/no: a single "on" arm (dose 1) vs off
        options, dose_levels = [], [1]
        unit, default_dose, unit_dose, dose_noun = "", 1.0, 0.0, ""
    else:
        # dose, or choice+dose (options AND a shared dose scale; arms = option x dose)
        options = _norm_choice_options(options) if kind == "choice_dose" else []
        if not dose_levels:
            dose_levels = dose_levels_from(default_dose, unit_dose, explore_doses, min_dose, max_dose)
    ts = 1 if (kind in ("choice", "choice_dose") and test_skip) else 0
    in_t, in_opts, in_lab = _norm_input(input_type, input_options, input_label)
    cur = conn.execute(
        "INSERT INTO interventions (name, unit, default_dose, unit_dose, dose_noun, explore_doses, "
        "delay_explore, min_dose, max_dose, open_arm, open_arm_label, noun_amount, dose_levels, "
        "active, note, kind, options, category, test_skip, prior_helps, "
        "input_type, input_options, input_label, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)",
        (name.strip(), unit.strip(), float(default_dose), float(unit_dose or 0),
         (dose_noun or "").strip(), 1 if explore_doses else 0, 1 if delay_explore else 0,
         min_dose, max_dose, 1 if open_arm else 0, open_arm_label, noun_amount,
         json.dumps(dose_levels), note.strip(), kind, json.dumps(options), category, ts,
         _clamp_prior(prior_helps), in_t, json.dumps(in_opts), in_lab, _now()),
    )
    _audit(conn, "create_intervention", intervention_id=cur.lastrowid,
           name=name.strip(), kind=kind, category=category)
    conn.commit()
    return get_intervention(conn, cur.lastrowid)


def update_intervention(conn, iid, **fields):
    iv = get_intervention(conn, iid)
    if not iv:
        return None
    cols, vals = [], []

    def setcol(col, val):
        cols.append(f"{col}=?")
        vals.append(val)

    if fields.get("name") is not None:
        setcol("name", fields["name"].strip())
    if fields.get("note") is not None:
        setcol("note", fields["note"].strip())
    if "active" in fields and fields["active"] is not None:
        setcol("active", 1 if fields["active"] else 0)
    if fields.get("category") is not None:
        setcol("category", _norm_category(fields["category"]))
    if fields.get("prior_helps") is not None:
        setcol("prior_helps", _clamp_prior(fields["prior_helps"]))
    if fields.get("no_lockin") is not None:
        setcol("no_lockin", 1 if fields["no_lockin"] else 0)
    if any(k in fields for k in ("input_type", "input_options", "input_label")):
        in_t, in_opts, in_lab = _norm_input(
            fields.get("input_type", iv["input_type"]),
            fields.get("input_options", iv["input_options"]),
            fields.get("input_label", iv["input_label"]))
        setcol("input_type", in_t)
        setcol("input_options", json.dumps(in_opts))
        setcol("input_label", in_lab)

    kind = fields.get("kind", iv["kind"])
    kind = kind if kind in KINDS else "dose"
    setcol("kind", kind)
    if kind == "choice":
        opts = _norm_choice_options(fields.get("options", iv["options"]))
        setcol("options", json.dumps(opts))
        setcol("dose_levels", json.dumps([o["id"] for o in opts]))
        setcol("unit", "")
        setcol("default_dose", 1.0)
        setcol("dose_noun", "")
        setcol("min_dose", None)
        setcol("max_dose", None)
        setcol("noun_amount", None)
        setcol("open_arm", 0)
        ts = fields["test_skip"] if fields.get("test_skip") is not None else iv.get("test_skip")
        setcol("test_skip", 1 if ts else 0)
    elif kind == "boolean":
        setcol("options", "[]")
        setcol("dose_levels", json.dumps([1]))
        setcol("unit", "")
        setcol("default_dose", 1.0)
        setcol("unit_dose", 0.0)
        setcol("dose_noun", "")
        setcol("min_dose", None)
        setcol("max_dose", None)
        setcol("noun_amount", None)
        setcol("open_arm", 0)
        setcol("test_skip", 0)
    else:
        if kind == "choice_dose":
            opts = _norm_choice_options(fields.get("options", iv["options"]))
            setcol("options", json.dumps(opts))
            ts = fields["test_skip"] if fields.get("test_skip") is not None else iv.get("test_skip")
            setcol("test_skip", 1 if ts else 0)
        else:
            setcol("test_skip", 0)
            setcol("options", "[]")
        std = float(fields["default_dose"]) if fields.get("default_dose") is not None else iv["default_dose"]
        udose = float(fields["unit_dose"]) if fields.get("unit_dose") is not None else (iv.get("unit_dose") or 0)
        explore = bool(fields["explore_doses"]) if fields.get("explore_doses") is not None \
            else iv.get("explore_doses", True)
        if fields.get("unit") is not None:
            setcol("unit", fields["unit"].strip())
        setcol("default_dose", std)
        setcol("unit_dose", udose)
        setcol("explore_doses", 1 if explore else 0)
        delay = bool(fields["delay_explore"]) if fields.get("delay_explore") is not None \
            else iv.get("delay_explore", True)
        setcol("delay_explore", 1 if delay else 0)
        setcol("dose_noun", (fields["dose_noun"].strip() if fields.get("dose_noun") is not None
                             else (iv.get("dose_noun") or "")))
        # bounds: an explicit null/blank clears them, absence keeps the old value
        lo = _opt_dose(fields["min_dose"]) if "min_dose" in fields else _opt_dose(iv.get("min_dose"))
        hi = _opt_dose(fields["max_dose"]) if "max_dose" in fields else _opt_dose(iv.get("max_dose"))
        setcol("min_dose", lo)
        setcol("max_dose", hi)
        # open-ended arm above the max: needs a max (and exploration) to exist
        oa = bool(fields["open_arm"]) if fields.get("open_arm") is not None else bool(iv.get("open_arm"))
        setcol("open_arm", 1 if (oa and hi is not None and explore and kind == "dose") else 0)
        if fields.get("open_arm_label") is not None:
            setcol("open_arm_label", str(fields["open_arm_label"]).strip())
        setcol("noun_amount", _opt_dose(fields["noun_amount"]) if "noun_amount" in fields
               else _opt_dose(iv.get("noun_amount")))
        if fields.get("dose_levels") is not None:
            setcol("dose_levels", json.dumps(fields["dose_levels"]))
        else:
            setcol("dose_levels", json.dumps(dose_levels_from(std, udose, explore, lo, hi)))

    vals.append(iid)
    conn.execute(f"UPDATE interventions SET {', '.join(cols)} WHERE id=?", vals)
    _audit(conn, "update_intervention", intervention_id=iid,
           changed=[c.split("=")[0] for c in cols])
    conn.commit()
    return get_intervention(conn, iid)


def delete_intervention(conn, iid):
    """Soft delete: hide the intervention from the tracker and reports but KEEP every
    assignment and outcome it ever recorded, so an accidental delete loses no data.
    Also deactivate it so it stops being suggested. (The row and its history remain
    in the DB and can be un-archived by clearing `archived`.)"""
    conn.execute("UPDATE interventions SET archived=1, active=0 WHERE id=?", (iid,))
    _audit(conn, "delete_intervention", intervention_id=iid)
    conn.commit()


# ---------------------------------------------------------------------------
# Nights + assignments
# ---------------------------------------------------------------------------
def get_night(conn, d):
    r = conn.execute("SELECT * FROM nights WHERE date=?", (d,)).fetchone()
    if not r:
        return None
    night = dict(r)
    night["assignments"] = [
        dict(a) for a in conn.execute(
            "SELECT * FROM assignments WHERE night_date=? ORDER BY intervention_id",
            (d,))
    ]
    return night


def ensure_night(conn, d):
    if not conn.execute("SELECT 1 FROM nights WHERE date=?", (d,)).fetchone():
        conn.execute(
            "INSERT INTO nights (date, status, created_at) VALUES (?, 'planned', ?)",
            (d, _now()))
        _audit(conn, "create_night", d)
        conn.commit()
    return get_night(conn, d)


def set_assignment(conn, d, iid, assigned_dose):
    """Record the plan for one cell. INSERT-ONLY and FROZEN: once a
    (night, intervention) assignment exists it can never be rewritten here.

    This is the core integrity guarantee of the experiment: the moment a night's
    dose/option is assigned, that is the historical record of what was prescribed,
    and it must not change afterwards -- not by a later planning run, an engine
    change, a server restart, or a reload. Planning may only *fill in* a cell that
    doesn't exist yet (ON CONFLICT DO NOTHING); to deliberately correct a mistaken
    record, use force_assignment (never called during normal planning).

    Checkmark starts UNchecked (taken_dose NULL); you tick cells as you do them.
    """
    cur = conn.execute(
        "INSERT INTO assignments (night_date, intervention_id, assigned_dose) "
        "VALUES (?,?,?) ON CONFLICT(night_date, intervention_id) DO NOTHING",
        (d, iid, float(assigned_dose)))
    if cur.rowcount:                       # a brand-new cell was created
        _audit(conn, "assign", d, iid, assigned_dose=float(assigned_dose))
    else:                                  # the cell already existed -> freeze protected it
        _audit(conn, "assign_blocked", d, iid, attempted_dose=float(assigned_dose))
    conn.commit()


def force_assignment(conn, d, iid, assigned_dose, taken_dose=None):
    """Deliberately overwrite an assignment (and optionally its taken_dose). This is
    the ONLY sanctioned way to change an already-recorded plan -- used for manual
    corrections, never by the planner. Kept separate from set_assignment so ordinary
    planning can't reach it."""
    conn.execute(
        "INSERT INTO assignments (night_date, intervention_id, assigned_dose, taken_dose) "
        "VALUES (?,?,?,?) ON CONFLICT(night_date, intervention_id) "
        "DO UPDATE SET assigned_dose=excluded.assigned_dose, taken_dose=excluded.taken_dose",
        (d, iid, float(assigned_dose), taken_dose))
    _audit(conn, "force_assign", d, iid, assigned_dose=float(assigned_dose), taken_dose=taken_dose)
    conn.commit()


def set_night_maximize(conn, d, on):
    """Flag / unflag a night as planned for maximum benefit (server.maximize_tonight)."""
    conn.execute("UPDATE nights SET maximize=? WHERE date=?", (1 if on else 0, d))
    _audit(conn, "maximize", d, enabled=bool(on))
    conn.commit()


def override_assignment(conn, d, iid, dose):
    """Maximize-benefit override of ONE unticked cell: the randomized plan is parked
    in random_dose (kept if already parked) and assigned_dose becomes `dose`. The
    sanctioned exception to the assignment freeze -- reversible via
    restore_assignment, audit-logged, and never touching a cell already ticked.
    Returns True if the cell changed."""
    row = conn.execute("SELECT assigned_dose, taken_dose, random_dose FROM assignments "
                       "WHERE night_date=? AND intervention_id=?", (d, iid)).fetchone()
    if not row or row["taken_dose"] is not None:
        return False
    if row["random_dose"] is None and float(dose) == row["assigned_dose"]:
        return False                                   # already the plan; nothing to park
    parked = row["random_dose"] if row["random_dose"] is not None else row["assigned_dose"]
    conn.execute("UPDATE assignments SET random_dose=?, assigned_dose=? "
                 "WHERE night_date=? AND intervention_id=?", (parked, float(dose), d, iid))
    _audit(conn, "maximize_assign", d, iid, before=row["assigned_dose"], after=float(dose),
           random_dose=parked)
    conn.commit()
    return True


def restore_assignment(conn, d, iid):
    """Undo a maximize-benefit override: put the parked randomized plan back as the
    cell's assignment, exactly as it was. Ticked cells stay as done. Returns True if
    the cell changed."""
    row = conn.execute("SELECT assigned_dose, taken_dose, random_dose FROM assignments "
                       "WHERE night_date=? AND intervention_id=?", (d, iid)).fetchone()
    if not row or row["random_dose"] is None or row["taken_dose"] is not None:
        return False
    conn.execute("UPDATE assignments SET assigned_dose=random_dose, random_dose=NULL "
                 "WHERE night_date=? AND intervention_id=?", (d, iid))
    _audit(conn, "maximize_restore", d, iid, before=row["assigned_dose"], after=row["random_dose"])
    conn.commit()
    return True


def delete_assignment(conn, d, iid):
    """Remove ONE intervention's cell from a night entirely -- as if it was never part of
    the experiment that night, so the night stops counting as on OR off for it. A
    deliberate, audit-logged correction; it leaves every other cell untouched. Returns
    True if a row was removed."""
    row = conn.execute(
        "SELECT assigned_dose, taken_dose FROM assignments WHERE night_date=? AND intervention_id=?",
        (d, iid)).fetchone()
    if not row:
        return False
    conn.execute("DELETE FROM assignments WHERE night_date=? AND intervention_id=?", (d, iid))
    _audit(conn, "delete_assignment", d, iid,
           removed={"assigned_dose": row["assigned_dose"], "taken_dose": row["taken_dose"]})
    conn.commit()
    return True


def clear_assignments(conn, d):
    """DANGER: deletes a night's plan outright, discarding the frozen record. Not used
    anywhere in the app's normal flow -- kept only for tooling. Never call this on a
    night you intend to re-plan; deleting then re-inserting defeats the freeze in
    set_assignment and would silently rewrite history."""
    removed = [dict(r) for r in conn.execute(
        "SELECT intervention_id, assigned_dose, taken_dose FROM assignments WHERE night_date=?", (d,))]
    conn.execute("DELETE FROM assignments WHERE night_date=?", (d,))
    _audit(conn, "clear_assignments", d, removed=removed)
    conn.commit()


def record_night(conn, d, hrv, taken, note=""):
    """
    Record a completed night: HRV value plus what was actually taken.
    taken : dict intervention_id -> dose actually taken (0 = skipped/off).
    """
    ensure_night(conn, d)
    conn.execute(
        "UPDATE nights SET hrv=?, status='completed', note=? WHERE date=?",
        (float(hrv), note, d))
    for iid, dose in taken.items():
        # only update rows that were actually assigned that night
        conn.execute(
            "UPDATE assignments SET taken_dose=? WHERE night_date=? AND "
            "intervention_id=?", (float(dose), d, int(iid)))
    _audit(conn, "record_night", d, hrv=float(hrv), taken={int(k): v for k, v in taken.items()})
    conn.commit()
    return get_night(conn, d)


def set_taken(conn, d, iid, done, input_value=None):
    """Tick/untick the 'did it' checkmark for one cell. Done => took the assigned
    dose; not done => 0. input_value is the recorded completion detail for a
    non-checkbox input type (a radio option's stable id as text, or the quantity
    as typed -- '-' = done but not measured); it is stored when done and cleared
    when not."""
    row = conn.execute(
        "SELECT assigned_dose, taken_dose, input_value FROM assignments "
        "WHERE night_date=? AND intervention_id=?",
        (d, iid)).fetchone()
    if not row:
        return
    new_taken = row["assigned_dose"] if done else 0.0
    new_val = str(input_value).strip() if (done and input_value is not None
                                           and str(input_value).strip() != "") else None
    conn.execute(
        "UPDATE assignments SET taken_dose=?, input_value=? WHERE night_date=? AND intervention_id=?",
        (new_taken, new_val, d, iid))
    _audit(conn, "set_taken", d, iid, done=bool(done),
           taken_before=row["taken_dose"], taken_after=new_taken,
           input_before=row["input_value"], input_after=new_val)
    conn.commit()


METRICS = ("hrv", "lrhr", "tst", "lat", "eff")  # hrv/tst/eff: higher is better; lrhr/lat: lower is better


def set_metric(conn, d, metric, value):
    """Set (or clear) an outcome metric for a night from the spreadsheet."""
    if metric not in METRICS:
        return
    ensure_night(conn, d)
    cleared = value is None or value == ""
    if cleared:
        conn.execute(f"UPDATE nights SET {metric}=NULL, {metric}_src=NULL WHERE date=?", (d,))
    else:
        conn.execute(f"UPDATE nights SET {metric}=?, {metric}_src='manual' WHERE date=?",
                     (float(value), d))
    # keep 'completed' tied to having the primary metric (HRV)
    conn.execute("UPDATE nights SET status=CASE WHEN hrv IS NOT NULL THEN 'completed' "
                 "ELSE 'planned' END WHERE date=?", (d,))
    _audit(conn, "set_metric", d, metric=metric, value=(None if cleared else float(value)))
    conn.commit()


def set_metric_from_oura(conn, d, metric, value):
    """Write an Oura-sourced outcome metric. Oura's numbers are the measurement of
    record (finer-grained than what its app displays, and than what was typed in by
    hand), so an existing value IS replaced when it differs -- the audit log keeps the
    old value. Returns 'filled' (was blank), 'updated' (replaced a different value),
    or None (nothing to do / already identical)."""
    if metric not in METRICS + ("lrhr_30s",) or value is None:
        return None
    value = float(value)
    ensure_night(conn, d)
    row = conn.execute(f"SELECT {metric} FROM nights WHERE date=?", (d,)).fetchone()
    old = row[metric] if row else None
    if old is not None and abs(old - value) < 0.05:   # same number at display precision
        # value stands, but Oura has confirmed it -- tag the source (no audit needed,
        # nothing measurable changed) so the UI doesn't flag it as hand-entered
        if metric in METRICS:
            cur = conn.execute(f"UPDATE nights SET {metric}_src='oura' "
                               f"WHERE date=? AND {metric}_src IS NOT 'oura'", (d,))
            if cur.rowcount:
                conn.commit()
        return None
    if metric in METRICS:   # hrv/lrhr carry a source tag; lrhr_30s is oura-only anyway
        conn.execute(f"UPDATE nights SET {metric}=?, {metric}_src='oura' WHERE date=?", (value, d))
    else:
        conn.execute(f"UPDATE nights SET {metric}=? WHERE date=?", (value, d))
    conn.execute("UPDATE nights SET status=CASE WHEN hrv IS NOT NULL THEN 'completed' "
                 "ELSE 'planned' END WHERE date=?", (d,))
    _audit(conn, "oura_fill", d, metric=metric, value=value, replaced=old)
    conn.commit()
    return "filled" if old is None else "updated"


def nights_for_oura_sync(conn, since, before):
    """Past nights (since <= date < before) a sync should look at -- every night in the
    window, because Oura data also upgrades hand-typed values to full precision."""
    return [dict(r) for r in conn.execute(
        "SELECT date, hrv, lrhr, lrhr_30s FROM nights WHERE date >= ? AND date < ? "
        "ORDER BY date", (since, before))]


# ---------------------------------------------------------------------------
# Settings (small key/value pairs: Oura credentials + tokens, sync bookkeeping)
# ---------------------------------------------------------------------------
def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    if value is None:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))
    else:
        conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()


def set_note(conn, d, note):
    """Set the free-text note for a night (the night is created if it doesn't exist).
    The full note text is captured in the audit log so an accidental overwrite is
    recoverable, same as every other write."""
    ensure_night(conn, d)
    note = (note or "").strip()
    conn.execute("UPDATE nights SET note=? WHERE date=?", (note, d))
    _audit(conn, "set_note", d, note=note)
    conn.commit()


def set_exclusion(conn, d, category, excluded):
    """Exclude (or re-include) a night's outcome from ONE category's analysis. This never
    touches an assignment -- it's a reversible, audit-logged flag that only changes which
    nights the engine counts for interventions in that category."""
    category = _norm_category(category)
    col = "excl_" + category
    ensure_night(conn, d)
    conn.execute(f"UPDATE nights SET {col}=? WHERE date=?", (1 if excluded else 0, d))
    _audit(conn, "set_exclusion", d, category=category, excluded=bool(excluded))
    conn.commit()


def set_cell_exclusion(conn, d, iid, excluded):
    """Exclude (or re-include) ONE intervention's cell for a night, so that night stops
    counting as on OR off for that intervention only. The rare case this exists for:
    the intervention was assigned OFF but you did it anyway, so the night is not a
    valid control for it. Like the category-level exclusion, it's a reversible,
    audit-logged flag -- the frozen assignment record (assigned/taken) is untouched."""
    row = conn.execute(
        "SELECT excluded FROM assignments WHERE night_date=? AND intervention_id=?",
        (d, iid)).fetchone()
    if not row:
        return
    conn.execute(
        "UPDATE assignments SET excluded=? WHERE night_date=? AND intervention_id=?",
        (1 if excluded else 0, d, iid))
    _audit(conn, "set_cell_exclusion", d, iid,
           excluded=bool(excluded), was=bool(row["excluded"]))
    conn.commit()


def completed_nights(conn):
    """All recorded nights, shaped for the bandit engine."""
    rows = conn.execute(
        "SELECT date, hrv FROM nights WHERE hrv IS NOT NULL ORDER BY date").fetchall()
    out = []
    for r in rows:
        taken = {}
        for a in conn.execute(
                "SELECT intervention_id, taken_dose, assigned_dose "
                "FROM assignments WHERE night_date=?", (r["date"],)):
            # fall back to the assigned dose if adherence wasn't explicitly set
            dose = a["taken_dose"] if a["taken_dose"] is not None else a["assigned_dose"]
            taken[a["intervention_id"]] = dose
        out.append({"date": r["date"], "hrv": r["hrv"], "taken": taken})
    return out


def hrv_series(conn):
    """Every night with a recorded HRV as (date, hrv), oldest first."""
    return [(r["date"], r["hrv"]) for r in conn.execute(
        "SELECT date, hrv FROM nights WHERE hrv IS NOT NULL ORDER BY date")]


def first_assigned_night(conn):
    """Date of the earliest planned night -- i.e. when the app started being used.
    Nights before this exist only as backfilled Oura history (no assignments)."""
    row = conn.execute("SELECT MIN(night_date) AS d FROM assignments").fetchone()
    return row["d"] if row else None


def analysis_nights(conn):
    """Every recorded night with per-intervention (assigned, taken) so the engine
    can apply the compliance rule: on-assigned counts only if the box was checked;
    off-assigned always counts as a control.

    Maximize-benefit nights (nights.maximize = 1) are LEFT OUT entirely: their plan
    was chosen from the data rather than randomized, so they are not evidence for
    any effect estimate (per the user, 2026-09-13). Their metrics are still recorded
    and still show in the grid and charts -- they just never feed the model."""
    rows = conn.execute(
        "SELECT date, hrv, lrhr, tst, lat, eff, excl_behavioral, excl_supplement, excl_environmental FROM nights "
        "WHERE (hrv IS NOT NULL OR lrhr IS NOT NULL OR tst IS NOT NULL OR lat IS NOT NULL OR eff IS NOT NULL) "
        "AND maximize = 0 "
        "ORDER BY date").fetchall()
    out = []
    for r in rows:
        assigns = {}
        for a in conn.execute(
                "SELECT intervention_id, assigned_dose, taken_dose, excluded, input_value "
                "FROM assignments WHERE night_date=?", (r["date"],)):
            assigns[a["intervention_id"]] = {"assigned": a["assigned_dose"],
                                             "taken": a["taken_dose"],
                                             "excluded": bool(a["excluded"]),
                                             "input": a["input_value"]}
        out.append({"date": r["date"], "hrv": r["hrv"], "lrhr": r["lrhr"], "tst": r["tst"], "lat": r["lat"], "eff": r["eff"],
                    "excl_behavioral": bool(r["excl_behavioral"]),
                    "excl_supplement": bool(r["excl_supplement"]),
                    "excl_environmental": bool(r["excl_environmental"]), "assigns": assigns})
    return out


def recent_nights(conn, limit=30):
    rows = conn.execute(
        "SELECT date, hrv, lrhr, lrhr_30s, tst, lat, eff, hrv_src, lrhr_src, tst_src, lat_src, eff_src, status, note, "
        "excl_behavioral, excl_supplement, excl_environmental, maximize FROM nights ORDER BY date DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for r in rows:
        assigns = [dict(a) for a in conn.execute(
            "SELECT * FROM assignments WHERE night_date=? ORDER BY intervention_id",
            (r["date"],))]
        out.append({**dict(r), "assignments": assigns})
    return out


def history(conn):
    """(date, hrv) series for charting, oldest first."""
    return [dict(r) for r in conn.execute(
        "SELECT date, hrv FROM nights WHERE hrv IS NOT NULL ORDER BY date")]


def latest_unrecorded(conn, today):
    """Most recent planned night (<= today) still missing an HRV value."""
    r = conn.execute(
        "SELECT date FROM nights WHERE hrv IS NULL AND date <= ? "
        "ORDER BY date DESC LIMIT 1", (today,)).fetchone()
    return r["date"] if r else None
