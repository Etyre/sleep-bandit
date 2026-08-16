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

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "sleep_bandit.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS interventions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    unit         TEXT NOT NULL DEFAULT '',
    default_dose REAL NOT NULL DEFAULT 1,       -- "standard dose": the starting point
    unit_dose    REAL NOT NULL DEFAULT 0,       -- smallest increment between doses
    dose_noun    TEXT NOT NULL DEFAULT '',      -- countable name for one unit dose (e.g. "pill")
    explore_doses INTEGER NOT NULL DEFAULT 1,   -- 0 = only ever use the standard dose
    dose_levels  TEXT NOT NULL DEFAULT '[]',   -- JSON array of candidate ON doses
    active       INTEGER NOT NULL DEFAULT 1,
    note         TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL DEFAULT 'dose',  -- 'dose' | 'choice'
    options      TEXT NOT NULL DEFAULT '[]',    -- JSON array of choice labels (choice kind)
    category     TEXT NOT NULL DEFAULT 'supplement',  -- 'supplement' | 'behavioral'
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
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    night_date      TEXT NOT NULL,
    intervention_id INTEGER NOT NULL,
    assigned_dose   REAL NOT NULL,             -- 0 = "off" (skip tonight)
    taken_dose      REAL,                      -- what you actually did; NULL until recorded
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
    if "lrhr_30s" not in ncols:
        # Oura reports lowest sleeping HR two ways: from 5-minute aggregate samples
        # (what its app displays -- the basis of every hand-typed lrhr here) and from
        # raw 30-second samples (the API's lowest_heart_rate field, usually a beat or
        # two lower). lrhr stays on the 5-minute basis so the series is consistent;
        # the 30-second reading is kept alongside for the record/export.
        conn.execute("ALTER TABLE nights ADD COLUMN lrhr_30s REAL")
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
        "explore_doses": bool(row["explore_doses"]),
        "dose_levels": json.loads(row["dose_levels"]),
        "active": bool(row["active"]),
        "note": row["note"],
        "kind": row["kind"],
        "options": _read_options(row["options"]),
        "category": row["category"],
        "test_skip": bool(row["test_skip"]),
        "prior_helps": row["prior_helps"],
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


def dose_levels_from(standard, unit_dose, explore=True):
    """Candidate doses to explore: the standard dose, plus/minus one unit-dose
    step (dropping non-positive). If no unit dose is given, fall back to +/-50%.
    When explore is False, the only candidate is the standard dose itself."""
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
    tidy = sorted(x for x in raw if x > 0) or [s]
    return [int(x) if float(x).is_integer() else x for x in tidy]


def _read_options(raw):
    """Normalize stored options to [{id, label}]. Old rows stored plain strings;
    map those to 1-based ids so their history (which used the index) still lines up."""
    out = []
    for i, o in enumerate(json.loads(raw)):
        if isinstance(o, dict) and "label" in o:
            out.append({"id": int(o.get("id", i + 1)), "label": str(o["label"])})
        else:
            out.append({"id": i + 1, "label": str(o)})
    return out


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
            label, oid = str(o.get("label", "")).strip(), o.get("id")
        else:
            label, oid = str(o).strip(), None
        if not label:
            continue
        if oid is None:
            oid = nxt
            nxt += 1
        out.append({"id": int(oid), "label": label})
    return out


