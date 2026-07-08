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
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    night_date      TEXT NOT NULL,
    intervention_id INTEGER NOT NULL,
    assigned_dose   REAL NOT NULL,             -- 0 = "off" (skip tonight)
    taken_dose      REAL,                      -- what you actually did; NULL until recorded
    PRIMARY KEY (night_date, intervention_id)
);
"""


def _now():
    return datetime.now().isoformat(timespec="seconds")


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
    ncols = {r["name"] for r in conn.execute("PRAGMA table_info(nights)")}
    if "lrhr" not in ncols:
        conn.execute("ALTER TABLE nights ADD COLUMN lrhr REAL")
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
    }


def list_interventions(conn, active_only=False):
    q = "SELECT * FROM interventions"
    if active_only:
        q += " WHERE active = 1"
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


def create_intervention(conn, name, unit="", default_dose=1.0, unit_dose=0.0,
                        dose_levels=None, note="", kind="dose", options=None,
                        category="supplement", dose_noun="", explore_doses=True):
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
    cur = conn.execute(
        "INSERT INTO interventions (name, unit, default_dose, unit_dose, dose_noun, explore_doses, "
        "dose_levels, active, note, kind, options, category, created_at) "
        "VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?)",
        (name.strip(), unit.strip(), float(default_dose), float(unit_dose or 0),
         (dose_noun or "").strip(), 1 if explore_doses else 0, json.dumps(dose_levels),
         note.strip(), kind, json.dumps(options), category, _now()),
    )
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
    elif kind == "boolean":
        setcol("options", "[]")
        setcol("dose_levels", json.dumps([1]))
        setcol("unit", "")
        setcol("default_dose", 1.0)
        setcol("unit_dose", 0.0)
        setcol("dose_noun", "")
    else:
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
    conn.commit()
    return get_intervention(conn, iid)


def delete_intervention(conn, iid):
    conn.execute("DELETE FROM assignments WHERE intervention_id=?", (iid,))
    conn.execute("DELETE FROM interventions WHERE id=?", (iid,))
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
        conn.commit()
    return get_night(conn, d)


def set_assignment(conn, d, iid, assigned_dose):
    # Checkmark starts UNchecked (taken_dose NULL); you tick cells as you do them.
    conn.execute(
        "INSERT INTO assignments (night_date, intervention_id, assigned_dose) "
        "VALUES (?,?,?) ON CONFLICT(night_date, intervention_id) "
        "DO UPDATE SET assigned_dose=excluded.assigned_dose",
        (d, iid, float(assigned_dose)))
    conn.commit()


def clear_assignments(conn, d):
    conn.execute("DELETE FROM assignments WHERE night_date=?", (d,))
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
    conn.commit()
    return get_night(conn, d)


def set_taken(conn, d, iid, done):
    """Tick/untick the 'did it' checkmark for one cell. Done => took the assigned
    dose; not done => 0."""
    row = conn.execute(
        "SELECT assigned_dose FROM assignments WHERE night_date=? AND intervention_id=?",
        (d, iid)).fetchone()
    if not row:
        return
    conn.execute(
        "UPDATE assignments SET taken_dose=? WHERE night_date=? AND intervention_id=?",
        (row["assigned_dose"] if done else 0.0, d, iid))
    conn.commit()


METRICS = ("hrv", "lrhr")  # hrv: higher is better; lrhr: lower is better


def set_metric(conn, d, metric, value):
    """Set (or clear) an outcome metric for a night from the spreadsheet."""
    if metric not in METRICS:
        return
    ensure_night(conn, d)
    if value is None or value == "":
        conn.execute(f"UPDATE nights SET {metric}=NULL WHERE date=?", (d,))
    else:
        conn.execute(f"UPDATE nights SET {metric}=? WHERE date=?", (float(value), d))
    # keep 'completed' tied to having the primary metric (HRV)
    conn.execute("UPDATE nights SET status=CASE WHEN hrv IS NOT NULL THEN 'completed' "
                 "ELSE 'planned' END WHERE date=?", (d,))
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


def analysis_nights(conn):
    """Every recorded night with per-intervention (assigned, taken) so the engine
    can apply the compliance rule: on-assigned counts only if the box was checked;
    off-assigned always counts as a control."""
    rows = conn.execute(
        "SELECT date, hrv, lrhr FROM nights WHERE hrv IS NOT NULL OR lrhr IS NOT NULL "
        "ORDER BY date").fetchall()
    out = []
    for r in rows:
        assigns = {}
        for a in conn.execute(
                "SELECT intervention_id, assigned_dose, taken_dose FROM assignments "
                "WHERE night_date=?", (r["date"],)):
            assigns[a["intervention_id"]] = {"assigned": a["assigned_dose"], "taken": a["taken_dose"]}
        out.append({"date": r["date"], "hrv": r["hrv"], "lrhr": r["lrhr"], "assigns": assigns})
    return out


def recent_nights(conn, limit=30):
    rows = conn.execute(
        "SELECT date, hrv, lrhr, status, note FROM nights ORDER BY date DESC LIMIT ?",
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
