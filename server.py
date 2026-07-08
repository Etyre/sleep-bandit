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
import os
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import bandit
import store

HOST = "127.0.0.1"
PORT = 8777
PUBLIC_DIR = os.path.join(os.path.dirname(__file__), "public")
GRID_NIGHTS = 21          # how many night-columns to show (newest first)


def today_str():
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Planning (Bayesian bandit / Thompson sampling)
# ---------------------------------------------------------------------------
def intervention_nights(iv_id, all_nights, metric, negate=False):
    """Nights that count for this intervention on the given metric, shaped for the
    bandit ({'hrv': y, 'taken': {iv_id: dose}}).

    Compliance rule (per the user): a night the intervention was assigned ON counts
    only if the checkbox was ticked; an un-ticked ON night is DROPPED (not a control).
    A night it was assigned OFF is always a control (we assume you skipped it).
    Returns (nights, n_on, n_off). For LRHR pass negate=True (lower is better ->
    negate so the engine's higher-is-better logic still applies)."""
    nights, n_on, n_off = [], 0, 0
    for n in all_nights:
        val = n.get(metric)
        if val is None:
            continue
        a = n["assigns"].get(iv_id)
        if a is None:
            continue
        assigned = a["assigned"] or 0
        taken = a["taken"]
        if assigned > 0:
            if taken is not None and taken > 0:  # ON and confirmed
                dose = float(assigned)
                n_on += 1
            else:                                # ON but not confirmed -> drop
                continue
        else:                                    # OFF -> randomized control
            dose = 0.0
            n_off += 1
        nights.append({"hrv": (-float(val) if negate else float(val)),
                       "taken": {iv_id: dose}})
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
        all_nights = store.analysis_nights(conn)
        for iv in missing:
            nights, _, _ = intervention_nights(iv["id"], all_nights, "hrv")
            model = bandit.fit_model([iv], nights)
            plan = bandit.plan_night(model["post"], model["cols"], [iv])
            store.set_assignment(conn, d, iv["id"], plan.get(iv["id"], 0.0))
    return store.get_night(conn, d)


# ---------------------------------------------------------------------------
# Grid-shaped state
# ---------------------------------------------------------------------------
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
            "status": n["status"],
            "is_today": n["date"] == d_today,
        })
        cmap = {}
        for a in n["assignments"]:
            assigned = a["assigned_dose"]
            taken = a["taken_dose"]
            cmap[a["intervention_id"]] = {
                "assigned_dose": assigned,
                "done": taken is not None and taken > 0 and assigned > 0,
            }
        cells[n["date"]] = cmap

    return {"today": d_today, "interventions": ivs, "nights": nights, "cells": cells}


def _readout(iv, all_nights, metric, negate):
    """Fit this one intervention on its compliant nights for one metric and return
    its read-out (with on/off night counts). Empty dict if it has no valid nights."""
    nights, n_on, n_off = intervention_nights(iv["id"], all_nights, metric, negate)
    r = bandit.analyze([iv], nights)["readouts"].get(iv["id"], {}) if nights else {}
    r = dict(r)
    r["n_on"], r["n_off"] = n_on, n_off
    return r


def reports_payload(conn):
    """Per-intervention efficacy read-outs for BOTH metrics (HRV: higher better;
    LRHR: lower better). Each intervention is fit on its own compliant nights."""
    ivs = store.list_interventions(conn)
    all_nights = store.analysis_nights(conn)
    n_hrv = sum(1 for n in all_nights if n["hrv"] is not None)
    out = []
    for iv in ivs:
        out.append({
            **iv,
            "readout": _readout(iv, all_nights, "hrv", negate=False),
            "readout_lrhr": _readout(iv, all_nights, "lrhr", negate=True),
        })
    return {"n_completed": n_hrv, "interventions": out}


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

    def do_GET(self):
        path = urlparse(self.path).path
        statics = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "application/javascript; charset=utf-8"),
        }
        if path in statics:
            name, ctype = statics[path]
            return self._send_file(os.path.join(PUBLIC_DIR, name), ctype)

        conn = store.connect()
        try:
            if path == "/api/state":
                return self._send_json(state_payload(conn))
            if path == "/api/reports":
                return self._send_json(reports_payload(conn))
            if path == "/api/export":
                return self._send_json({
                    "interventions": store.list_interventions(conn),
                    "nights": store.recent_nights(conn, 100000),
                })
        finally:
            conn.close()
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
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
                )
                ensure_tonight(conn)
                return self._send_json(state_payload(conn), 201)

            if path == "/api/cell":
                store.set_taken(conn, body["date"], int(body["intervention_id"]),
                                bool(body.get("done")))
                return self._send_json(state_payload(conn))

            if path == "/api/metric":
                store.set_metric(conn, body["date"], body.get("metric", "hrv"),
                                 body.get("value"))
                return self._send_json(state_payload(conn))
        finally:
            conn.close()
        self.send_error(404)

    def do_PUT(self):
        path = urlparse(self.path).path
        body = self._read_body()
        conn = store.connect()
        try:
            if path.startswith("/api/interventions/"):
                iid = int(path.rsplit("/", 1)[-1])
                fields = {k: body[k] for k in
                          ("name", "unit", "default_dose", "unit_dose", "dose_noun", "note",
                           "active", "kind", "options", "category", "explore_doses")
                          if k in body}
                if "dose_levels" in body:
                    fields["dose_levels"] = _clean_levels(body["dose_levels"])
                store.update_intervention(conn, iid, **fields)
                ensure_tonight(conn)
                return self._send_json(state_payload(conn))
        finally:
            conn.close()
        self.send_error(404)

    def do_DELETE(self):
        path = urlparse(self.path).path
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


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Sleep Bandit is running.")
    print(f"  Open http://localhost:{PORT} in your browser.")
    print("  Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