def _norm_category(category):
    return "behavioral" if category == "behavioral" else "supplement"


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
                        test_skip=False, prior_helps=0.0):
    kind = kind if kind in ("choice", "boolean") else "dose"
    explore_doses = bool(explore_doses)
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
        options = []
        if not dose_levels:
            dose_levels = dose_levels_from(default_dose, unit_dose, explore_doses)
    ts = 1 if (kind == "choice" and test_skip) else 0
    cur = conn.execute(
        "INSERT INTO interventions (name, unit, default_dose, unit_dose, dose_noun, explore_doses, "
        "dose_levels, active, note, kind, options, category, test_skip, prior_helps, created_at) "
        "VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
        (name.strip(), unit.strip(), float(default_dose), float(unit_dose or 0),
         (dose_noun or "").strip(), 1 if explore_doses else 0, json.dumps(dose_levels),
         note.strip(), kind, json.dumps(options), category, ts, _clamp_prior(prior_helps), _now()),
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

    kind = fields.get("kind", iv["kind"])
    kind = kind if kind in ("choice", "boolean") else "dose"
    setcol("kind", kind)
    if kind == "choice":
        opts = _norm_choice_options(fields.get("options", iv["options"]))
        setcol("options", json.dumps(opts))
        setcol("dose_levels", json.dumps([o["id"] for o in opts]))
        setcol("unit", "")
        setcol("default_dose", 1.0)
        setcol("dose_noun", "")
        ts = fields["test_skip"] if fields.get("test_skip") is not None else iv.get("test_skip")
        setcol("test_skip", 1 if ts else 0)
    elif kind == "boolean":
        setcol("options", "[]")
        setcol("dose_levels", json.dumps([1]))
        setcol("unit", "")
        setcol("default_dose", 1.0)
        setcol("unit_dose", 0.0)
        setcol("dose_noun", "")
        setcol("test_skip", 0)
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
        setcol("dose_noun", (fields["dose_noun"].strip() if fields.get("dose_noun") is not None
                             else (iv.get("dose_noun") or "")))
        if fields.get("dose_levels") is not None:
            setcol("dose_levels", json.dumps(fields["dose_levels"]))
        else:
            setcol("dose_levels", json.dumps(dose_levels_from(std, udose, explore)))

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


def set_taken(conn, d, iid, done):
    """Tick/untick the 'did it' checkmark for one cell. Done => took the assigned
    dose; not done => 0."""
    row = conn.execute(
        "SELECT assigned_dose, taken_dose FROM assignments WHERE night_date=? AND intervention_id=?",
        (d, iid)).fetchone()
    if not row:
        return
    new_taken = row["assigned_dose"] if done else 0.0
    conn.execute(
        "UPDATE assignments SET taken_dose=? WHERE night_date=? AND intervention_id=?",
        (new_taken, d, iid))
    _audit(conn, "set_taken", d, iid, done=bool(done),
           taken_before=row["taken_dose"], taken_after=new_taken)
    conn.commit()


METRICS = ("hrv", "lrhr", "eff")  # hrv/eff: higher is better; lrhr: lower is better


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
    col = "excl_behavioral" if category == "behavioral" else "excl_supplement"
    ensure_night(conn, d)
    conn.execute(f"UPDATE nights SET {col}=? WHERE date=?", (1 if excluded else 0, d))
    _audit(conn, "set_exclusion", d, category=category, excluded=bool(excluded))
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
    off-assigned always counts as a control."""
    rows = conn.execute(
        "SELECT date, hrv, lrhr, eff, excl_behavioral, excl_supplement FROM nights "
        "WHERE hrv IS NOT NULL OR lrhr IS NOT NULL OR eff IS NOT NULL ORDER BY date").fetchall()
    out = []
    for r in rows:
        assigns = {}
        for a in conn.execute(
                "SELECT intervention_id, assigned_dose, taken_dose FROM assignments "
                "WHERE night_date=?", (r["date"],)):
            assigns[a["intervention_id"]] = {"assigned": a["assigned_dose"], "taken": a["taken_dose"]}
        out.append({"date": r["date"], "hrv": r["hrv"], "lrhr": r["lrhr"], "eff": r["eff"],
                    "excl_behavioral": bool(r["excl_behavioral"]),
                    "excl_supplement": bool(r["excl_supplement"]), "assigns": assigns})
    return out


def recent_nights(conn, limit=30):
    rows = conn.execute(
        "SELECT date, hrv, lrhr, lrhr_30s, eff, hrv_src, lrhr_src, eff_src, status, note, "
        "excl_behavioral, excl_supplement FROM nights ORDER BY date DESC LIMIT ?",
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
