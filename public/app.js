"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) { const e = new Error((await r.text()) || r.statusText); e.status = r.status; throw e; }
  return r.status === 204 ? null : r.json();
};
const post = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const signed = (x) => (x >= 0 ? "+" : "") + Number(x).toFixed(1);
const pct = (p) => Math.round(p * 100) + "%";
const numLabel = (d) => (Number.isInteger(d) ? d : +d.toFixed(3));
// Open-ended arm above the max dose ("no timer — stay on until I fall asleep"): recorded
// as this sentinel dose (mirrors bandit.OPEN_ARM_DOSE) and shown by its label, never as a number.
const OPEN_ARM_DOSE = 1e9;
const isOpenDose = (d) => Number(d) >= OPEN_ARM_DOSE / 2;
// "Multiple choice + dose": a cell's level packs option id and dose as
// option_id * COMBO_BASE + dose (mirrors bandit.COMBO_BASE).
const COMBO_BASE = 1e6;
const comboDecode = (level) => { const o = Math.floor(Number(level) / COMBO_BASE); return [o, +(Number(level) - o * COMBO_BASE).toFixed(6)]; };
const isChoiceKind = (iv) => iv.kind === "choice" || iv.kind === "choice_dose";
const sameLevel = (a, b) => Math.abs(Number(a) - Number(b)) < 1e-6;
const openLabel = (iv) => (iv.open_arm_label || "").trim() || "no timer";
const avg1 = (x) => Number(x).toFixed(1);   // raw metric averages, one decimal
const shortDate = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" }); };
const weekday = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "short" }); };

let STATE = null;

// Runtime config injected by the server (see server._send_page). mode: "local" (your
// own machine, full access, no login), "public" (hosted root: read-only, no notes),
// "login" (hosted admin path, not signed in) or "admin" (signed in, full access).
const SB = window.SB || { mode: "local", base: "/", hosted: false };
const READ_ONLY = SB.mode === "public" || SB.mode === "login";

const CATEGORY_ORDER = ["supplement", "behavioral", "environmental"];
const CATEGORY_LABEL = { supplement: "Supplements", behavioral: "Behavioral", environmental: "Environmental" };

// "prior belief it helps" is stored as a PROBABILITY in [0,1] (0 or 0.5 = neutral / no
// prior). It's used for scheduling only, never in the efficacy numbers. The UI edits it
// as a whole-number percent.
const priorPct = (frac) => Math.round((Number(frac) || 0) * 100);

// outcome metrics shown as rows under the interventions (HRV is the target)
const METRICS = [
  { key: "hrv", label: "HRV", hint: "higher is better" },
  { key: "lrhr", label: "Resting HR", hint: "lower is better · bpm" },
  // the three sleep-architecture metrics are minor next to HRV / resting HR: shown smaller
  { key: "tst", label: "Total sleep", hint: "higher is better · hours", minor: true },
  { key: "lat", label: "Sleep latency", hint: "lower is better · min", minor: true },
  { key: "eff", label: "Sleep efficiency", hint: "higher is better · %", minor: true },
];

// Report-card charts fill their column (about half the card) instead of hugging
// their few data points; the column shrinks them proportionally on narrow screens.
const CHART_MIN_W = 540;

// need a real contrast before we show any recommendation: at least this many
// nights on AND off (choice: this many option-nights total).
const MIN_ON = 2, MIN_OFF = 2, MIN_CHOICE = 4;
// crowning a *dose* as "recommended" is a higher bar than showing the read-out:
// it needs enough on-nights AND at least two distinct doses actually tried, else
// the "best dose" is just prior noise. (Dose exploration is deliberately slow, so
// this crown stays hidden until real dose-contrast data accumulates.)
const MIN_ON_DOSE_REC = 8;

function groupByCategory(list) {
  const seen = [];
  const by = {};
  for (const iv of list) {
    const c = CATEGORY_ORDER.includes(iv.category) ? iv.category : "supplement";
    if (!by[c]) { by[c] = []; }
    by[c].push(iv);
  }
  const cats = CATEGORY_ORDER.filter((c) => by[c]);
  return cats.map((c) => [c, by[c]]);
}

function pluralize(noun, n) {
  if (n === 1) return noun;
  // only plain words pluralize ("pill" -> "pills"); unit-like symbols or
  // abbreviations ("° c", "mg", "IU") are left alone
  if (!/^[a-z][a-z -]*$/i.test(noun) || /^(mg|mcg|g|iu|ml|l|oz|kg|min|sec|hr|hrs)$/i.test(noun)) return noun;
  if (/[^aeiou]y$/i.test(noun)) return noun.slice(0, -1) + "ies";
  return noun + (/s$/i.test(noun) ? "" : "s");
}

// how a dose amount is written: as a count ("3 pills") if the intervention names a
// countable noun -- one noun = noun_amount of the unit (falls back to the unit dose) --
// otherwise as the raw amount ("144 mg").
// The open-ended arm is written as "[max]+" in the same terms as a normal dose
// ("30+ minutes", "4+ pills"), never as an infinity symbol; the arm's label follows.
function openDoseText(iv, native) {
  const max = iv.max_dose || 0;
  if (!(max > 0)) return "∞";
  const u = iv.noun_amount || iv.unit_dose || 0, noun = (iv.dose_noun || "").trim();
  if (!native && u > 0 && noun) return `${numLabel(max / u)}+ ${pluralize(noun, 2)}`;
  return iv.unit ? `${numLabel(max)}+ ${iv.unit}` : `${numLabel(max)}+`;
}
function doseTextPlain(iv, amount) {
  if (isOpenDose(amount)) return `${openDoseText(iv, false)} · ${openLabel(iv)}`;
  const u = iv.noun_amount || iv.unit_dose || 0, noun = (iv.dose_noun || "").trim();
  if (u > 0 && noun) {
    const count = amount / u;
    return `${numLabel(count)} ${pluralize(noun, count)}`;
  }
  return iv.unit ? `${numLabel(amount)} ${iv.unit}` : "on";
}
const doseText = (iv, amount) => esc(doseTextPlain(iv, amount));

// Reports page shows doses in the supplement's native unit (mg/g/mcg/minutes),
// never as a pill count -- the Tracker keeps pills, but results read in mass.
function doseAmount(iv, amount) {
  if (isOpenDose(amount)) return `${esc(openDoseText(iv, true))} · ${esc(openLabel(iv))}`;
  return iv.unit ? `${numLabel(amount)} ${esc(iv.unit)}` : "on";
}

// options are {id, label}; look one up by its stable id
function optLabel(iv, id) {
  if (iv.kind === "choice_dose") {
    // an encoded option+dose pair reads "Option · dose" (plain text; callers escape)
    const [oid, dose] = comboDecode(id);
    const o = (iv.options || []).find((x) => x.id === oid);
    return o ? `${o.label} · ${doseTextPlain(iv, dose)}` : null;
  }
  const o = (iv.options || []).find((x) => x.id === Math.round(id));
  return o ? o.label : null;
}

// label shown in a grid cell (null = not activated / grayed out)
function cellLabel(iv, assigned) {
  if (isChoiceKind(iv)) {
    const label = optLabel(iv, assigned);
    return label != null ? esc(label) : null;
  }
  if (iv.kind === "boolean") return assigned > 0 ? "yes" : null;
  if (assigned > 0) return doseText(iv, assigned);
  return null;
}

// Maximize-benefit toggle for tonight. ON overrides tonight's unticked cells
// with the all-exploit plan (their randomized plan is parked server-side); OFF
// puts the parked randomized plan back exactly -- nothing is re-rolled.
async function toggleMaximize() {
  const tonight = STATE.nights.find((n) => n.is_today);
  if (!tonight) return;
  const res = await post("/api/maximize", { mode: tonight.maximize ? "off" : "on" });
  STATE = res.state; renderGrid();
}

// options still on offer (a removed option stays in iv.options as retired so its
// past nights keep their label, but it's never planned or listed as current)
function liveOptions(iv) { return (iv.options || []).filter((o) => !o.retired); }

function ivMeta(iv) {
  if (iv.kind === "choice_dose") return liveOptions(iv).map((o) => esc(o.label)).join(" · ") + ` × ${doseText(iv, iv.default_dose)}`;
  if (iv.kind === "choice") return liveOptions(iv).map((o) => esc(o.label)).join(" · ");
  if (iv.kind === "boolean") return "yes / no";
  return doseText(iv, iv.default_dose);
}

// =====================================================================
// TRACKER GRID
// =====================================================================
async function loadGrid() {
  STATE = await api("/api/state");
  renderRecencySlot(STATE.recency, STATE.count_maximize, STATE.baseline);
  renderGrid();
}

function renderGrid() {
  const { interventions, cells } = STATE;
  // Server sends nights newest-first; show them oldest -> newest so tonight is on the right.
  const nights = STATE.nights.slice().reverse();
  const table = $("#grid");

  if (!interventions.length) {
    table.innerHTML =
      `<tbody><tr><td class="empty" style="padding:26px">No interventions yet. Click <b>+ Add intervention</b> to start experimenting.</td></tr></tbody>`;
    return;
  }

  // header: night columns (oldest first, tonight on the right)
  let head = `<thead><tr><th class="corner">Intervention</th>`;
  for (const n of nights) {
    // Tonight carries the maximize-benefit toggle: all exploit, no explore --
    // every intervention found to help, at its best-measured dose or option.
    // Switching it off restores the exact randomized plan (no re-roll).
    let maxCtl = "";
    if (n.is_today && !READ_ONLY) {
      maxCtl = `<button type="button" class="max-btn${n.maximize ? " on" : ""}" title="${n.maximize
        ? "Tonight is planned for maximum benefit (not randomized). Click to go back to the exact randomized plan."
        : "Maximize benefit tonight: no exploration — every intervention found to help, at its best-measured dose or option. Not randomized. Reversible."}">` +
        `⭐ ${n.maximize ? "Maximizing benefit" : "Maximize benefit"}</button>`;
    } else if (n.maximize) {
      // a past (or read-only) maximize night keeps its label for good
      maxCtl = `<span class="max-btn on static" title="This night was planned for maximum benefit, not randomized${COUNT_MAX_ON ? "." : " — its data doesn't count toward results (see the header switch)."}">⭐ Maximize benefit</span>`;
    }
    head += `<th class="night${n.is_today ? " today" : ""}">` +
      `${n.is_today ? "Tonight" : weekday(n.date)}<span class="ndate">${shortDate(n.date)}</span>${cityCtl(n)}${maxCtl}</th>`;
  }
  head += `</tr></thead>`;

  let body = `<tbody>`;

  // intervention rows, grouped by category
  for (const [cat, listRaw] of groupByCategory(interventions)) {
    const list = listRaw.slice().sort((a, b) =>
      a.active === b.active ? a.name.localeCompare(b.name) : (a.active ? -1 : 1));
    // category header row: each night gets a toggle to exclude that night's data for
    // this category (its outcome stops counting toward these interventions' results).
    const exKey = "excl_" + cat;
    const catWord = CATEGORY_LABEL[cat].toLowerCase();
    body += `<tr class="group-row"><th class="rowlab group-lab">${CATEGORY_LABEL[cat]}</th>`;
    for (const n of nights) {
      const ex = !!n[exKey];
      if (READ_ONLY) {   // no toggles; just mark the excluded nights
        body += `<td class="group-fill exclcell${n.is_today ? " col-today" : ""}">` +
          (ex ? `<span class="excl-btn on static" title="${catWord} data excluded from this night's results">excluded</span>` : "") + `</td>`;
        continue;
      }
      body += `<td class="group-fill exclcell${n.is_today ? " col-today" : ""}">` +
        `<button class="excl-btn${ex ? " on" : ""}" data-date="${n.date}" data-cat="${cat}" ` +
        `title="${ex ? `${catWord} data excluded from this night's results — click to include`
                     : `Exclude this night's ${catWord} data from results`}">` +
        `${ex ? "excluded" : "exclude"}</button></td>`;
    }
    body += `</tr>`;
    // Paused interventions sit under a fold row ("▸ 3 paused") so they don't clutter
    // the grid; click it to show or hide them. Remembered per category.
    const nPaused = list.filter((iv) => !iv.active).length;
    const open = !!PAUSED_OPEN[cat];
    for (const iv of list) {
      if (!iv.active && !open) continue;
      if (!iv.active && iv === list.find((x) => !x.active)) {
        body += foldRow(cat, nPaused, open, nights.length);
      }
      const meta = ivMeta(iv) + (iv.active ? "" : " · paused");
      body += `<tr class="${iv.active ? "" : "paused"}" data-iv-id="${iv.id}">` +
        `<th class="rowlab">${esc(iv.name)}<span class="rmeta">${meta}</span></th>`;
      for (const n of nights) {
        const c = (cells[n.date] || {})[iv.id];
        const todayCls = n.is_today ? " col-today" : "";
        // dim: this night doesn't count here -- category-excluded, or a past
        // maximize-benefit night while the "count maximize nights" switch is off.
        // Tonight stays undimmed while maximizing so its plan is still readable.
        const exCls = (n[exKey] || (n.maximize && !n.is_today && !COUNT_MAX_ON)) ? " excluded" : "";
        if (!c) { body += `<td class="cell na${todayCls}${exCls}"></td>`; continue; }
        const label = cellLabel(iv, c.assigned_dose);
        const ro = READ_ONLY ? " ro" : "";
        if (label != null) {
          // ON cell. How completion is recorded depends on the intervention's
          // input type: the default checkbox (click the cell), mutually exclusive
          // buttons (click one — it records WHICH and marks the night done), or a
          // number ("-" = done but not measured). The recorded value is saved per
          // cell (input_value) for later analysis.
          // the ✓ checkmark visual belongs to the checkbox type only; radio and
          // quantity cells show their own control (the done state still colors
          // the cell)
          const cls = `cell active${c.done ? " done" : ""}${todayCls}${exCls}${ro}`;
          const core = `<span class="dose">${label}</span>`;
          const inputType = iv.input_type || "checkbox";
          if (inputType === "radio" && (iv.input_options || []).length) {
            const sel = c.input_value != null ? String(c.input_value) : null;
            let controls = "";
            for (const o of iv.input_options) {
              const on = sel === String(o.id);
              if (READ_ONLY) { if (on) controls += `<span class="in-val">${esc(o.label)}</span>`; }
              else controls += `<button type="button" class="in-radio${on ? " on" : ""}" data-oid="${o.id}" ` +
                `title="${on ? "Recorded — click to un-mark this night" : "Mark done: " + esc(o.label)}">${esc(o.label)}</button>`;
            }
            body += `<td class="${cls} has-input" data-date="${n.date}" data-iv="${iv.id}">${core}${controls}</td>`;
          } else if (inputType === "quantity") {
            const v = c.input_value != null ? String(c.input_value) : "";
            const controls = READ_ONLY
              ? (v ? `<span class="in-val">${esc(v)}${iv.input_label ? " " + esc(iv.input_label) : ""}</span>` : "")
              : `<input class="in-qty" type="text" inputmode="decimal" value="${esc(v)}" ` +
                `placeholder="${esc(iv.input_label || "qty")}" ` +
                `title="${esc(iv.input_label || "Quantity")} — a number, or “-” for done but not measured; clear to un-mark">`;
            body += `<td class="${cls} has-input" data-date="${n.date}" data-iv="${iv.id}">${core}${controls}</td>`;
          } else {
            body += `<td class="${cls}" data-date="${n.date}" data-iv="${iv.id}">${core}<span class="check">✓</span></td>`;
          }
        } else if (READ_ONLY) {
          const cex = !!c.excluded;
          body += `<td class="cell off${cex ? " cell-excl" : ""}${todayCls}${exCls}" ` +
            `title="${cex ? "Done despite assigned off — excluded from this intervention's results" : "Assigned off (control night)"}">${cex ? "✗" : "–"}</td>`;
        } else {
          // OFF (control) cell. Clicking it marks the rare "did it anyway" night:
          // that one cell is excluded from this intervention's results (it's not a
          // valid control), without touching the frozen assignment record.
          const cex = !!c.excluded;
          body += `<td class="cell off togglable${cex ? " cell-excl" : ""}${todayCls}${exCls}" ` +
            `data-date="${n.date}" data-iv="${iv.id}" ` +
            `title="${cex ? "Did it despite assigned off — excluded from this intervention's results. Click to count it as a control again."
                          : "Assigned off (control night). Click if you did it anyway, to exclude this cell from this intervention's results."}">` +
            `${cex ? "✗" : "–"}</td>`;
        }
      }
      body += `</tr>`;
    }
    if (nPaused && !open) body += foldRow(cat, nPaused, open, nights.length);
  }

  // separator between interventions and the outcome metrics
  body += `<tr class="group-row outcomes-row"><th class="rowlab group-lab">Outcomes</th>` +
    `<td class="group-fill" colspan="${nights.length}"></td></tr>`;

  // outcome metric rows (HRV = target; resting HR = secondary, lower is better)
  for (const m of METRICS) {
    if (m.key === "tst") body += tagsRow(nights);   // night-shape tags sit under resting HR
    body += `<tr class="metric-row${m.minor ? " minor" : ""}"><th class="rowlab metric-lab">${m.label}` +
      `<span class="rmeta">${m.hint}</span></th>`;
    for (const n of nights) {
      // source marker: hand-entered values get a small pencil (Oura-synced is the
      // norm, so the exception is what's flagged); tooltips say it either way
      const manual = n[m.key] != null && n[m.key + "_src"] !== "oura";
      const title = n[m.key] == null ? "" :
        ` title="${manual ? "entered by hand" : "synced from Oura"}"`;
      // Oura values are measured to one decimal, so a trailing .0 is significant --
      // show it. Hand-typed values display exactly as typed.
      const val = n[m.key] == null ? "" :
        (manual ? n[m.key] : Number(n[m.key]).toFixed(1));
      body += `<td class="hrvcell${n.is_today ? " col-today" : ""}${manual ? " manual" : ""}">` +
        `<input type="number" step="any" min="0" data-date="${n.date}" data-metric="${m.key}" ` +
        `value="${val}" placeholder="—"${title}${READ_ONLY ? " readonly tabindex=\"-1\"" : ""}></td>`;
    }
    body += `</tr>`;
  }
  // per-night free-text notes (one editable cell per column) -- never on the public view
  if (!READ_ONLY) {
    body += `<tr class="metric-row notes-row"><th class="rowlab metric-lab">Notes` +
      `<span class="rmeta">per night</span></th>`;
    for (const n of nights) {
      body += `<td class="notecell${n.is_today ? " col-today" : ""}">` +
        `<textarea rows="2" data-date="${n.date}" placeholder="—">${esc(n.note || "")}</textarea></td>`;
    }
    body += `</tr>`;
  }

  body += `</tbody>`;
  table.innerHTML = head + body;

  if (!READ_ONLY) {
    // cells with a non-checkbox input type save through their inner controls,
    // not a whole-cell click
    $$(".cell.active:not(.has-input)", table).forEach((td) => td.addEventListener("click", () => toggleCell(td)));
    $$(".cell .in-radio", table).forEach((b) => b.addEventListener("click", () => saveCellRadio(b)));
    $$(".cell .in-qty", table).forEach((inp) => inp.addEventListener("change", () => saveCellQty(inp)));
    $$(".cell.off.togglable", table).forEach((td) => td.addEventListener("click", () => toggleCellExclusion(td)));
    $$(".hrvcell input", table).forEach((inp) => inp.addEventListener("change", () => saveMetric(inp)));
    $$(".notecell textarea", table).forEach((t) => t.addEventListener("change", () => saveNote(t)));
    $$(".excl-btn", table).forEach((b) => b.addEventListener("click", () => toggleExclusion(b)));
    $$("button.max-btn", table).forEach((b) => b.addEventListener("click", toggleMaximize));
    $$(".city-sel", table).forEach((sel) => sel.addEventListener("change", () => saveCity(sel)));
  }
  $$(".fold-btn", table).forEach((b) => b.addEventListener("click", () => {
    PAUSED_OPEN[b.dataset.cat] = !PAUSED_OPEN[b.dataset.cat];
    try { localStorage.setItem("sb.pausedOpen", JSON.stringify(PAUSED_OPEN)); } catch (e) { /* private mode etc. */ }
    const wrap = $(".grid-wrap");
    GRID_SCROLL = wrap ? wrap.scrollLeft : null;
    renderGrid();
  }));

  // Tonight is the right-most column, so start scrolled all the way over to it --
  // but only on a fresh load; a re-render (e.g. after a toggle) keeps your place.
  const wrap = table.closest(".grid-wrap");
  if (wrap) wrap.scrollLeft = GRID_SCROLL == null ? wrap.scrollWidth : GRID_SCROLL;
}

let GRID_SCROLL = null;  // remembered horizontal scroll, so re-renders don't jump to Tonight

// ---- where each night was slept -----------------------------------------------
// A city dropdown under each date. A night without a choice of its own inherits
// the previous night's -- that IS the normal case, shown exactly like an explicit
// choice: if you slept in DC last night you're assumed to be in DC tonight until
// you say otherwise. "Other…" adds a city
// (geocoded server-side) and selects it. The night's outdoor weather (the season
// sections on the Reports page) is looked up at this city.
function cityCtl(n) {
  if (READ_ONLY || !n.city_eff) return "";
  const inherited = !n.city;
  const opts = (STATE.cities || []).map((c) =>
    `<option value="${esc(c)}"${c === n.city_eff ? " selected" : ""}>${esc(c)}</option>`).join("");
  return `<select class="city-sel" data-date="${n.date}" ` +
    `title="${inherited ? "Where you slept — same as the night before unless you change it."
                        : "Where you slept this night (later nights follow it)"}">` +
    `${opts}<option value="__other">Other…</option></select>`;
}
async function saveCity(sel) {
  const date = sel.dataset.date;
  let city = sel.value;
  try {
    if (city === "__other") {
      const name = prompt("City (it will be located automatically):");
      if (!name || !name.trim()) { renderGrid(); return; }
      const r = await post("/api/cities", { name: name.trim() });
      city = r.city;
    }
    const wrap = $(".grid-wrap"); GRID_SCROLL = wrap ? wrap.scrollLeft : null;
    STATE = await post("/api/city", { date, city });
    renderGrid();
  } catch (e) { alert("Error: " + e.message); renderGrid(); }
}

// ---- night-shape tags row --------------------------------------------------
// Low-HRV nights (below STATE.tags.threshold) get one or more shape tags from
// Claude's read of the raw within-night series (tags.py). Row appears once
// tagging is on or any shown night carries a tag; a low night still waiting for
// its read shows a dashed "…".
const tagLabel = (slug) => ((STATE && STATE.tags && STATE.tags.vocab[slug]) || {}).label || slug;
const tagDesc = (slug) => ((STATE && STATE.tags && STATE.tags.vocab[slug]) || {}).description || "";
function tagChip(slug, extraTitle) {
  const t = tagDesc(slug) + (extraTitle ? "\n\n" + extraTitle : "");
  return `<span class="ntag" title="${esc(t)}">${esc(tagLabel(slug))}</span>`;
}
function tagsRow(nights) {
  const info = (STATE && STATE.tags) || { enabled: false, threshold: 42, vocab: {} };
  if (!info.enabled && !nights.some((n) => (n.tags || []).length)) return "";
  let row = `<tr class="metric-row minor tags-row"><th class="rowlab metric-lab">Night shape` +
    `<span class="rmeta">low-HRV nights (&lt; ${esc(info.threshold)}) · tagged by Claude</span></th>`;
  for (const n of nights) {
    const ts = n.tags || [];
    let inner = "";
    if (ts.length) inner = ts.map((s) => tagChip(s, n.tags_summary || "")).join("");
    else if (info.enabled && n.hrv != null && n.hrv < info.threshold)
      inner = `<span class="ntag pending" title="Low night — not read yet (tagging runs in the background)">…</span>`;
    row += `<td class="tagcell${n.is_today ? " col-today" : ""}"><div class="tagwrap">${inner}</div></td>`;
  }
  return row + `</tr>`;
}

// ---- stale-tab guard ---------------------------------------------------------
// The grid is a snapshot: nothing pushes updates to an open tab. A tab left open
// past the 4 AM rollover keeps showing yesterday as "Tonight", and a tap on it
// files the tick under yesterday's date (this happened on 2026-09-25: a tab from
// Wednesday swallowed Thursday's ticks). Two defences:
//   * every tick carries the date this tab believes is tonight; the server
//     refuses (409) when that is no longer its tonight, and we refresh instead;
//   * on focus, on return from the background, and every few minutes while
//     visible, ask the server for the current state and re-render if Tonight
//     has moved on. Backfilling older nights on purpose still works: only the
//     tab's notion of *tonight* is checked, not the date being ticked.
const stateTonight = () => ((STATE && STATE.nights || []).find((n) => n.is_today) || {}).date;
let SYNCING = false;
async function syncTonight() {
  if (!STATE || SYNCING) return false;
  SYNCING = true;
  try {
    const fresh = await api("/api/state");
    const now = (fresh.nights.find((n) => n.is_today) || {}).date;
    if (now === stateTonight()) return false;
    STATE = fresh; GRID_SCROLL = null;                 // jump to the real Tonight
    renderRecencySlot(STATE.recency, STATE.count_maximize, STATE.baseline);
    renderGrid();
    return true;
  } catch (e) {
    return false;                                      // offline etc.: next trigger retries
  } finally {
    SYNCING = false;
  }
}
// A tick the server refused as stale: refresh and tell the user nothing was saved.
async function handleStaleTick(e) {
  if (!e || e.status !== 409) return false;
  await syncTonight();
  alert("Nothing was saved: this page was still showing an old night as \u201cTonight\u201d. The grid has been refreshed \u2014 please tick again.");
  return true;
}
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") syncTonight(); });
window.addEventListener("focus", () => { syncTonight(); });
setInterval(() => { if (document.visibilityState === "visible") syncTonight(); }, 5 * 60 * 1000);

// which categories currently show their paused interventions (default: hidden)
let PAUSED_OPEN = {};
try { PAUSED_OPEN = JSON.parse(localStorage.getItem("sb.pausedOpen") || "{}") || {}; } catch (e) { PAUSED_OPEN = {}; }

function foldRow(cat, n, open, ncols) {
  return `<tr class="fold-row"><th class="rowlab fold-lab">` +
    `<button class="fold-btn" data-cat="${cat}" title="${open ? "Hide" : "Show"} paused interventions">` +
    `${open ? "▾" : "▸"} ${n} paused</button></th>` +
    `<td class="group-fill" colspan="${ncols}"></td></tr>`;
}

async function toggleExclusion(btn) {
  const date = btn.dataset.date, cat = btn.dataset.cat;
  const excluded = !btn.classList.contains("on");
  const wrap = $(".grid-wrap");
  GRID_SCROLL = wrap ? wrap.scrollLeft : null;   // stay where we are across the re-render
  try {
    STATE = await post("/api/exclude", { date, category: cat, excluded });
    renderGrid();
  } catch (e) {
    alert("Could not update exclusion: " + e.message);
  }
}

async function toggleCellExclusion(td) {
  const cell = (STATE.cells[td.dataset.date] || {})[td.dataset.iv];
  const excluded = !(cell && cell.excluded);
  const wrap = $(".grid-wrap");
  GRID_SCROLL = wrap ? wrap.scrollLeft : null;   // keep our place across the re-render
  try {
    STATE = await post("/api/exclude_cell",
      { date: td.dataset.date, intervention_id: td.dataset.iv, excluded });
    renderGrid();
  } catch (e) {
    alert("Could not update exclusion: " + e.message);
  }
}

async function toggleCell(td) {
  const done = !td.classList.contains("done");
  td.classList.toggle("done", done);                       // optimistic
  const cell = (STATE.cells[td.dataset.date] || {})[td.dataset.iv];
  if (cell) cell.done = done;
  try {
    await post("/api/cell", { date: td.dataset.date, intervention_id: td.dataset.iv, done,
                              client_tonight: stateTonight() });
  } catch (e) {
    td.classList.toggle("done", !done);
    if (cell) cell.done = !done;
    if (await handleStaleTick(e)) return;
    alert("Could not save: " + e.message);
  }
}

// Radio completion: clicking a button records it (and marks the night done);
// clicking the selected button again clears the night back to not-done.
async function saveCellRadio(btn) {
  const td = btn.closest("td");
  const cell = (STATE.cells[td.dataset.date] || {})[td.dataset.iv];
  const value = btn.classList.contains("on") ? null : btn.dataset.oid;
  const wrap = $(".grid-wrap");
  GRID_SCROLL = wrap ? wrap.scrollLeft : null;   // keep our place across the re-render
  try {
    await post("/api/cell", { date: td.dataset.date, intervention_id: td.dataset.iv,
                              done: value != null, input_value: value, client_tonight: stateTonight() });
    if (cell) { cell.done = value != null; cell.input_value = value; }
    renderGrid();
  } catch (e) {
    if (await handleStaleTick(e)) return;
    alert("Could not save: " + e.message);
  }
}

// Quantity completion: a number (or "-" for done but not measured) marks the night
// done and is recorded; clearing the field un-marks it. Saved on change; the cell
// updates in place so focus isn't lost to a re-render.
async function saveCellQty(inp) {
  const td = inp.closest("td");
  const cell = (STATE.cells[td.dataset.date] || {})[td.dataset.iv];
  const v = inp.value.trim();
  if (v !== "" && v !== "-" && !Number.isFinite(Number(v))) {
    alert("Enter a number, “-” for done but not measured, or clear the field.");
    inp.value = cell && cell.input_value != null ? cell.input_value : "";
    return;
  }
  const value = v === "" ? null : v;
  try {
    await post("/api/cell", { date: td.dataset.date, intervention_id: td.dataset.iv,
                              done: value != null, input_value: value, client_tonight: stateTonight() });
    if (cell) { cell.done = value != null; cell.input_value = value; }
    td.classList.toggle("done", value != null);
  } catch (e) {
    if (await handleStaleTick(e)) return;
    alert("Could not save: " + e.message);
  }
}

async function saveMetric(inp) {
  const val = inp.value.trim();
  const metric = inp.dataset.metric;
  const num = val === "" ? null : parseFloat(val);
  const night = STATE.nights.find((n) => n.date === inp.dataset.date);
  try {
    await post("/api/metric", { date: inp.dataset.date, metric, value: num });
    if (night) { night[metric] = num; night[metric + "_src"] = num == null ? null : "manual"; }
    // reflect the source marker without a full re-render (which would drop focus)
    const td = inp.closest("td");
    td.classList.toggle("manual", num != null);
    inp.title = num == null ? "" : "entered by hand";
  } catch (e) {
    alert("Could not save: " + e.message);
  }
}

async function saveNote(t) {
  const night = STATE.nights.find((n) => n.date === t.dataset.date);
  try {
    await post("/api/note", { date: t.dataset.date, note: t.value });
    if (night) night.note = t.value.trim();
  } catch (e) {
    alert("Could not save note: " + e.message);
  }
}

// =====================================================================
// REPORTS
// =====================================================================
// enough logged nights to actually compare? (dose/boolean need on AND off;
// choice needs several option-nights). Below this we show no recommendation.
function enough(nOn, nOff, isChoice) {
  return isChoice ? nOn >= MIN_CHOICE : (nOn >= MIN_ON && nOff >= MIN_OFF);
}

// Higher bar, dose interventions only: crown a "recommended dose" only once there
// are enough on-nights and at least two distinct doses have actually been tried.
function enoughForDoseRec(r) {
  return (r.n_on || 0) >= MIN_ON_DOSE_REC && (r.n_doses_seen || 0) >= 2;
}

function verdict(r) {
  // need a contrast of on AND off logged nights before claiming anything
  if ((r.n_on || 0) < MIN_ON || (r.n_off || 0) < MIN_OFF) return ["v-new", "🌱 gathering data"];
  // We keep anything reliably positive, however SMALL -- so the verdict is driven
  // by P(effect > 0), not by any minimum effect size. (Small effects simply need
  // more nights before this probability climbs, which guards against false wins.)
  const help = r.p_helps ?? 0;
  if (help >= 0.95) return ["v-help", "✔ helps"];
  if (help >= 0.8) return ["v-maybe", "… leaning helpful"];
  if (help <= 0.2) return ["v-no", "✕ no benefit seen"];
  return ["v-maybe", "… inconclusive"];
}

// Whether recency weighting is on (mirrors the server-side setting; set by the
// last reports/interactions payload). When on, night counts shown on this page
// are effective (decayed) counts and are flagged as adjusted.
let RECENCY_ON = false;
let RECENCY_BUSY = false;   // a toggle request is in flight; ignore re-clicks until it lands
// Whether maximize-benefit (non-randomized) nights count toward results (mirrors
// the server-side setting; default off).
let COUNT_MAX_ON = false;
// Whether fits are conditioned on each night's local baseline (mirrors the
// server-side setting; default off). When on, the averages shown on this page are
// baseline-adjusted and are flagged as such.
let BASELINE_ON = false;
let BASELINE_DAYS = 7;
// label suffix for an average that is baseline-adjusted in this mode
function baseNote() { return BASELINE_ON ? " (baseline-adj.)" : ""; }
function baseFlag() {
  return BASELINE_ON
    ? ` <span class="adj-flag" title="Each night's value is compared with the average of the surrounding ±${BASELINE_DAYS} days, so a good or bad stretch isn't credited to whatever happened to be on. Averages shown are baseline-adjusted (value − its local baseline + the overall mean), in the usual units.">baseline-adjusted — averages are relative to each night's ±${BASELINE_DAYS}-day baseline</span>`
    : "";
}

async function loadReports() {
  const box = $("#reports-body");
  box.innerHTML = `<p class="empty">Crunching the numbers…</p>`;
  let data;
  try { data = await api("/api/reports"); }
  catch (e) { box.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`; return; }
  renderReports(data);
}

function renderReports(data) {
  const box = $("#reports-body");
  RECENCY_ON = !!(data.recency && data.recency.enabled);
  renderRecencySlot(data.recency, data.count_maximize, data.baseline);
  box.innerHTML = "";
  box.appendChild(el("p", "rep-stat", data.n_completed === 0
    ? "No nights logged yet — reports fill in as you enter your HRV on the Tracker."
    : `Based on <b>${data.n_completed}</b> logged night${data.n_completed === 1 ? "" : "s"}` +
      (RECENCY_ON ? ` <span class="adj-flag">recency-weighted — counts shown are adjusted</span>` : "") +
      baseFlag() +
      `. “Effect” is the change in HRV from taking the intervention, in your HRV units. Anything reliably positive is worth keeping — even a small effect.`));
  if (data.hrv_trend) box.appendChild(hrvTrendCard(data.hrv_trend));
  if (!data.interventions.length) {
    box.appendChild(el("p", "empty", "No interventions yet."));
    return;
  }
  for (const [cat, list] of groupByCategory(data.interventions)) {
    box.appendChild(el("h3", "rep-group", CATEGORY_LABEL[cat]));
    // active cards first (by name), then paused ones -- each paused card collapsed
    // to its header line unless you've opened it.
    const sorted = list.slice().sort((a, b) =>
      a.active === b.active ? a.name.localeCompare(b.name) : (a.active ? -1 : 1));
    for (const iv of sorted) box.appendChild(reportCard(iv));
  }
}

// ---- recency-weighting toggle (in the header, under the Tracker/Reports
// buttons) --------------------------------------------------------------------
// Server-wide mode: when on, every night is weighted by 0.5^(age/half-life) in
// BOTH the read-outs and the live bandit (Thompson planning, warm-up, lock-in
// guard). Values become weighted means, sample sizes become effective counts.
function renderRecencySlot(recency, countMax, baseline) {
  if (recency) RECENCY_ON = !!recency.enabled;
  if (countMax !== undefined) COUNT_MAX_ON = !!countMax;
  if (baseline) { BASELINE_ON = !!baseline.enabled; BASELINE_DAYS = baseline.window_days || BASELINE_DAYS; }
  const slot = $("#recency-slot");
  if (!slot) return;
  const hl = Math.round((recency && recency.half_life_days) || 30);
  const baseTip = `Each night is compared with its own local baseline: the average of the same metric over the ` +
    `${BASELINE_DAYS} days before and ${BASELINE_DAYS} after (the night itself excluded). The model takes that baseline ` +
    `as a covariate, so a good or bad stretch is read as drift rather than credited to whatever happened to be on ` +
    `— the fix for an intervention that locked in early and got its few off nights in one bad month. ` +
    `Averages on the Reports page become baseline-adjusted (flagged), and the nightly planner schedules from the same fit.`;
  const tip = `Each night's datapoint — on- and off-nights alike — is weighted by how recent it is, ` +
    `halving every ${hl} days. The weights scale both the measured values and the effective sample sizes ` +
    `(shown as adjusted counts on the Reports page), and the nightly planner schedules from the same ` +
    `weighted evidence. The early lock-in guard is off in this mode — decayed counts can't reach its ` +
    `night-count tiers, and fading evidence keeps the on/off contrast alive on its own. Ages count back ` +
    `from the most recent logged night.`;
  const maxTip = `Maximize-benefit nights are planned from the data rather than randomized. ` +
    `With this on, their results count toward the measured effects like any other night; ` +
    `off, they are recorded and charted but never feed the effect estimates or the planner.`;
  if (READ_ONLY) {
    slot.innerHTML = (RECENCY_ON
      ? `<span class="recency-badge" title="${esc(tip)}">⏳ recency weighting on · ${hl}-day half-life</span>` : "") +
      (COUNT_MAX_ON ? "" : `<span class="recency-badge" title="${esc(maxTip)}">⭐ maximize nights not counted</span>`) +
      (BASELINE_ON ? `<span class="recency-badge" title="${esc(baseTip)}">📐 baseline-adjusted · ±${BASELINE_DAYS} days</span>` : "");
    return;
  }
  slot.innerHTML =
    `<label class="recency-label" title="${esc(tip)}">` +
    `<span>⏳ Weight recent nights more</span>` +
    `<span class="switch"><input type="checkbox" id="recency-toggle"${RECENCY_ON ? " checked" : ""}>` +
    `<span class="knob"></span></span></label>` +
    `<label class="recency-label" title="${esc(maxTip)}">` +
    `<span>⭐ Count maximize nights</span>` +
    `<span class="switch"><input type="checkbox" id="count-max-toggle"${COUNT_MAX_ON ? " checked" : ""}>` +
    `<span class="knob"></span></span></label>` +
    `<label class="recency-label" title="${esc(baseTip)}">` +
    `<span>📐 Adjust for baseline drift</span>` +
    `<span class="switch"><input type="checkbox" id="baseline-toggle"${BASELINE_ON ? " checked" : ""}>` +
    `<span class="knob"></span></span></label>`;
  const restore = () => renderRecencySlot({ enabled: RECENCY_ON, half_life_days: hl }, COUNT_MAX_ON,
                                          { enabled: BASELINE_ON, window_days: BASELINE_DAYS });
  const refreshViews = (fresh) => {
    if (!$("#view-reports").hidden) {
      if (!$("#reports-body").hidden) renderReports(fresh);
      else loadInteractions();
    }
  };
  $("#baseline-toggle").addEventListener("change", async (e) => {
    if (RECENCY_BUSY) { e.target.checked = BASELINE_ON; return; }
    RECENCY_BUSY = true;
    const enabled = e.target.checked;
    e.target.disabled = true;
    try {
      const fresh = await post("/api/baseline", { enabled });   // returns the reports payload
      renderRecencySlot(fresh.recency, fresh.count_maximize, fresh.baseline);
      refreshViews(fresh);
    } catch (err) {
      alert("Could not switch baseline adjustment: " + err.message);
      restore();
    } finally {
      RECENCY_BUSY = false;
    }
  });
  $("#count-max-toggle").addEventListener("change", async (e) => {
    if (RECENCY_BUSY) { e.target.checked = COUNT_MAX_ON; return; }
    RECENCY_BUSY = true;
    const enabled = e.target.checked;
    e.target.disabled = true;
    try {
      const fresh = await post("/api/count_maximize", { enabled });   // returns the reports payload
      renderRecencySlot(fresh.recency, fresh.count_maximize, fresh.baseline);
      if (!$("#view-reports").hidden) refreshViews(fresh);
      else if (STATE) renderGrid();   // past maximize nights dim / undim
    } catch (err) {
      alert("Could not switch maximize-night counting: " + err.message);
      restore();
    } finally {
      RECENCY_BUSY = false;
    }
  });
  $("#recency-toggle").addEventListener("change", async (e) => {
    if (RECENCY_BUSY) { e.target.checked = RECENCY_ON; return; }   // a switch is already in flight
    RECENCY_BUSY = true;
    const enabled = e.target.checked;
    e.target.disabled = true;
    try {
      const fresh = await post("/api/recency", { enabled });   // returns the reports payload
      renderRecencySlot(fresh.recency, fresh.count_maximize, fresh.baseline);
      refreshViews(fresh);   // an open reports subview shows adjusted numbers — refresh it
    } catch (err) {
      alert("Could not switch recency weighting: " + err.message);
      restore();
    } finally {
      RECENCY_BUSY = false;
    }
  });
}

// a night count, flagged when it's a recency-adjusted effective count
function adjNote() { return RECENCY_ON ? " (adjusted)" : ""; }

// ---- early lock-in guard modal (opened from a report card's cap indicator) --
// Lets the user switch the scheduling clamp (bandit.LOCKIN_TIERS) off for one
// intervention — trading slower growth of the thin arm for scheduling that
// follows the measured probability right away — and back on again.
let capIv = null;

function openCapModal(iv) {
  capIv = iv;
  const armN = Math.min(iv.sched_n_on ?? 0, iv.sched_n_off ?? 0);
  const armSide = (iv.sched_n_on ?? 0) <= (iv.sched_n_off ?? 0) ? "on" : "off";
  const arm = `${armN} ${armSide}-night${armN === 1 ? "" : "s"}`;
  const raw = pct(iv.p_scheduled_raw);
  if (iv.no_lockin) {
    $("#cap-text").innerHTML = `The guard is <b>off</b> for <b>${esc(iv.name)}</b>: the planner follows the ` +
      `measured <b>${raw}</b> chance unclamped, even though its smaller arm (${arm}) is still thin.`;
    $("#cap-hint").textContent = "Turning it back on caps scheduling at 90% until the smaller arm has " +
      "15 nights, then 95% until it has 30 — so contrast nights keep being collected while the estimate is thin. " +
      "There is no floor: a low chance is always followed as measured.";
    $("#cap-toggle").textContent = "Turn the guard back on";
  } else {
    $("#cap-text").innerHTML = `The measured chance that <b>${esc(iv.name)}</b> helps is <b>${raw}</b>, but its ` +
      `smaller arm has only <b>${arm}</b>, so the planner caps scheduling at ` +
      `<b>${pct(iv.sched_tier)}</b>: a few noisy early nights can't lock the decision ` +
      `in, and the arm the estimate is short on keeps being sampled.`;
    $("#cap-hint").textContent = "Turning the guard off lets scheduling follow the measured chance immediately. " +
      "The cost: the under-sampled arm grows even more slowly, so the effect estimate stays uncertain for longer. " +
      "Scheduling only — the efficacy numbers are never affected either way.";
    $("#cap-toggle").textContent = "Turn the guard off";
  }
  $("#cap-backdrop").hidden = false;
}

function closeCapModal() { $("#cap-backdrop").hidden = true; capIv = null; }

$("#cap-close").addEventListener("click", closeCapModal);
$("#cap-cancel").addEventListener("click", closeCapModal);
$("#cap-backdrop").addEventListener("click", (e) => { if (e.target === $("#cap-backdrop")) closeCapModal(); });
$("#cap-toggle").addEventListener("click", async () => {
  if (!capIv) return;
  try {
    // pass dose_levels back so the update doesn't regenerate them (as togglePause does)
    STATE = await api(`/api/interventions/${capIv.id}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ no_lockin: !capIv.no_lockin, dose_levels: capIv.dose_levels }),
    });
  } catch (err) { alert("Error: " + err.message); return; }
  closeCapModal();
  loadReports();
});

// which paused report cards are expanded (default: collapsed), by intervention id
let REPORT_OPEN = {};
try { REPORT_OPEN = JSON.parse(localStorage.getItem("sb.reportOpen") || "{}") || {}; } catch (e) { REPORT_OPEN = {}; }

function reportCard(iv) {
  const card = reportCardBody(iv);
  if (iv.season) appendSeason(card, iv);
  appendTagFlags(card, iv);
  return card;
}

// ---- setting vs. outdoor temperature ------------------------------------------
// For temperature-dosed interventions (Chili Pad, air conditioning): every counted
// night plotted as outdoor night temperature vs HRV, colored by the setting, plus
// the nights binned by outdoor temperature with each setting's average -- does the
// best setting move with the season? (server.season_payload; weather.py)
function appendSeason(card, iv) {
  const sp = iv.season;
  const deg = sp.deg;
  // color follows the setting's VALUE, coolest blue through purple to warmest red
  // (hue 220 -> 360), so two nearby settings look alike and the extremes stand
  // apart; off nights are hollow grey
  const onDoses = sp.doses.filter((d) => d > 0);
  const dLo = Math.min(...onDoses), dHi = Math.max(...onDoses);
  const colorOf = (d) => d > 0
    ? `hsl(${Math.round(dHi > dLo ? 220 + 140 * (d - dLo) / (dHi - dLo) : 220)} 72% 62%)`
    : "var(--muted)";
  const fmtDose = (d) => d > 0 ? `${numLabel(d)} ${esc(iv.unit)}` : "off";

  const sec = el("div", "season");
  sec.appendChild(el("div", "season-head",
    `🌡 Setting vs. outdoor temperature <span class="rmeta">Outside air where you slept (${(sp.cities || []).map(esc).join(", ")}), averaged over your actual sleep window (Oura bedtime to wake; Open-Meteo), in ${deg} · ` +
    `${sp.n} night${sp.n === 1 ? "" : "s"}${BASELINE_ON ? " · HRV baseline-adj." : ""}</span>`));
  const row = el("div", "season-row");

  // --- scatter: x outdoor, y HRV, colored by setting; per-setting trend when ≥ 4 nights
  const pts = sp.points;
  const W = 460, H = 210, mL = 36, mR = 12, mT = 12, mB = 34;
  const pL = mL, pR = W - mR, pT = mT, pB = H - mB;
  let xlo = Math.min(...pts.map((p) => p.out)), xhi = Math.max(...pts.map((p) => p.out));
  let ylo = Math.min(...pts.map((p) => p.hrv)), yhi = Math.max(...pts.map((p) => p.hrv));
  if (xhi - xlo < 1) { xhi += 0.5; xlo -= 0.5; }
  if (yhi - ylo < 1) { yhi += 1; ylo -= 1; }
  const xpad = (xhi - xlo) * 0.06, ypad = (yhi - ylo) * 0.08;
  xlo -= xpad; xhi += xpad; ylo -= ypad; yhi += ypad;
  const px = (v) => pL + (v - xlo) / (xhi - xlo) * (pR - pL);
  const py = (v) => pT + (yhi - v) / (yhi - ylo) * (pB - pT);
  const parts = [];
  parts.push(`<line x1="${pL}" y1="${pB}" x2="${pR}" y2="${pB}" style="stroke:var(--line)"/>`);
  parts.push(`<line x1="${pL}" y1="${pT}" x2="${pL}" y2="${pB}" style="stroke:var(--line)"/>`);
  parts.push(`<text x="${pL - 5}" y="${pT + 4}" text-anchor="end" font-size="10" style="fill:var(--muted)">${Math.round(yhi)}</text>`);
  parts.push(`<text x="${pL - 5}" y="${pB}" text-anchor="end" font-size="10" style="fill:var(--muted)">${Math.round(ylo)}</text>`);
  parts.push(`<text x="${pL - 5}" y="${(pT + pB) / 2 + 3}" text-anchor="end" font-size="9.5" style="fill:var(--muted)">HRV</text>`);
  // bin boundaries (the cut-offs the table uses)
  const cuts = sp.bins.slice(1).map((b) => Number((b.label.match(/^([\d.]+)/) || [])[1])).filter((v) => !Number.isNaN(v));
  for (const c of cuts) parts.push(`<line x1="${px(c)}" y1="${pT}" x2="${px(c)}" y2="${pB}" stroke-dasharray="3 4" style="stroke:var(--line)"/>`);
  for (const v of [xlo + xpad, xhi - xpad]) parts.push(`<text x="${px(v)}" y="${pB + 13}" text-anchor="middle" font-size="10" style="fill:var(--muted)">${v.toFixed(1)}</text>`);
  for (const c of cuts) parts.push(`<text x="${px(c)}" y="${pB + 13}" text-anchor="middle" font-size="10" style="fill:var(--muted)">${c.toFixed(1)}</text>`);
  parts.push(`<text x="${(pL + pR) / 2}" y="${H - 3}" text-anchor="middle" font-size="9.5" style="fill:var(--muted)">outdoor night temperature, ${deg}</text>`);
  // trend per setting (least squares) when it has at least 4 nights
  for (const d of sp.doses) {
    const q = pts.filter((p) => p.dose === d);
    if (q.length < 4) continue;
    const mx = q.reduce((a, p) => a + p.out, 0) / q.length, my = q.reduce((a, p) => a + p.hrv, 0) / q.length;
    const sxx = q.reduce((a, p) => a + (p.out - mx) ** 2, 0);
    if (sxx < 1e-9) continue;
    const b = q.reduce((a, p) => a + (p.out - mx) * (p.hrv - my), 0) / sxx;
    const x0 = Math.min(...q.map((p) => p.out)), x1 = Math.max(...q.map((p) => p.out));
    parts.push(`<line x1="${px(x0)}" y1="${py(my + b * (x0 - mx))}" x2="${px(x1)}" y2="${py(my + b * (x1 - mx))}" ` +
      `stroke-width="1.5" style="stroke:${colorOf(d)};opacity:.55"${d === 0 ? ' stroke-dasharray="4 3"' : ""}/>`);
  }
  for (const p of pts) {
    const col = colorOf(p.dose);
    const title = `${shortDate(p.date)} · ${fmtDose(p.dose)} · HRV ${p.hrv.toFixed(1)} · ${p.city ? p.city + " " : ""}${p.out.toFixed(1)}${deg} outside`;
    parts.push(p.dose > 0
      ? `<circle cx="${px(p.out)}" cy="${py(p.hrv)}" r="4.5" style="fill:${col};stroke:var(--panel)" stroke-width="1.5"><title>${esc(title)}</title></circle>`
      : `<circle cx="${px(p.out)}" cy="${py(p.hrv)}" r="4" fill="none" style="stroke:${col}" stroke-width="1.6"><title>${esc(title)}</title></circle>`);
  }
  const chart = el("div", "season-chart",
    `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;display:block" role="img" ` +
    `aria-label="Outdoor night temperature versus HRV for ${esc(iv.name)}, colored by setting">${parts.join("")}</svg>`);
  const legend = el("div", "season-legend");
  legend.innerHTML = sp.doses.map((d) =>
    `<span class="season-key"><i style="${d > 0 ? "background:" + colorOf(d) : "border:1.5px solid var(--muted)"}"></i>${fmtDose(d)}` +
    `<span class="muted"> ${sp.dose_counts[String(Number(d).toFixed(1))] || ""}</span></span>`).join("");
  chart.appendChild(legend);
  row.appendChild(chart);

  // --- table: outdoor bins × settings (settings with ≥ 2 nights overall)
  const cols = sp.doses.filter((d) => (sp.dose_counts[String(Number(d).toFixed(1))] || 0) >= 2);
  const t = el("table", "season-table");
  let h = `<tr><th>Outdoor night</th>`;
  for (const d of cols) h += `<th style="color:${colorOf(d)}">${fmtDose(d)}</th>`;
  h += `</tr>`;
  for (const b of sp.bins) {
    h += `<tr><th>${esc(b.label)}<span class="rmeta">${b.n} night${b.n === 1 ? "" : "s"}</span></th>`;
    for (const d of cols) {
      const c = b.cells[String(Number(d).toFixed(1))];
      const best = b.best != null && Number(b.best) === d;
      h += `<td class="${best ? "best" : ""}" title="${c ? `${c.n} night${c.n === 1 ? "" : "s"} at this setting in this outdoor range` : "no nights"}">` +
        (c ? `${best ? "★ " : ""}<b>${avg1(c.mean)}</b><span class="muted"> ${c.n}</span>` : `<span class="muted">—</span>`) + `</td>`;
    }
    h += `</tr>`;
  }
  t.innerHTML = h;
  row.appendChild(t);
  sec.appendChild(row);

  // --- does the best setting move with the weather? (dose × outdoor interaction)
  const it = sp.interaction;
  let note;
  if (it) {
    const lo = it.coef - 1.645 * it.se, hi = it.coef + 1.645 * it.se;
    const clear = lo > 0 || hi < 0;
    const s1 = (x) => Math.abs(x) < 0.05 ? "0.0" : signed(x);   // no "-0.0"
    note = `Average HRV per bin, ★ = best setting with at least 2 nights. Interaction fit on the ${it.n} on-nights: ` +
      `each extra outdoor degree changes the per-degree effect of the setting by <b>${s1(it.coef)}</b> ms ` +
      `(90% CI ${s1(lo)} to ${s1(hi)}) — ` +
      (clear ? `<b class="${it.coef > 0 ? "good" : "bad"}">the best setting does look weather-dependent</b>.`
             : `no sign yet that the best setting moves with the weather.`);
  } else {
    note = `Average HRV per bin, ★ = best setting with at least 2 nights. Too few on-nights across settings to test whether the best setting moves with the weather.`;
  }
  sec.appendChild(el("p", "season-note", note));
  card.appendChild(sec);
}

// Night-shape associations, at the foot of the card: a tag this intervention's
// nights carry disproportionately often (or rarely) vs its control nights --
// Fisher exact test on raw counts (server.tag_associations). Flagged either way.
function appendTagFlags(card, iv) {
  for (const f of iv.tag_flags || []) {
    const times = f.ratio == null ? "" : `${(f.ratio >= 1 ? f.ratio : 1 / f.ratio).toFixed(1)}× `;
    const tip = `Among this intervention's ${f.on_n} confirmed-on nights, ${f.on_tag} were tagged “${tagLabel(f.slug)}”; ` +
      `among its ${f.off_n} assigned-off (control) nights, ${f.off_tag}. Two-sided Fisher exact p = ${f.p}. ` +
      `Only low-HRV nights get tags, so an intervention that simply lowers HRV picks up every tag — ` +
      `restricted to low nights the split is ${f.low_on_tag} of ${f.low_on_n} on vs ${f.low_off_tag} of ${f.low_off_n} off.`;
    card.appendChild(el("p", "iv-tagflag",
      `🏷 Tagged ${tagChip(f.slug, tip)} <b class="${f.direction === "more" ? "bad" : "good"}">${times}${f.direction === "more" ? "more" : "less"} often:</b> ` +
      `${f.on_tag} of ${f.on_n} on-nights vs ${f.off_tag} of ${f.off_n} off-nights, p = ${f.p}.`));
  }
}

function reportCardBody(iv) {
  const r = iv.readout || {};
  const isChoice = isChoiceKind(iv);
  const nOn = r.n_on || 0, nOff = r.n_off || 0, nObs = nOn + nOff;
  const card = el("div", "iv" + (iv.active ? "" : " inactive"));

  const top = el("div", "iv-top");
  if (!iv.active) {
    // paused: the whole header is a toggle; only the header shows while collapsed
    const open = !!REPORT_OPEN[iv.id];
    card.classList.toggle("collapsed", !open);
    const chev = el("span", "iv-chev", open ? "▾" : "▸");
    top.appendChild(chev);
    top.classList.add("toggle");
    top.title = "Paused — click to " + (open ? "collapse" : "expand");
    top.addEventListener("click", () => {
      const nowOpen = card.classList.contains("collapsed");
      card.classList.toggle("collapsed", !nowOpen);
      chev.textContent = nowOpen ? "▾" : "▸";
      top.title = "Paused — click to " + (nowOpen ? "collapse" : "expand");
      REPORT_OPEN[iv.id] = nowOpen;
      try { localStorage.setItem("sb.reportOpen", JSON.stringify(REPORT_OPEN)); } catch (e) { /* ignore */ }
    });
  }
  top.appendChild(el("span", "iv-name", esc(iv.name)));
  if (isChoice && !iv.test_skip) {
    // A plain choice (no skip baseline) can't say "helps vs nothing", so it only
    // flags gathering-data; a skip-testing choice earns a real verdict like a dose.
    if (nOn < 4) top.appendChild(el("span", "verdict v-new", "🌱 gathering data"));
  } else {
    const [vc, vt] = verdict(r);
    top.appendChild(el("span", "verdict " + vc, vt));
  }
  top.appendChild(el("span", "iv-meta",
    iv.kind === "choice_dose" ? `${liveOptions(iv).length} options × dose · standard ${doseAmount(iv, iv.default_dose)}`
    : isChoice ? `${liveOptions(iv).length} options`
    : iv.kind === "boolean" ? "yes / no"
    : `standard ${doseAmount(iv, iv.default_dose)}`));
  card.appendChild(top);

  // Scheduling probability: how often the planner will put this on. Includes the prior
  // (unlike the efficacy numbers below) and converges to the measured chance over time.
  if (!iv.active) {
    card.appendChild(el("p", "iv-sched muted", "🗓 Paused — not currently scheduled."));
  } else if (iv.p_scheduled != null) {
    // Early lock-in guard: while the smaller arm is thin the planner caps this
    // probability (<15 nights: 90%, <30: 95%; never floored). Say so when it actually bit —
    // or when the guard is switched off for this intervention while it would
    // otherwise apply. Clicking the marker opens a box to toggle the guard.
    let capNote = "";
    if (iv.sched_cap != null || (iv.no_lockin && iv.sched_tier != null)) {
      const arm = iv.sched_n_on <= iv.sched_n_off ? `${iv.sched_n_on} on-night${iv.sched_n_on === 1 ? "" : "s"}`
                                                  : `${iv.sched_n_off} off-night${iv.sched_n_off === 1 ? "" : "s"}`;
      if (iv.sched_cap != null) {
        const bound = pct(iv.sched_cap);
        capNote = ` <span class="sched-cap" title="Measured chance is ${pct(iv.p_scheduled_raw)}, but with only ${arm} so far ` +
          `the planner caps scheduling at ${bound} so a few noisy early nights can't lock the decision in. ` +
          `The cap is 90% until the smaller arm has 15 nights, then 95% until it has 30. There is no floor.` +
          `${READ_ONLY ? "" : " Click to adjust."}">` +
          `capped at ${bound} · ${arm}</span>`;
      } else {
        capNote = ` <span class="sched-cap cap-off" title="The early lock-in guard is switched off for this intervention: ` +
          `scheduling follows the measured ${pct(iv.p_scheduled_raw)} unclamped even though the smaller arm has only ${arm}.` +
          `${READ_ONLY ? "" : " Click to turn it back on."}">guard off · ${arm}</span>`;
      }
    }
    const sched = el("p", "iv-sched", `🗓 <b>Scheduling tomorrow night: ${pct(iv.p_scheduled)}</b>${capNote}`);
    const capEl = sched.querySelector(".sched-cap");
    if (capEl && !READ_ONLY) {
      capEl.classList.add("clickable");
      capEl.addEventListener("click", () => openCapModal(iv));
    }
    card.appendChild(sched);
  }

  const lObs = (iv.readout_lrhr?.n_on || 0) + (iv.readout_lrhr?.n_off || 0);

  // No real data on EITHER metric -> stay empty instead of showing prior noise.
  if (nObs === 0 && lObs === 0) {
    card.appendChild(el("p", "iv-meta",
      "No logged nights yet — enter your HRV or resting HR on the Tracker and this report will fill in."));
    return card;
  }

  let lrhrHandled = false;
  if (nObs > 0) {
    const dataNote = `${nOn} night${nOn === 1 ? "" : "s"} on, ${nOff} off${adjNote()}.`;
    // The hero "any option vs skipping" numbers need an on/off contrast, exactly like
    // a dose intervention; a plain choice (no skip) just needs enough option-nights.
    const skipChoice = isChoice && iv.test_skip;
    const ready = skipChoice ? (nOn >= MIN_ON && nOff >= MIN_OFF) : enough(nOn, nOff, isChoice);
    if (!ready) {
      const need = skipChoice ? `at least ${MIN_ON} on and ${MIN_OFF} off`
        : isChoice ? `at least ${MIN_CHOICE} option-nights`
        : `at least ${MIN_ON} on and ${MIN_OFF} off`;
      card.appendChild(el("p", "iv-meta",
        `📈 <b>HRV</b>: gathering data — ${dataNote} Needs ${need} before it can compare.`));
    } else if (skipChoice) {
      // Skip-testing choice: SAME hero layout as a dose intervention. The big numbers
      // are the average effect of doing ANY option vs skipping (HRV + resting HR); the
      // side chart is a bar graph comparing each option against skipping.
      const body = el("div", "iv-2col");
      const left = el("div", "iv-metrics");
      left.appendChild(hrvMetric(r, dataNote, iv));
      left.appendChild(rhrMetric(iv));
      lrhrHandled = true;
      body.appendChild(left);

      const right = el("div", "iv-graph");
      if ((r.doses || []).length) {
        right.appendChild(el("p", "chart-cap",
          `Option → HRV change vs skipping${baseNote()} · <span class="mk-dot">●</span> raw mean · <span class="mk-ci">│</span> model 90% CI`));
        right.appendChild(optionChart(iv, r.doses, r));
      }
      body.appendChild(right);
      card.appendChild(body);
    } else if (isChoice) {
      // Plain multiple-choice (no skip option): horse-race table of the options.
      // Always on, so there's no baseline to measure an effect against — show each
      // option's raw average HRV and resting HR instead.
      const doses = (r.doses || []).slice().sort((a, b) => b.p_best - a.p_best);
      const lrByOpt = new Map((iv.readout_lrhr?.doses || []).map((d) => [Number(d.dose).toFixed(6), d]));
      const best = optLabel(iv, r.recommended_dose);
      // when the card also has an observed-behavior section below, label this
      // one explicitly as the randomized comparison
      if ((iv.observational || []).some((o) => o.hrv_n > 0 || o.lrhr_n > 0)) {
        card.appendChild(el("p", "sec-head", "🎲 Randomized — by assigned option"));
      }
      card.appendChild(el("p", "iv-meta",
        (best ? `📈 Best for HRV: <b>${esc(best)}</b> — each option's average across its nights. ` : "📈 ") +
        `Based on ${dataNote}`));
      if (doses.length) {
        const table = el("table", "dose-table");
        table.innerHTML = `<tr><th>Option</th><th>Avg HRV${baseNote()}</th><th>Avg resting HR${baseNote()}</th><th>P(best)</th><th></th></tr>`;
        for (const d of doses) {
          const rec = sameLevel(d.dose, r.recommended_dose) && r.recommended_dose > 0;
          table.appendChild(choiceRow(d, lrByOpt.get(Number(d.dose).toFixed(6)),
            esc(optLabel(iv, d.dose) || "(removed)"), rec));
        }
        card.appendChild(table);
      }
    } else {
      // Dose intervention with enough data: big "does it work" numbers (HRV +
      // resting HR) on the left, the dose graph on the right, ~equal width. The
      // headline effect is what matters; dose response is the secondary detail.
      const body = el("div", "iv-2col");
      const left = el("div", "iv-metrics");
      left.appendChild(hrvMetric(r, dataNote, iv));
      left.appendChild(rhrMetric(iv));
      lrhrHandled = true;
      body.appendChild(left);

      const right = el("div", "iv-graph");
      if ((r.doses || []).length) {
        const showRec = enoughForDoseRec(r);
        right.appendChild(el("p", "chart-cap",
          `Dose → HRV change vs off nights${baseNote()} · <span class="mk-dot">●</span> raw mean · <span class="mk-ci">│</span> model 90% CI`));
        right.appendChild(doseChart(iv, r.doses, r, showRec));
        if (!showRec) {
          right.appendChild(el("p", "iv-meta",
            "No dose recommended yet — needs more on-nights across at least two different doses."));
        }
      }
      body.appendChild(right);
      card.appendChild(body);
    }
  }

  // Observed-behavior section: raw averages grouped by what ACTUALLY happened
  // each night (recorded button first, behavior-pinning option otherwise) —
  // assigned or chosen, so this section is correlational, not randomized.
  const obs = (iv.observational || []);
  if (obs.some((o) => o.hrv_n > 0 || o.lrhr_n > 0)) {
    const head = el("p", "sec-head", "👁 Observed — by what actually happened");
    const flag = el("span", "obs-flag", "correlational");
    flag.title = "Nights are grouped by the behavior that actually occurred, whether it was " +
      "assigned or your own choice — so differences here can reflect why you chose it (e.g. " +
      "eating because it was a rough day), not just its effect. Treat gaps as leads, not verdicts.";
    head.appendChild(flag);
    card.appendChild(head);
    const t = el("table", "dose-table");
    t.innerHTML = `<tr><th>Behavior</th><th>Avg HRV${baseNote()}</th><th>Avg resting HR${baseNote()}</th><th>P(best)</th><th></th></tr>`;
    t.querySelectorAll("th")[3].title = "Chance this behavior has the highest average HRV of the " +
      "behaviors listed, given the night-to-night noise in your data. Ranks averages only — " +
      "it inherits the correlational caveat above.";
    const nN = (n) => ` <span class="ci">(${n} night${n === 1 ? "" : "s"}${RECENCY_ON ? ", adj." : ""})</span>`;
    // best-first, like the randomized table; behaviors with no HRV nights sink to the bottom
    const rows = obs.slice().sort((a, b) => (b.hrv_p_best ?? -1) - (a.hrv_p_best ?? -1));
    for (const o of rows) {
      const tr = el("tr");
      tr.appendChild(el("td", null, esc(o.name.replace(" (actual)", ""))));
      tr.appendChild(el("td", null, o.hrv_mean != null
        ? `<b>${avg1(o.hrv_mean)}</b>${nN(o.hrv_n)}` : '<span class="ci">—</span>'));
      const rhr = el("td", null, o.lrhr_mean != null
        ? `<b>${avg1(o.lrhr_mean)}</b> bpm${nN(o.lrhr_n)}` : '<span class="ci">—</span>');
      if (o.lrhr_p_best != null) rhr.title = `${pct(o.lrhr_p_best)} chance this is the lowest resting HR of the behaviors listed`;
      tr.appendChild(rhr);
      const barTd = el("td");
      if (o.hrv_p_best != null) {
        const bar = el("div", "bar"); const span = el("span"); span.style.width = Math.round(o.hrv_p_best * 100) + "%";
        bar.appendChild(span); barTd.appendChild(bar);
      }
      tr.appendChild(barTd);
      tr.appendChild(el("td", "pbest", o.hrv_p_best != null ? pct(o.hrv_p_best) : "—"));
      t.appendChild(tr);
    }
    card.appendChild(t);
  }

  if (!lrhrHandled) appendLrhr(card, iv);
  appendTst(card, iv);
  appendLat(card, iv);
  appendEff(card, iv);
  return card;
}

// Sleep-onset latency line (minutes to fall asleep, LOWER is better). The fit is on
// -latency, so flip back to report a real change in minutes.
function appendLat(card, iv) {
  const re = iv.readout_lat || {};
  const isChoice = isChoiceKind(iv);
  if (isChoice && !iv.test_skip) return;
  const nOn = re.n_on || 0, nOff = re.n_off || 0;
  if (nOn + nOff === 0) return;
  const ready = isChoice ? (nOn >= MIN_ON && nOff >= MIN_OFF) : enough(nOn, nOff, false);
  if (!ready || re.on_effect == null) {
    card.appendChild(el("p", "iv-meta eff-line",
      `⏱ <b>Sleep latency</b>: gathering data — ${nOn} night${nOn === 1 ? "" : "s"} on, ${nOff} off${adjNote()}.`));
    return;
  }
  const delta = (re.on_mean != null && re.off_mean != null)
    ? re.on_mean - re.off_mean : -re.on_effect;                            // minutes, real sign
  const lo = -re.on_ci_high, hi = -re.on_ci_low;
  const cls = delta <= 0 ? "pos" : "neg";
  const label = isChoice ? "Sleep latency — any option vs skipping" : "Sleep latency";
  const avgs = (re.on_mean != null && re.off_mean != null)
    ? ` Avg${baseNote()} <b>${avg1(re.on_mean)} min</b> on / <b>${avg1(re.off_mean)} min</b> off.` : "";
  card.appendChild(el("p", "iv-meta eff-line",
    `⏱ <b>${label}</b>: <span class="effect ${cls}">${signed(delta)}</span> min ` +
    `model CI <span class="ci">[${signed(lo)}, ${signed(hi)}]</span> ` +
    `(lower is better) — <b>${pct(re.p_helps)}</b> chance it shortens time to fall asleep.` + avgs));
}

// Total-sleep-time line (hours asleep; effect is in hours, higher is better).
// Same compact form as the sleep-efficiency line, and shown just above it.
function appendTst(card, iv) {
  const re = iv.readout_tst || {};
  const isChoice = isChoiceKind(iv);
  if (isChoice && !iv.test_skip) return;
  const nOn = re.n_on || 0, nOff = re.n_off || 0;
  if (nOn + nOff === 0) return;
  const ready = isChoice ? (nOn >= MIN_ON && nOff >= MIN_OFF) : enough(nOn, nOff, false);
  if (!ready || re.on_effect == null) {
    card.appendChild(el("p", "iv-meta eff-line",
      `🛌 <b>Total sleep</b>: gathering data — ${nOn} night${nOn === 1 ? "" : "s"} on, ${nOff} off${adjNote()}.`));
    return;
  }
  const eff = rawEff(re);
  const cls = eff >= 0 ? "pos" : "neg";
  const label = isChoice ? "Total sleep — any option vs skipping" : "Total sleep";
  const hm = (h) => { const m = Math.round(h * 60); return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`; };
  const mins = (h) => (h >= 0 ? "+" : "−") + Math.round(Math.abs(h) * 60) + " min";
  const avgs = (re.on_mean != null && re.off_mean != null)
    ? ` Avg${baseNote()} <b>${hm(re.on_mean)}</b> on / <b>${hm(re.off_mean)}</b> off.` : "";
  card.appendChild(el("p", "iv-meta eff-line",
    `🛌 <b>${label}</b>: <span class="effect ${cls}">${mins(eff)}</span> ` +
    `model CI <span class="ci">[${mins(re.on_ci_low)}, ${mins(re.on_ci_high)}]</span> ` +
    `(higher is better) — <b>${pct(re.p_helps)}</b> chance it lengthens your sleep.` + avgs));
}

// Sleep-efficiency line (percent of time in bed spent asleep; effect is in
// percentage points, higher is better). Compact like the resting-HR line -- HRV
// stays the headline metric.
function appendEff(card, iv) {
  const re = iv.readout_eff || {};
  const isChoice = isChoiceKind(iv);
  // For a choice, the effect is "any option vs skipping" -- needs a skip baseline.
  if (isChoice && !iv.test_skip) return;
  const nOn = re.n_on || 0, nOff = re.n_off || 0;
  if (nOn + nOff === 0) return;
  const ready = isChoice ? (nOn >= MIN_ON && nOff >= MIN_OFF) : enough(nOn, nOff, false);
  if (!ready || re.on_effect == null) {
    card.appendChild(el("p", "iv-meta eff-line",
      `😴 <b>Sleep efficiency</b>: gathering data — ${nOn} night${nOn === 1 ? "" : "s"} on, ${nOff} off${adjNote()}.`));
    return;
  }
  const eff = rawEff(re);
  const cls = eff >= 0 ? "pos" : "neg";
  const label = isChoice ? "Sleep efficiency — any option vs skipping" : "Sleep efficiency";
  const avgs = (re.on_mean != null && re.off_mean != null)
    ? ` Avg${baseNote()} <b>${avg1(re.on_mean)}%</b> on / <b>${avg1(re.off_mean)}%</b> off.` : "";
  card.appendChild(el("p", "iv-meta eff-line",
    `😴 <b>${label}</b>: <span class="effect ${cls}">${signed(eff)}</span> pp ` +
    `model CI <span class="ci">[${signed(re.on_ci_low)}, ${signed(re.on_ci_high)}]</span> ` +
    `(higher is better) — <b>${pct(re.p_helps)}</b> chance it improves your sleep efficiency.` + avgs));
}

// Displayed effects are RAW on-vs-off differences (unbiased under randomized
// assignment; no prior/shrinkage). The model still runs underneath: CIs,
// P(helps), verdicts, recommendations and scheduling are all posterior-based,
// so a raw number can sit outside its model CI — that gap IS the model's
// skepticism about thin data. Falls back to the modeled effect if a raw mean
// is missing (shouldn't happen where these render).
const rawEff = (r) => (r && r.on_mean != null && r.off_mean != null)
  ? r.on_mean - r.off_mean : (r ? r.on_effect : null);
const rawDoseEff = (d, r) => (d.raw_mean != null && r.off_mean != null)
  ? d.raw_mean - r.off_mean : d.mean_effect;

// Big "does HRV improve?" number for a dose intervention (left column): the raw
// on-vs-off difference. (The per-dose story, including the recommended dose's
// own raw effect, lives in the chart beside it — its ringed dot.)
function hrvMetric(r, dataNote, iv) {
  const eff = rawEff(r);
  const cls = eff >= 0 ? "pos" : "neg";
  const sizable = r.p_meaningful >= 0.85 ? ' · <span class="metric-flag">likely sizable</span>' : "";
  const b = el("div", "metric-big");
  b.appendChild(el("div", "metric-label", '📈 HRV <span class="metric-hint">higher is better</span>'));
  b.appendChild(el("div", "metric-val " + cls, signed(eff)));
  b.appendChild(el("div", "metric-sub",
    `model 90% CI <span class="ci">[${signed(r.on_ci_low)}, ${signed(r.on_ci_high)}]</span> · ` +
    `<b>${pct(r.p_helps)}</b> chance of a real benefit${sizable}`));
  if (r.on_mean != null && r.off_mean != null)
    b.appendChild(el("div", "metric-avgs",
      `Avg HRV${baseNote()} <b>${avg1(r.on_mean)}</b> on nights · <b>${avg1(r.off_mean)}</b> off`));
  b.appendChild(el("div", "metric-note", `Based on ${dataNote}`));
  return b;
}

// Big resting-HR hero number (left column). LRHR fit is on -LRHR, so flip back to
// bpm; a negative change (lower resting HR) is good -> green.
function rhrMetric(iv) {
  const rl = iv.readout_lrhr || {};
  const nOn = rl.n_on || 0, nOff = rl.n_off || 0;
  const b = el("div", "metric-big");
  b.appendChild(el("div", "metric-label", '🫀 Resting HR <span class="metric-hint">lower is better</span>'));
  // The hero number is an on-vs-off (choice: any-option-vs-skip) aggregate, so it
  // needs BOTH arms -- enough on AND enough off nights -- for either kind.
  if (nOn + nOff === 0 || !(nOn >= MIN_ON && nOff >= MIN_OFF) || rl.on_effect == null) {
    b.appendChild(el("div", "metric-val muted", "—"));
    b.appendChild(el("div", "metric-sub", `gathering data — ${nOn} on, ${nOff} off${adjNote()}`));
    return b;
  }
  // raw difference in real bpm (on_mean/off_mean are un-negated real values)
  const effBpm = (rl.on_mean != null && rl.off_mean != null)
    ? rl.on_mean - rl.off_mean : -rl.on_effect;
  const cls = effBpm <= 0 ? "pos" : "neg";
  b.appendChild(el("div", "metric-val " + cls, `${signed(effBpm)}<span class="metric-unit"> bpm</span>`));
  b.appendChild(el("div", "metric-sub",
    `model 90% CI <span class="ci">[${signed(-rl.on_ci_high)}, ${signed(-rl.on_ci_low)}]</span> · ` +
    `<b>${pct(rl.p_helps)}</b> chance it lowers`));
  if (rl.on_mean != null && rl.off_mean != null)
    b.appendChild(el("div", "metric-avgs",
      `Avg resting HR${baseNote()} <b>${avg1(rl.on_mean)}</b> on nights · <b>${avg1(rl.off_mean)}</b> off`));
  return b;
}

// Resting-HR line (LRHR fit is on -LRHR, so flip the sign back to bpm; negative = lower = good)
function appendLrhr(card, iv) {
  const rl = iv.readout_lrhr || {};
  const isChoice = isChoiceKind(iv);
  // For a choice, the resting-HR effect is "any option vs skipping" -- only meaningful
  // when it's testing the skip option and has some control nights.
  if (isChoice && !iv.test_skip) return;
  const nOn = rl.n_on || 0, nOff = rl.n_off || 0;
  if (nOn + nOff === 0) return;
  const ready = isChoice ? (nOn >= MIN_ON && nOff >= MIN_OFF) : enough(nOn, nOff, false);
  if (!ready || rl.on_effect == null) {
    card.appendChild(el("p", "iv-meta",
      `🫀 <b>Resting HR</b>: gathering data — ${nOn} night${nOn === 1 ? "" : "s"} on, ${nOff} off${adjNote()}.`));
    return;
  }
  const effBpm = (rl.on_mean != null && rl.off_mean != null)
    ? rl.on_mean - rl.off_mean : -rl.on_effect;
  const cls = effBpm <= 0 ? "pos" : "neg";  // lower resting HR is good -> green
  const label = isChoice ? "Resting HR — any option vs skipping" : "Resting HR";
  const avgs = (rl.on_mean != null && rl.off_mean != null)
    ? ` Avg${baseNote()} <b>${avg1(rl.on_mean)}</b> on / <b>${avg1(rl.off_mean)}</b> off bpm.` : "";
  card.appendChild(el("p", "iv-meta",
    `🫀 <b>${label}</b>: <span class="effect ${cls}">${signed(effBpm)}</span> bpm ` +
    `model CI <span class="ci">[${signed(-rl.on_ci_high)}, ${signed(-rl.on_ci_low)}]</span> ` +
    `(lower is better) — <b>${pct(rl.p_helps)}</b> chance it lowers your resting HR.` + avgs));
}

// Row for an always-on choice: raw per-option averages (HRV, resting HR in bpm)
// plus the chance it's the best option. `lr` is the matching readout_lrhr dose row.
function choiceRow(d, lr, label, rec) {
  const tr = el("tr", rec ? "rec" : "");
  tr.appendChild(el("td", null, label + (rec ? ' <span class="rec-tag">◀ recommended</span>' : "")));
  const nNote = (n) => ` <span class="ci">(${n} night${n === 1 ? "" : "s"})</span>`;
  tr.appendChild(el("td", null, d.raw_mean != null
    ? `<b>${avg1(d.raw_mean)}</b>${nNote(d.n_nights)}` : '<span class="ci">—</span>'));
  tr.appendChild(el("td", null, lr && lr.raw_mean != null
    ? `<b>${avg1(lr.raw_mean)}</b> bpm${nNote(lr.n_nights)}` : '<span class="ci">—</span>'));
  const barTd = el("td");
  const bar = el("div", "bar"); const span = el("span"); span.style.width = Math.round(d.p_best * 100) + "%";
  bar.appendChild(span); barTd.appendChild(bar); tr.appendChild(barTd);
  tr.appendChild(el("td", "pbest", pct(d.p_best)));
  return tr;
}

function doseRow(d, label, rec, isOff) {
  const tr = el("tr", rec ? "rec" : "");
  tr.appendChild(el("td", null, label + (rec ? ' <span class="rec-tag">◀ recommended</span>' : "")));
  if (isOff) tr.appendChild(el("td", null, '<span class="ci">baseline</span>'));
  else {
    const cls = d.mean_effect >= 0 ? "pos" : "neg";
    tr.appendChild(el("td", null,
      `<span class="effect ${cls}">${signed(d.mean_effect)}</span> <span class="ci">[${signed(d.ci_low)}, ${signed(d.ci_high)}]</span>`));
  }
  const barTd = el("td");
  const bar = el("div", "bar"); const span = el("span"); span.style.width = Math.round(d.p_best * 100) + "%";
  bar.appendChild(span); barTd.appendChild(bar); tr.appendChild(barTd);
  tr.appendChild(el("td", "pbest", pct(d.p_best)));
  return tr;
}

// Custom chart tooltip. Native SVG <title> tooltips proved unreliable, so any
// chart mark group carrying data-tip gets a floating div that appears instantly
// on hover and follows the cursor.
const chartTip = el("div", "chart-tip");
chartTip.hidden = true;
document.body.appendChild(chartTip);
document.addEventListener("mouseover", (e) => {
  // SVG mark groups only -- the form's ⓘ hints also carry data-tip but render
  // their own CSS bubble (.tip::after), so matching them here would double up
  const g = e.target.closest && e.target.closest("g[data-tip]");
  if (!g) { chartTip.hidden = true; return; }
  chartTip.textContent = g.getAttribute("data-tip");
  chartTip.hidden = false;
});
document.addEventListener("mousemove", (e) => {
  if (chartTip.hidden) return;
  const pad = 14;
  const r = chartTip.getBoundingClientRect();
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + r.width > window.innerWidth - 8) x = e.clientX - r.width - pad;
  if (y + r.height > window.innerHeight - 8) y = e.clientY - r.height - pad;
  chartTip.style.left = x + "px";
  chartTip.style.top = y + "px";
});

// Dose axis tick + unit caption, in the native unit (mg/g/…), never pills.
// The open-ended arm reads as "[max]+" (e.g. "30+"): beyond the max, unbounded.
function doseTick(iv, dose) {
  if (isOpenDose(dose)) return iv.max_dose > 0 ? numLabel(iv.max_dose) + "+" : "∞";   // unit is in the caption
  return numLabel(dose);
}
function doseUnitCaption(iv) {
  return iv.unit || "";
}
// Greedy word-wrap to at most maxChars per line and maxLines lines (the last line
// is ellipsised if the text runs on). Used for the open arm's tick label.
function wrapLabel(text, maxChars, maxLines) {
  const words = String(text).trim().split(/\s+/).filter(Boolean);
  const lines = [];
  let cur = "";
  for (const w of words) {
    if (!cur) cur = w;
    else if ((cur + " " + w).length <= maxChars) cur += " " + w;
    else { lines.push(cur); cur = w; }
  }
  if (cur) lines.push(cur);
  if (lines.length > maxLines) {
    const kept = lines.slice(0, maxLines);
    kept[maxLines - 1] = kept[maxLines - 1].slice(0, Math.max(1, maxChars - 1)) + "…";
    return kept;
  }
  return lines;
}

// Dose interventions get a graph, not a horse-race table: dose on x, mean HRV
// change over baseline on y, with a 90% credible-interval whisker per dose (the
// uncertainty matters — it's huge early). Dots are colored by sign, baseline at 0.
function doseChart(iv, rows, r, showRec) {
  const n = rows.length;
  // the open-ended arm (always the last, highest "dose") sits a bit apart from the
  // numeric doses: an extra gap before it says "beyond the max, not one more step"
  const hasOpen = rows.some((d) => isOpenDose(d.dose));
  const GAP = hasOpen ? 0.6 : 0;                 // in column widths
  // fill the report column (it's ~half the card); only grow past that for many doses
  const W = Math.max(CHART_MIN_W, 54 + (n + GAP) * 72);
  const mL = 42, mR = 44, mT = 16;            // right margin fits the last value label
  const pL = mL, pR = W - mR, pW = pR - pL;
  // the open-ended arm's tick is its own label ("no timer — stay on until I fall
  // asleep"), word-wrapped to its slot; the chart grows downward to fit the lines
  const slotW0 = pW / (n + GAP);
  const openLines = hasOpen
    ? wrapLabel(openLabel(iv), Math.max(10, Math.floor(2 * Math.min(0.8 * slotW0 - 12, slotW0 / 2 + 40) / 5.4)), 3)
    : [];
  const mB = 30 + Math.max(0, openLines.length - 1) * 11, H = 170 + Math.max(0, openLines.length - 1) * 11;
  const pT = mT, pB = H - mB, pH = pB - pT;

  // dots are the RAW per-dose difference vs the off-nights baseline; whiskers
  // stay the model's 90% CI, so a thin dose's wild raw reading can sit outside
  // its own whisker — that gap is the model's skepticism, on display
  const eff = (d) => rawDoseEff(d, r);
  let lo = 0, hi = 0;
  for (const d of rows) { lo = Math.min(lo, d.ci_low, eff(d)); hi = Math.max(hi, d.ci_high, eff(d)); }
  if (hi - lo < 1) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.08; hi += pad; lo -= pad;
  const py = (v) => pT + (hi - v) / (hi - lo) * pH;
  // x is TO SCALE: numeric doses sit at their value along the axis (half a column of
  // padding at each end); the open-ended arm keeps its own slot at the far right.
  const slotW = pW / (n + GAP);
  const nums = rows.filter((d) => !isOpenDose(d.dose)).map((d) => Number(d.dose));
  const dmin = Math.min(...nums), dmax = Math.max(...nums);
  const numL = pL + slotW / 2, numW = pW - slotW - (hasOpen ? slotW * (1 + GAP) : 0);
  const cx = (i) => {
    const d = Number(rows[i].dose);
    if (isOpenDose(d)) return pR - slotW / 2;
    return dmax === dmin ? numL + numW / 2 : numL + (d - dmin) / (dmax - dmin) * numW;
  };
  const y0 = py(0);
  const topLbl = (hi >= 0 ? "+" : "") + Math.round(hi);
  const botLbl = (lo >= 0 ? "+" : "") + Math.round(lo);

  const parts = [];
  // y extents + baseline (an extent label that would sit on the "0" is dropped)
  if (Math.abs((pT + 4) - (y0 + 3.5)) > 11)
    parts.push(`<text x="${pL - 6}" y="${pT + 4}" text-anchor="end" font-size="10" style="fill:var(--muted)">${topLbl}</text>`);
  if (Math.abs(pB - (y0 + 3.5)) > 11)
    parts.push(`<text x="${pL - 6}" y="${pB}" text-anchor="end" font-size="10" style="fill:var(--muted)">${botLbl}</text>`);
  parts.push(`<line x1="${pL}" y1="${y0}" x2="${pR}" y2="${y0}" stroke-width="1.5" stroke-dasharray="4 4" style="stroke:var(--line)"/>`);
  parts.push(`<text x="${pL - 6}" y="${y0 + 3.5}" text-anchor="end" font-size="10" style="fill:var(--text)">0</text>`);

  rows.forEach((d, i) => {
    const v = eff(d);
    const x = cx(i), col = v >= 0 ? "var(--good)" : "var(--bad)";
    const yTop = py(d.ci_high), yBot = py(d.ci_low), yM = py(v);
    // the whole mark group (whisker + dot + label + an invisible hit strip)
    // shares one tooltip, so hovering anywhere near the datapoint shows it
    parts.push(`<g data-tip="n=${d.n_nights}">`);
    parts.push(`<rect x="${x - 12}" y="${Math.min(yTop, yM) - 10}" width="24" height="${Math.abs(yBot - yTop) + 20}" fill="transparent"/>`);
    // whisker + caps
    parts.push(`<line x1="${x}" y1="${yTop}" x2="${x}" y2="${yBot}" stroke-width="2" style="stroke:var(--muted);opacity:.5"/>`);
    parts.push(`<line x1="${x - 5}" y1="${yTop}" x2="${x + 5}" y2="${yTop}" stroke-width="2" style="stroke:var(--muted);opacity:.5"/>`);
    parts.push(`<line x1="${x - 5}" y1="${yBot}" x2="${x + 5}" y2="${yBot}" stroke-width="2" style="stroke:var(--muted);opacity:.5"/>`);
    // recommended ring (only when the crown is earned)
    if (showRec && Math.abs(d.dose - r.recommended_dose) < 1e-6 && r.recommended_dose > 0) {
      parts.push(`<circle cx="${x}" cy="${yM}" r="8.5" fill="none" stroke-width="2" style="stroke:var(--accent-2)"/>`);
    }
    // raw-mean dot with a surface ring
    parts.push(`<circle cx="${x}" cy="${yM}" r="5" stroke-width="2" style="fill:${col};stroke:var(--panel)"/>`);
    // direct value label, always to the right of the dot (same direction for every dose)
    parts.push(`<text x="${x + 9}" y="${yM + 3.5}" text-anchor="start" font-size="11" font-weight="600" style="fill:${col}">${signed(v)}</text>`);
    parts.push(`</g>`);
    // x tick: the dose, or for the open-ended arm its label (wrapped; full text on hover)
    if (isOpenDose(d.dose)) {
      const tspans = openLines.map((ln, k) => `<tspan x="${x}" dy="${k ? 11 : 0}">${esc(ln)}</tspan>`).join("");
      parts.push(`<text x="${x}" y="${pB + 15}" text-anchor="middle" font-size="10.5" style="fill:var(--muted)">${tspans}<title>${esc(openLabel(iv))}</title></text>`);
    } else {
      parts.push(`<text x="${x}" y="${pB + 15}" text-anchor="middle" font-size="10.5" style="fill:var(--muted)">${esc(String(doseTick(iv, d.dose)))}</text>`);
    }
  });

  const cap = doseUnitCaption(iv);
  if (cap) parts.push(`<text x="${(pL + pR) / 2}" y="${H - 2}" text-anchor="middle" font-size="9.5" style="fill:var(--muted)">${esc(cap)}</text>`);

  const svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;display:block" role="img" ` +
    `aria-label="Dose versus HRV change over baseline for ${esc(iv.name)}">${parts.join("")}</svg>`;
  return el("div", "dose-chart", svg);
}

// Bar graph comparing a choice's options on HRV change vs skipping (the baseline).
// One bar per option (mean effect, colored by sign) with a 90% CI whisker; the
// most-likely-best option's label is highlighted. This is the choice counterpart
// of doseChart, shown beside the big "any option vs skipping" hero numbers.
function optionChart(iv, rows, r) {
  const n = rows.length;
  const W = Math.max(CHART_MIN_W, 54 + n * 84), H = 170;
  const mL = 42, mR = 16, mT = 16, mB = 32;
  const pL = mL, pR = W - mR, pT = mT, pB = H - mB, pH = pB - pT, pW = pR - pL;

  // dots: raw per-option difference vs skipping; whiskers: model 90% CI (as in doseChart)
  const eff = (d) => rawDoseEff(d, r);
  let lo = 0, hi = 0;
  for (const d of rows) { lo = Math.min(lo, d.ci_low, eff(d)); hi = Math.max(hi, d.ci_high, eff(d)); }
  if (hi - lo < 1) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.08; hi += pad; lo -= pad;
  const py = (v) => pT + (hi - v) / (hi - lo) * pH;
  const colW = pW / n, cx = (i) => pL + colW * (i + 0.5);
  const y0 = py(0);

  // the option most likely to be the best choice gets its dot ringed + label highlighted
  let bestId = null, bestP = -1;
  for (const d of rows) { if (d.p_best > bestP) { bestP = d.p_best; bestId = d.dose; } }

  const parts = [];
  parts.push(`<text x="${pL - 6}" y="${pT + 4}" text-anchor="end" font-size="10" style="fill:var(--muted)">${(hi >= 0 ? "+" : "") + Math.round(hi)}</text>`);
  parts.push(`<text x="${pL - 6}" y="${pB}" text-anchor="end" font-size="10" style="fill:var(--muted)">${(lo >= 0 ? "+" : "") + Math.round(lo)}</text>`);
  parts.push(`<line x1="${pL}" y1="${y0}" x2="${pR}" y2="${y0}" stroke-width="1.5" stroke-dasharray="4 4" style="stroke:var(--line)"/>`);
  parts.push(`<text x="${pL - 6}" y="${y0 + 3.5}" text-anchor="end" font-size="10" style="fill:var(--text)">0</text>`);

  rows.forEach((d, i) => {
    const v = eff(d);
    const x = cx(i), col = v >= 0 ? "var(--good)" : "var(--bad)";
    const yTop = py(d.ci_high), yBot = py(d.ci_low), yM = py(v);
    // one tooltip for the whole mark group, with a widened invisible hit strip
    parts.push(`<g data-tip="n=${d.n_nights}">`);
    parts.push(`<rect x="${x - 12}" y="${Math.min(yTop, yM) - 10}" width="24" height="${Math.abs(yBot - yTop) + 20}" fill="transparent"/>`);
    // 90% CI whisker with caps (identical mark to doseChart)
    parts.push(`<line x1="${x}" y1="${yTop}" x2="${x}" y2="${yBot}" stroke-width="2" style="stroke:var(--muted);opacity:.5"/>`);
    parts.push(`<line x1="${x - 5}" y1="${yTop}" x2="${x + 5}" y2="${yTop}" stroke-width="2" style="stroke:var(--muted);opacity:.5"/>`);
    parts.push(`<line x1="${x - 5}" y1="${yBot}" x2="${x + 5}" y2="${yBot}" stroke-width="2" style="stroke:var(--muted);opacity:.5"/>`);
    // ring the most-likely-best option (parity with doseChart's recommended ring)
    if (d.dose === bestId && bestP > 0) {
      parts.push(`<circle cx="${x}" cy="${yM}" r="8.5" fill="none" stroke-width="2" style="stroke:var(--accent-2)"/>`);
    }
    // raw-mean dot with a surface ring
    parts.push(`<circle cx="${x}" cy="${yM}" r="5" stroke-width="2" style="fill:${col};stroke:var(--panel)"/>`);
    // value label beside the dot, flipped left on the last column so it never overflows
    const left = i === n - 1 && n > 1;
    parts.push(`<text x="${x + (left ? -9 : 9)}" y="${yM + 3.5}" text-anchor="${left ? "end" : "start"}" font-size="11" font-weight="600" style="fill:${col}">${signed(v)}</text>`);
    parts.push(`</g>`);
    // option label tick (truncated, full text on hover); best option highlighted
    const raw = optLabel(iv, d.dose) || "(removed)";
    const isBest = d.dose === bestId;
    const short = raw.length > 11 ? raw.slice(0, 10) + "…" : raw;
    parts.push(`<text x="${x}" y="${pB + 14}" text-anchor="middle" font-size="10" ` +
      `style="fill:${isBest ? "var(--accent-2)" : "var(--muted)"};font-weight:${isBest ? "700" : "400"}">${esc(short)}<title>${esc(raw)}</title></text>`);
  });

  const svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;display:block" role="img" ` +
    `aria-label="Per-option HRV change vs skipping for ${esc(iv.name)}">${parts.join("")}</svg>`;
  return el("div", "dose-chart", svg);
}

// =====================================================================
// HRV READINESS CARD (training guidance, separate from the interventions)
// =====================================================================
// Server computes everything in ln(RMSSD) space and exponentiates back to ms:
// avg7 is the 7-day rolling mean, lo/hi the rolling 60-day mu +/- 0.5*sigma band
// (Hopkins' smallest-worthwhile-change convention). avg7 at/above lo -> intervals;
// below -> zone 2 training only.
function hrvTrendCard(t) {
  const card = el("div", "iv hrv-card");
  const top = el("div", "iv-top");
  top.appendChild(el("span", "iv-name", "🫀 HRV readiness"));
  const L = t.latest;
  let metaSpan = null;
  if (L) {
    const ok = L.status === "intervals";
    top.appendChild(el("span", "verdict " + (ok ? "v-help" : "v-no"),
      ok ? "🏃 intervals OK" : "🚶 zone 2"));
    metaSpan = el("div", "hrv-stats");
  } else {
    top.appendChild(el("span", "verdict v-new", "🌱 gathering data"));
    top.appendChild(el("span", "iv-meta",
      `${t.n_nights}/${t.min_nights} nights toward a baseline band`));
  }
  card.appendChild(top);
  if (metaSpan) card.appendChild(metaSpan);

  // legend on the left, range toggle (recent window vs everything since the app's
  // first night) on the right, sharing one row above the chart
  const range = el("span", "range-toggle");
  const btnRecent = el("button", "on", `${t.default_days} days`);
  const btnAll = el("button", "", "all");
  range.appendChild(btnRecent); range.appendChild(btnAll);
  const head = el("div", "chart-head");
  head.appendChild(el("p", "chart-cap",
    '<span class="mk-avg">━</span> 7-day avg · <span class="mk-peak">─</span> its peak in view · <span class="mk-band">▮</span> ' +
    `${t.window_days}-day μ ± 0.5σ band (of ln RMSSD) · <span class="mk-night">●</span> nightly`));
  head.appendChild(range);
  card.appendChild(head);
  const chartBox = el("div");
  const draw = (all) => {
    btnRecent.classList.toggle("on", !all);
    btnAll.classList.toggle("on", all);
    const slice = all ? t.days : t.days.slice(-t.default_days);
    if (metaSpan) {
      // middle figure follows the view: the geometric mean (consistent with the
      // ln-space method) of the nights currently on screen
      const logs = slice.filter((d) => d.hrv != null).map((d) => Math.log(d.hrv));
      const gm = logs.length ? Math.exp(logs.reduce((a, b) => a + b, 0) / logs.length) : null;
      // color-coded to the chart: blue = the 7-day line, teal = the band's lower edge
      const stat = (label, val, cls) =>
        `<span class="stat"><span class="stat-label">${label}</span>` +
        `<span class="stat-val ${cls}">${avg1(val)}<span class="stat-unit"> ms</span></span></span>`;
      metaSpan.innerHTML =
        stat("7-day avg", L.avg7, "c-avg") +
        (gm != null ? stat(all ? "all-time avg" : `${t.default_days}-day avg`, gm, "c-mid") : "") +
        stat("lower bound", L.lo, "c-lo");
    }
    chartBox.innerHTML = "";
    chartBox.appendChild(hrvTrendChart(slice, t.window_days, t.app_start));
  };
  btnRecent.onclick = () => draw(false);
  btnAll.onclick = () => draw(true);
  draw(false);
  card.appendChild(chartBox);
  card.appendChild(el("p", "iv-meta",
    "Nightly HRV wobbles a lot and the wobble is lopsided; the log makes it symmetric, and the " +
    "band means “within half a typical wobble of your average.” 7-day average at or above the " +
    "band's lower edge → intervals; below → zone 2 training. The band refits over a rolling " +
    `${t.window_days}-day window so it tracks fitness changes.`));
  return card;
}

function hrvTrendChart(days, windowDays, appStart) {
  const N = days.length;
  const W = 1100, H = 210;
  const mL = 40, mR = 46, mT = 10, mB = 24;
  const pL = mL, pR = W - mR, pT = mT, pB = H - mB, pH = pB - pT, pW = pR - pL;

  let lo = Infinity, hi = -Infinity;
  for (const d of days) for (const v of [d.hrv, d.avg7, d.lo, d.hi])
    if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  if (!isFinite(lo)) return el("div", "dose-chart", "");
  if (hi - lo < 1) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.1; hi += pad; lo -= pad;
  const px = (i) => N === 1 ? (pL + pR) / 2 : pL + (i / (N - 1)) * pW;
  const py = (v) => pT + (hi - v) / (hi - lo) * pH;

  // contiguous runs of days where a field is defined -> [[startIdx, endIdx], ...]
  const runs = (key) => {
    const out = []; let s = null;
    days.forEach((d, i) => {
      if (d[key] != null) { if (s === null) s = i; }
      else if (s !== null) { out.push([s, i - 1]); s = null; }
    });
    if (s !== null) out.push([s, N - 1]);
    return out;
  };

  const parts = [];
  // y axis: a line down the left edge, with extent labels matching the other charts
  parts.push(`<line x1="${pL}" y1="${pT}" x2="${pL}" y2="${pB}" stroke-width="1.5" style="stroke:var(--line)"/>`);
  parts.push(`<text x="${pL - 6}" y="${pT + 4}" text-anchor="end" font-size="10" style="fill:var(--muted)">${Math.round(hi)}</text>`);
  parts.push(`<text x="${pL - 6}" y="${pB}" text-anchor="end" font-size="10" style="fill:var(--muted)">${Math.round(lo)}</text>`);

  // the mu +/- 0.5*sigma band: filled region, solid lower edge (the decision line),
  // faint dashed center
  for (const [a, b] of runs("lo")) {
    const seg = days.slice(a, b + 1);
    const fwd = seg.map((d, j) => `${px(a + j)},${py(d.hi)}`).join(" ");
    const back = seg.map((d, j) => `${px(a + j)},${py(d.lo)}`).reverse().join(" ");
    parts.push(`<polygon points="${fwd} ${back}" style="fill:var(--accent-2);opacity:.14"/>`);
    parts.push(`<polyline points="${seg.map((d, j) => `${px(a + j)},${py(d.mid)}`).join(" ")}" fill="none" stroke-width="2" style="stroke:var(--accent-2)"/>`);
    parts.push(`<polyline points="${seg.map((d, j) => `${px(a + j)},${py(d.lo)}`).join(" ")}" fill="none" stroke-width="1" stroke-dasharray="3 4" style="stroke:var(--accent-2);opacity:.5"/>`);
  }

  // nightly HRV dots (the raw data, deliberately quiet)
  days.forEach((d, i) => {
    if (d.hrv == null) return;
    parts.push(`<circle cx="${px(i)}" cy="${py(d.hrv)}" r="2.5" style="fill:var(--muted);opacity:.55">` +
      `<title>${shortDate(d.date)}: ${avg1(d.hrv)} ms</title></circle>`);
  });

  // the 7-day rolling average -- the readiness signal itself
  for (const [a, b] of runs("avg7")) {
    const seg = days.slice(a, b + 1);
    parts.push(`<polyline points="${seg.map((d, j) => `${px(a + j)},${py(d.avg7)}`).join(" ")}" fill="none" stroke-width="2.5" stroke-linejoin="round" style="stroke:var(--accent)"/>`);
  }

  // thin, dim reference line through the period's highest 7-day average, labelled
  // at the left edge; a quiet "how far from my best" cue for the shown range
  let iPeak = -1;
  days.forEach((d, i) => { if (d.avg7 != null && (iPeak < 0 || d.avg7 > days[iPeak].avg7)) iPeak = i; });
  if (iPeak >= 0) {
    const yP = py(days[iPeak].avg7);
    parts.push(`<line x1="${pL}" y1="${yP}" x2="${pR}" y2="${yP}" stroke-width="1" ` +
      `style="stroke:var(--accent);opacity:.6"/>`);
    parts.push(`<text x="${pL + 5}" y="${yP - 3}" font-size="9.3" style="fill:var(--accent);opacity:.85">` +
      `peak 7-day avg ${avg1(days[iPeak].avg7)}</text>`);
  }

  // latest-value labels on the right edge, nudged apart if they'd collide
  const last = (key) => { for (let i = N - 1; i >= 0; i--) if (days[i][key] != null) return i; return -1; };
  const iA = last("avg7"), iL = last("mid");
  let yA = iA >= 0 ? py(days[iA].avg7) : null, yL = iL >= 0 ? py(days[iL].mid) : null;
  if (yA != null && yL != null && Math.abs(yA - yL) < 11) {
    const mid = (yA + yL) / 2, s = yA <= yL ? 1 : -1;
    yA = mid - s * 5.5; yL = mid + s * 5.5;
  }
  if (yA != null) parts.push(`<text x="${px(iA) + 7}" y="${yA + 3.5}" font-size="11" font-weight="600" style="fill:var(--accent)">${avg1(days[iA].avg7)}</text>`);
  if (yL != null) parts.push(`<text x="${px(iL) + 7}" y="${yL + 3.5}" font-size="11" font-weight="600" style="fill:var(--accent-2)">${avg1(days[iL].mid)}</text>`);

  // marker at the night the app (and its interventions) came into use -- HRV to the
  // left of this line is pure Oura history, untouched by any experiment
  const iS = appStart ? days.findIndex((d) => d.date === appStart) : -1;
  if (iS >= 0) {
    const x = px(iS);
    parts.push(`<line x1="${x}" y1="${pT}" x2="${x}" y2="${pB}" stroke-width="1.25" ` +
      `stroke-dasharray="4 4" style="stroke:var(--warn);opacity:.55"/>`);
    const flip = iS > N * 0.8;
    parts.push(`<text x="${x + (flip ? -5 : 5)}" y="${pT + 8}" text-anchor="${flip ? "end" : "start"}" ` +
      `font-size="9.3" style="fill:var(--warn);opacity:.78">app start</text>`);
  }

  // date ticks anchored on the newest day, in whole weeks, aiming for ~9 labels
  const tickStep = Math.max(7, Math.ceil(N / 10 / 7) * 7);
  for (let i = N - 1; i >= 0; i -= tickStep) {
    parts.push(`<text x="${px(i)}" y="${pB + 15}" text-anchor="middle" font-size="10" style="fill:var(--muted)">${shortDate(days[i].date)}</text>`);
  }

  const svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;display:block" role="img" ` +
    `aria-label="7-day average HRV against the rolling baseline band">${parts.join("")}</svg>`;
  const wrap = el("div", "dose-chart hrv-chart", svg);

  // Hover: a vertical cursor line snapped to the nearest day, with that day's
  // three header numbers (7-day avg, window avg, lower bound) in a floating box.
  const svgEl = wrap.querySelector("svg");
  const cursor = document.createElementNS("http://www.w3.org/2000/svg", "line");
  cursor.setAttribute("y1", pT); cursor.setAttribute("y2", pB);
  cursor.setAttribute("stroke-width", "1"); cursor.setAttribute("stroke-dasharray", "2 3");
  cursor.setAttribute("style", "stroke:var(--muted);opacity:.8");
  cursor.setAttribute("visibility", "hidden");
  svgEl.appendChild(cursor);
  const tip = el("div", "hrv-tip");
  tip.hidden = true;
  wrap.appendChild(tip);
  const fmt = (v) => (v == null ? "—" : avg1(v) + " ms");
  svgEl.addEventListener("mousemove", (e) => {
    const rect = svgEl.getBoundingClientRect();
    const sx = (e.clientX - rect.left) * (W / rect.width);   // viewBox coords
    const i = Math.max(0, Math.min(N - 1, Math.round((sx - pL) / pW * (N - 1))));
    const d = days[i], x = px(i);
    cursor.setAttribute("x1", x); cursor.setAttribute("x2", x);
    cursor.setAttribute("visibility", "visible");
    tip.innerHTML = `<b>${shortDate(d.date)}</b><br>7-day avg ${fmt(d.avg7)}` +
      `<br>${windowDays || 60}-day avg ${fmt(d.mid)}<br>lower bound ${fmt(d.lo)}`;
    tip.hidden = false;
    // keep the box beside the cursor, flipping sides near the right edge
    const left = x / W * rect.width;
    const flip = left > rect.width * 0.62;
    tip.style.left = flip ? "auto" : (left + 12) + "px";
    tip.style.right = flip ? (rect.width - left + 12) + "px" : "auto";
  });
  svgEl.addEventListener("mouseleave", () => {
    cursor.setAttribute("visibility", "hidden");
    tip.hidden = true;
  });
  return wrap;
}


// =====================================================================
// INTERACTIONS (Reports > Interactions): do two interventions help more or
// less together than their separate effects would add up to?
// =====================================================================
// Gate: a verdict needs this many nights with BOTH on, and a few in every other cell.
const MIN_BOTH = 4, MIN_CELL = 2;
const FLAG_P = 0.9, LEAN_P = 0.75;

// verdict from the HRV read-out: [class, label, rank] -- rank orders the page
// (strong flags first, gathering-data last).
function pairVerdict(r) {
  if (!r || r.n_both < MIN_BOTH || r.n_neither < MIN_CELL || r.n_a_only < MIN_CELL || r.n_b_only < MIN_CELL)
    return ["v-new", "🌱 gathering data", 5];
  // A flag needs the INTERACTION term itself to be reliably non-zero -- not merely
  // "together < the better one alone", which is trivially true when neither helps.
  // Given a reliably sub-additive pair: "substitutes" is the case where the pair is
  // about as good as the better one alone (A adds nothing once B is on), so
  // interference must CLEARLY beat that -- the pair is reliably worse than the
  // better one alone -- not merely be the coin-flip side of "about equal".
  if (r.p_synergy >= FLAG_P) return ["v-help", "✔ synergy — better together", 0];
  if (r.p_subadd >= FLAG_P) {
    return r.p_interfere >= LEAN_P ? ["v-no", "✕ interfere — worse together", 0]
                                   : ["v-sub", "≈ substitutes — effects overlap", 1];
  }
  if (r.p_synergy >= LEAN_P) return ["v-maybe", "… leaning synergy", 2];
  if (r.p_subadd >= LEAN_P) {
    return r.p_interfere >= LEAN_P ? ["v-maybe", "… leaning interference", 2]
                                   : ["v-maybe", "… leaning substitutes", 3];
  }
  return ["v-indep", "no interaction seen yet", 4];
}

function pairSortKey(p) {
  const r = p.hrv || {};
  const [, , rank] = pairVerdict(r);
  const strength = Math.max(r.p_synergy || 0, r.p_subadd || 0, r.p_interfere || 0);
  return [rank, -strength, -(r.n_both || 0)];
}

async function loadInteractions() {
  const box = $("#interactions-body");
  box.innerHTML = `<p class="empty">Crunching the numbers…</p>`;
  let data;
  try { data = await api("/api/interactions"); }
  catch (e) { box.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`; return; }

  box.innerHTML = "";
  RECENCY_ON = !!(data.recency && data.recency.enabled);
  renderRecencySlot(data.recency, data.count_maximize, data.baseline);
  const nPairs = data.pairs.length;
  box.appendChild(el("p", "rep-stat",
    `Every pair of interventions that has shared a night, across <b>${data.n_completed}</b> logged night${data.n_completed === 1 ? "" : "s"}` +
    (RECENCY_ON ? ` <span class="adj-flag">recency-weighted — counts shown are adjusted</span>` : "") + baseFlag() + `. ` +
    `For each pair the model asks whether the two did <em>better</em> together than their separate effects add up to (synergy), ` +
    `about as much as either alone (substitutes — their effects overlap), or <em>worse</em> than the better one alone (interference). ` +
    `A verdict needs ≥${MIN_BOTH} nights with both on. With ${nPairs} pair${nPairs === 1 ? "" : "s"} under test, expect the odd false flag — treat a flag as a lead to watch, not a conclusion.` +
    (data.n_pairs_no_overlap ? ` ${data.n_pairs_no_overlap} pair${data.n_pairs_no_overlap === 1 ? " has" : "s have"} never co-occurred yet.` : "")));

  if (!nPairs) { box.appendChild(el("p", "empty", "No two interventions have been on together on a logged night yet.")); return; }

  const sorted = data.pairs.slice().sort((x, y) => {
    const kx = pairSortKey(x), ky = pairSortKey(y);
    for (let i = 0; i < kx.length; i++) if (kx[i] !== ky[i]) return kx[i] - ky[i];
    return pairName(x).localeCompare(pairName(y));
  });
  const flagged = sorted.filter((p) => pairVerdict(p.hrv)[2] <= 3);
  const rest = sorted.filter((p) => pairVerdict(p.hrv)[2] > 3);

  if (flagged.length) {
    box.appendChild(el("h3", "rep-group", "Flagged pairs"));
    for (const p of flagged) box.appendChild(pairCard(p));
  } else {
    box.appendChild(el("h3", "rep-group", "Flagged pairs"));
    box.appendChild(el("p", "empty", "Nothing flagged yet — no pair looks reliably better or worse than additive."));
  }
  if (rest.length) {
    box.appendChild(el("h3", "rep-group", "All other pairs"));
    box.appendChild(pairTable(rest));
  }
}

const pairName = (p) => `${p.a.name} + ${p.b.name}`;

// Marker for pairs whose second side is OBSERVED behavior (e.g. "Fasting
// (actual)") rather than a randomized assignment: nights count whether the
// behavior was assigned or chosen, so these rows are correlational.
const obsTip = (p) => `“${p.b.name}” is what actually happened each night — assigned or chosen — not a ` +
  `randomized arm. Nights are included regardless of why the behavior occurred, so this row is ` +
  `correlational: a flag here is a lead worth probing, not a causal verdict.`;
const obsBadgeHtml = (p) => ` <span class="obs-flag" title="${esc(obsTip(p))}">👁 observed</span>`;
function obsBadge(p) {
  const b = el("span", "obs-flag", "👁 observed");
  b.title = obsTip(p);
  return b;
}

// the raw-average story: "together X, vs Y if the two effects simply added"
function additiveExpected(r) {
  if (r.raw_neither == null || r.raw_a_only == null || r.raw_b_only == null) return null;
  return r.raw_a_only + r.raw_b_only - r.raw_neither;
}

function pairCard(p) {
  const r = p.hrv || {};
  const [vc, vt] = pairVerdict(r);
  const card = el("div", "iv" + (p.a.active && p.b.active ? "" : " inactive"));
  const top = el("div", "iv-top");
  top.appendChild(el("span", "pair-name", `${esc(p.a.name)}<span class="plus">+</span>${esc(p.b.name)}`));
  top.appendChild(el("span", "verdict " + vc, vt));
  if (p.observational) top.appendChild(obsBadge(p));
  top.appendChild(el("span", "iv-meta", `${r.n_both || 0} night${r.n_both === 1 ? "" : "s"} with both · ${r.n || 0} comparable`));
  card.appendChild(top);

  const cols = el("div", "pair-2col");
  // left: the modeled numbers
  const left = el("div");
  left.appendChild(el("div", "metric-label", `Interaction on HRV <span class="metric-hint">beyond the two effects adding up</span>`));
  if (r.interaction) {
    const g = r.interaction;
    const cls = g.mean >= 0 ? "pos" : "neg";
    left.appendChild(el("div", `metric-val ${vc === "v-new" ? "muted" : cls}`,
      `${signed(g.mean)}<span class="metric-unit"> HRV</span>`));
    left.appendChild(el("div", "metric-sub",
      `90% CI ${signed(g.ci_low)} to ${signed(g.ci_high)}. ` +
      `Together: <b>${signed(r.combined.mean)}</b> vs the better one alone (${esc(r.effect_a.mean >= r.effect_b.mean ? p.a.name : p.b.name)}: <b>${signed(Math.max(r.effect_a.mean, r.effect_b.mean))}</b>).`));
    left.appendChild(el("div", "metric-sub",
      `P(better together than additive) <b>${pct(r.p_synergy)}</b> · P(worse than additive) <b>${pct(r.p_subadd)}</b> · P(pair beats the better one alone) <b>${pct(r.p_stack)}</b>`));
    // the conditional story: what each adds with vs. without the other
    const cond = (name, solo, withOther, other) =>
      `<b>${esc(name)}</b> adds <b>${signed(solo.mean)}</b> without ${esc(other)}, <b>${signed(withOther.mean)}</b> with it`;
    left.appendChild(el("div", "metric-sub",
      cond(p.a.name, r.effect_a, r.effect_a_with_b, p.b.name) + " · " +
      cond(p.b.name, r.effect_b, r.effect_b_with_a, p.a.name)));
  } else {
    left.appendChild(el("div", "metric-val muted", "—"));
  }
  const lr = p.lrhr;
  if (lr && lr.n_both) {
    // lrhr is fit on the NEGATED value (so higher = better inside the engine); flip the
    // sign back so the number reads as a change in resting HR (negative = good).
    const g = lr.interaction;
    left.appendChild(el("div", "metric-note",
      `Resting HR: interaction ${signed(-g.mean)} bpm (90% CI ${signed(-g.ci_high)} to ${signed(-g.ci_low)}; lower is better) · ` +
      `P(better together) ${pct(lr.p_synergy)} · P(interfere) ${pct(lr.p_interfere)} · ${lr.n_both} nights with both`));
  }
  cols.appendChild(left);

  // right: the raw 2x2 so the verdict is inspectable
  const right = el("div");
  right.appendChild(el("div", "metric-label", `Average HRV by night type <span class="metric-hint">${BASELINE_ON ? "baseline-adjusted averages" : "raw averages"} · n nights</span>`));
  const fmt = (v, n) => v == null ? `<span class="n">—</span>` : `${avg1(v)}<span class="n">n=${n}</span>`;
  right.appendChild(el("table", "cell-table",
    `<tr><th></th><th>${esc(p.b.name)} off</th><th>${esc(p.b.name)} on</th></tr>` +
    `<tr><th>${esc(p.a.name)} off</th><td>${fmt(r.raw_neither, r.n_neither)}</td><td>${fmt(r.raw_b_only, r.n_b_only)}</td></tr>` +
    `<tr><th>${esc(p.a.name)} on</th><td>${fmt(r.raw_a_only, r.n_a_only)}</td><td class="both">${fmt(r.raw_both, r.n_both)}</td></tr>`));
  const exp = additiveExpected(r);
  if (exp != null && r.raw_both != null) {
    right.appendChild(el("div", "metric-sub",
      `Both on averaged <b>${avg1(r.raw_both)}</b>; if the two effects simply added you'd expect about <b>${avg1(exp)}</b>.`));
  }
  cols.appendChild(right);
  card.appendChild(cols);
  return card;
}

function pairTable(pairs) {
  const t = el("table", "pair-rows");
  t.innerHTML = `<tr><th>Pair</th><th>Verdict</th><th>Nights with both</th><th>Interaction (HRV)</th><th>P(synergy)</th><th>P(sub-additive)</th></tr>`;
  for (const p of pairs) {
    const r = p.hrv || {};
    const [vc, vt] = pairVerdict(r);
    const tr = el("tr", "clickable");
    const g = r.interaction;
    tr.innerHTML =
      `<td>${esc(pairName(p))}${p.observational ? obsBadgeHtml(p) : ""}</td>` +
      `<td><span class="verdict ${vc}">${vt}</span></td>` +
      `<td class="num">${r.n_both || 0}</td>` +
      `<td class="num">${g ? `<span class="effect ${g.mean >= 0 ? "pos" : "neg"}">${signed(g.mean)}</span> <span class="ci">[${signed(g.ci_low)}, ${signed(g.ci_high)}]</span>` : "—"}</td>` +
      `<td class="num">${g ? pct(r.p_synergy) : "—"}</td>` +
      `<td class="num">${g ? pct(r.p_subadd) : "—"}</td>`;
    // click a row to expand it into a full card beneath
    tr.addEventListener("click", () => {
      const next = tr.nextElementSibling;
      if (next && next.classList.contains("expand")) { next.remove(); return; }
      const ex = el("tr", "expand");
      const td = el("td"); td.colSpan = 6; td.appendChild(pairCard(p)); ex.appendChild(td);
      tr.after(ex);
    });
    t.appendChild(tr);
  }
  return t;
}

// =====================================================================
// NAV + MODAL
// =====================================================================
// views: grid | reports (effects) | interactions -- the last two are sub-pages of the
// Reports tab, each with its own hash so they can be bookmarked.
function switchView(view) {
  if (!["grid", "reports", "interactions"].includes(view)) view = "grid";
  const top = view === "grid" ? "grid" : "reports";
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === top));
  $("#view-grid").hidden = top !== "grid";
  $("#view-reports").hidden = top !== "reports";
  $$(".subtab").forEach((b) => b.classList.toggle("active", b.dataset.sub === view));
  $("#reports-body").hidden = view !== "reports";
  $("#interactions-body").hidden = view !== "interactions";
  if (("#" + view) !== location.hash) location.hash = view;
  if (view === "reports") loadReports();
  if (view === "interactions") loadInteractions();
}
$$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));
$$(".subtab").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.sub)));
window.addEventListener("hashchange", () => switchView(location.hash.replace("#", "")));

let editingId = null;
function syncKind() {
  const k = $("#iv-kind").value;
  const dosed = k === "dose" || k === "choice_dose", choice = k === "choice" || k === "choice_dose";
  $("#dose-fields").hidden = !dosed;
  $("#choice-fields").hidden = !choice;
  // the open-ended arm is a plain-dose feature
  $("#open-arm-label").hidden = k !== "dose";
  if (k !== "dose") { $("#iv-form").open_arm.checked = false; $("#open-arm-name").hidden = true; }
  // the prior drives the on/off draw; a multiple choice only has one if it also tests skipping
  $("#prior-field").hidden = choice && !$("#iv-form").test_skip.checked;
}
$("#iv-kind").addEventListener("change", syncKind);
$("#iv-form").test_skip.addEventListener("change", syncKind);
// ⓘ tooltips: tap toggles (for touch), and a click inside a <label> must not focus its input
$$(".tip").forEach((t) => t.addEventListener("click", (e) => {
  e.preventDefault(); e.stopPropagation();
  const was = t.classList.contains("open");
  $$(".tip.open").forEach((o) => o.classList.remove("open"));
  if (!was) t.classList.add("open");
}));
document.addEventListener("click", () => $$(".tip.open").forEach((o) => o.classList.remove("open")));
// the "delay exploration" option only means something when exploring is on
function syncDelay() {
  const f = $("#iv-form");
  const exploring = f.explore_doses.checked;
  f.delay_explore.disabled = !exploring;
  $("#delay-explore-label").classList.toggle("dim", !exploring);
  // the open-ended arm needs exploration AND a max dose to sit above
  const hasMax = parseFloat(f.max_dose.value) > 0;
  const canOpen = exploring && hasMax;
  f.open_arm.disabled = !canOpen;
  $("#open-arm-label").classList.toggle("dim", !canOpen);
  $("#open-arm-name").hidden = !(canOpen && f.open_arm.checked);
}
$("#iv-form").explore_doses.addEventListener("change", syncDelay);
$("#iv-form").open_arm.addEventListener("change", syncDelay);
$("#iv-form").max_dose.addEventListener("input", syncDelay);

// ---- multiple-choice options editor (each option keeps a stable id) ----
let modalOptions = [];  // [{id: number|null, label, prior, retired}]
const liveModalOptions = () => modalOptions.filter((o) => !o.retired);

// Effective prior over the options (fraction each): assigned values as given, the
// blank ones sharing the remaining mass equally (mirrors bandit.effective_option_probs).
function effectiveOptionPriors(opts) {
  const given = opts.map((o) => (o.prior > 0 ? o.prior : null));
  const s = given.reduce((a, p) => a + (p || 0), 0);
  const blanks = given.filter((p) => p == null).length;
  return given.map((p) => p != null ? p : (blanks && s < 1 ? (1 - s) / blanks : 0));
}

function refreshPriorPlaceholders() {
  const live = liveModalOptions();
  const eff = effectiveOptionPriors(live);
  $$("#options-list .opt-prior").forEach((inp, i) => {
    inp.placeholder = Math.round(eff[i] * 100) + "%";
    inp.title = live[i].prior > 0 ? "Your prior that this option is the best"
      : `Blank — gets an equal share of what's left: ${Math.round(eff[i] * 100)}%`;
  });
  const s = live.reduce((a, o) => a + (o.prior > 0 ? o.prior : 0), 0);
  const blanks = live.filter((o) => !(o.prior > 0)).length;
  const bad = s > 1 + 1e-9 || (!blanks && live.length && Math.abs(s - 1) > 1e-9);
  const hint = $("#prior-sum-hint");
  if (hint) {
    hint.hidden = !live.length;
    hint.textContent = bad
      ? (s > 1 ? `Priors add up to ${Math.round(s * 100)}% — they can't exceed 100%.`
               : `Priors add up to ${Math.round(s * 100)}% — with every option assigned they must total 100%.`)
      : (blanks ? `Assigned ${Math.round(s * 100)}% · the ${blanks} blank option${blanks === 1 ? "" : "s"} share${blanks === 1 ? "s" : ""} the remaining ${Math.round((1 - s) * 100)}%.`
                : "Priors total 100%.");
    hint.classList.toggle("bad", bad);
  }
}

function renderOptions() {
  const list = $("#options-list");
  list.innerHTML = "";
  const live = liveModalOptions();
  if (!live.length) {
    list.appendChild(el("p", "field-hint", "No options yet — add at least two below."));
  }
  modalOptions.forEach((o, i) => {
    if (o.retired) return;
    const row = el("div", "opt-row");
    row.appendChild(el("span", "opt-label", esc(o.label)));
    // optional prior that THIS option is the best, as a percent (blank = equal share of the rest)
    const pri = document.createElement("input");
    pri.type = "number"; pri.min = "1"; pri.max = "100"; pri.step = "1"; pri.inputMode = "numeric";
    pri.className = "opt-prior";
    pri.value = o.prior > 0 ? String(Math.round(o.prior * 100)) : "";
    pri.addEventListener("input", () => {
      const v = parseFloat(pri.value);
      modalOptions[i] = { ...modalOptions[i], prior: v > 0 ? Math.min(v, 100) / 100 : null };
      refreshPriorPlaceholders();
    });
    row.appendChild(pri);
    row.appendChild(el("span", "opt-pct", "%"));
    const ren = el("button", "ghost opt-btn", "Rename");
    ren.type = "button";
    ren.addEventListener("click", () => renameOption(i));
    const del = el("button", "ghost opt-btn opt-del", "✕");
    del.type = "button";
    del.title = "Remove option";
    del.addEventListener("click", () => deleteOption(i));
    row.appendChild(ren);
    row.appendChild(del);
    list.appendChild(row);
  });
  // Removed options: kept (their past nights still show under their name) and
  // restorable -- one click puts an option back on offer for future nights.
  const retired = modalOptions.map((o, i) => [o, i]).filter(([o]) => o.retired);
  if (retired.length) {
    const box = el("details", "retired-options");
    box.appendChild(el("summary", "", `${retired.length} removed option${retired.length === 1 ? "" : "s"} — past nights still count; restore to offer again`));
    for (const [o, i] of retired) {
      const row = el("div", "opt-row retired");
      row.appendChild(el("span", "opt-label", esc(o.label)));
      const res = el("button", "ghost opt-btn", "Restore");
      res.type = "button";
      res.title = "Offer this option again on future nights";
      res.addEventListener("click", () => restoreOption(i));
      row.appendChild(res);
      box.appendChild(row);
    }
    list.appendChild(box);
  }
  refreshPriorPlaceholders();
}

function addOptionFromInput() {
  const inp = $("#new-option");
  const v = inp.value.trim();
  if (!v) return;
  modalOptions.push({ id: null, label: v, prior: null });   // id minted server-side on save
  inp.value = "";
  renderOptions();
  inp.focus();
}

function renameOption(i) {
  const cur = modalOptions[i].label;
  const next = window.prompt(`Rename “${cur}”?\nIts history stays attached to this option.`, cur);
  if (next == null) return;
  const t = next.trim();
  if (t && t !== cur) { modalOptions[i] = { ...modalOptions[i], label: t }; renderOptions(); }
}

function deleteOption(i) {
  if (window.confirm(`Remove “${modalOptions[i].label}”?\nIt won't be offered on future nights, but its past nights are kept — and you can restore it later.`)) {
    // never dropped: kept as retired so its history stays labelled and it can come back
    modalOptions[i] = { ...modalOptions[i], prior: null, retired: true };
    renderOptions();
  }
}

function restoreOption(i) {
  modalOptions[i] = { ...modalOptions[i], retired: false };
  renderOptions();
}

$("#add-option-btn").addEventListener("click", addOptionFromInput);
$("#new-option").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); addOptionFromInput(); }
});

// ---- completion-input editor -----------------------------------------------
// How a night's cell is marked done: checkbox (default), mutually exclusive
// buttons, or a number. Radio buttons keep stable ids exactly like choice
// options, so recorded nights survive renames.
const INPUT_TYPES = ["checkbox", "radio", "quantity"];
let inputOptions = [];  // [{id: number|null, label}]

function renderInputOptions() {
  const list = $("#input-options-list");
  list.innerHTML = "";
  if (!inputOptions.length) {
    list.appendChild(el("p", "field-hint", "No buttons yet — add at least one below."));
  }
  inputOptions.forEach((o, i) => {
    const row = el("div", "opt-row");
    row.appendChild(el("span", "opt-label", esc(o.label)));
    const ren = el("button", "ghost opt-btn", "Rename");
    ren.type = "button";
    ren.addEventListener("click", () => {
      const next = window.prompt(`Rename “${o.label}”?\nIts recorded nights stay attached.`, o.label);
      if (next == null) return;
      const t = next.trim();
      if (t && t !== o.label) { inputOptions[i] = { ...o, label: t }; renderInputOptions(); }
    });
    const del = el("button", "ghost opt-btn opt-del", "✕");
    del.type = "button";
    del.title = "Remove button";
    del.addEventListener("click", () => {
      if (window.confirm(`Remove “${o.label}”?\nPast nights recorded with it are kept.`)) {
        inputOptions.splice(i, 1);
        renderInputOptions();
      }
    });
    row.appendChild(ren);
    row.appendChild(del);
    list.appendChild(row);
  });
}

function addInputOptionFromInput() {
  const inp = $("#new-input-option");
  const v = inp.value.trim();
  if (!v) return;
  inputOptions.push({ id: null, label: v });   // id minted server-side on save
  inp.value = "";
  renderInputOptions();
  inp.focus();
}

$("#add-input-option-btn").addEventListener("click", addInputOptionFromInput);
$("#new-input-option").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); addInputOptionFromInput(); }
});

function syncInputType() {
  const t = $("#iv-input-type").value;
  $("#input-radio-fields").hidden = t !== "radio";
  $("#input-qty-field").hidden = t !== "quantity";
}
$("#iv-input-type").addEventListener("change", syncInputType);

function openModal(iv) {
  editingId = iv ? iv.id : null;
  $("#modal-title").textContent = iv ? "Edit intervention" : "Add intervention";
  const f = $("#iv-form");
  f.name.value = iv ? iv.name : "";
  f.category.value = iv && CATEGORY_ORDER.includes(iv.category) ? iv.category : "supplement";
  f.kind.value = iv ? iv.kind : "dose";
  f.unit.value = iv ? iv.unit : "";
  f.default_dose.value = iv ? iv.default_dose : 1;
  f.unit_dose.value = iv && iv.unit_dose ? iv.unit_dose : "";
  f.dose_noun.value = iv ? (iv.dose_noun || "") : "";
  f.noun_amount.value = iv && iv.noun_amount != null ? iv.noun_amount : "";
  f.min_dose.value = iv && iv.min_dose != null ? iv.min_dose : "";
  f.max_dose.value = iv && iv.max_dose != null ? iv.max_dose : "";
  f.explore_doses.checked = iv ? iv.explore_doses !== false : true;
  f.delay_explore.checked = iv ? iv.delay_explore !== false : true;
  f.open_arm.checked = iv ? !!iv.open_arm : false;
  f.open_arm_label.value = iv ? (iv.open_arm_label || "") : "";
  syncDelay();
  f.test_skip.checked = iv ? !!iv.test_skip : false;
  f.prior_helps.value = String(iv && iv.prior_helps ? priorPct(iv.prior_helps) : 50);
  f.note.value = iv ? iv.note : "";
  modalOptions = iv && isChoiceKind(iv) ? (iv.options || []).map((o) => ({ id: o.id, label: o.label, prior: o.prior > 0 ? o.prior : null, retired: !!o.retired })) : [];
  $("#new-option").value = "";
  renderOptions();
  f.input_type.value = iv && INPUT_TYPES.includes(iv.input_type) ? iv.input_type : "checkbox";
  f.input_label.value = iv ? (iv.input_label || "") : "";
  inputOptions = iv ? (iv.input_options || []).map((o) => ({ id: o.id, label: o.label })) : [];
  $("#new-input-option").value = "";
  renderInputOptions();
  syncInputType();
  syncKind();
  const del = $("#iv-delete");
  del.hidden = !iv;
  if (iv) { del.textContent = "Delete"; del.onclick = () => deleteIv(iv); }
  const pause = $("#iv-pause");
  pause.hidden = !iv;
  if (iv) { pause.textContent = iv.active ? "Pause" : "Resume"; pause.onclick = () => togglePause(iv); }
  $("#modal-backdrop").hidden = false;
  f.name.focus();
}
function closeModal() { $("#modal-backdrop").hidden = true; editingId = null; }

// Pause/resume: paused interventions stop being suggested on future nights (a night
// that's already planned is left unchanged) and keep all their data; pass dose_levels
// back so the update doesn't regenerate them.
async function togglePause(iv) {
  STATE = await api(`/api/interventions/${iv.id}`, {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ active: !iv.active, dose_levels: iv.dose_levels }),
  });
  closeModal(); renderGrid();
}

async function deleteIv(iv) {
  if (!confirm(`Remove “${iv.name}” from your tracker?\n\nIts recorded data is kept — nothing is deleted, and it stops being suggested.`)) return;
  await api(`/api/interventions/${iv.id}`, { method: "DELETE" });
  closeModal(); loadGrid();
}

$("#iv-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const kind = ["choice", "boolean", "choice_dose"].includes(f.kind.value) ? f.kind.value : "dose";
  const prPct = Math.min(80, Math.max(1, parseFloat(f.prior_helps.value) || 50));  // capped at 80%
  const payload = { name: f.name.value.trim(), kind, category: f.category.value, note: f.note.value.trim(),
                    prior_helps: prPct / 100 };
  payload.input_type = INPUT_TYPES.includes(f.input_type.value) ? f.input_type.value : "checkbox";
  payload.input_options = inputOptions;
  payload.input_label = f.input_label.value.trim();
  if (payload.input_type === "radio" && !inputOptions.length) {
    alert("Add at least one button label (or switch the completion input back to a checkbox).");
    return;
  }
  if (kind === "choice" || kind === "choice_dose") {
    const live = liveModalOptions();
    if (!live.length) { alert("Add at least one option (or restore a removed one)."); return; }
    const ps = live.reduce((a, o) => a + (o.prior > 0 ? o.prior : 0), 0);
    const blanks = live.filter((o) => !(o.prior > 0)).length;
    if (ps > 1 + 1e-9) { alert("The option priors add up to more than 100%."); return; }
    if (!blanks && Math.abs(ps - 1) > 1e-9) { alert("With every option given a prior, they must add up to exactly 100%."); return; }
    payload.options = modalOptions;
    payload.test_skip = f.test_skip.checked;
  }
  if (kind === "dose" || kind === "choice_dose") {
    payload.unit = f.unit.value.trim();
    payload.default_dose = parseFloat(f.default_dose.value) || 1;
    payload.unit_dose = parseFloat(f.unit_dose.value) || 0;
    payload.dose_noun = f.dose_noun.value.trim();
    const per = parseFloat(f.noun_amount.value);
    payload.noun_amount = per > 0 ? per : null;
    payload.explore_doses = f.explore_doses.checked;
    payload.delay_explore = f.delay_explore.checked;
    payload.open_arm = kind === "dose" && f.open_arm.checked && !f.open_arm.disabled;
    payload.open_arm_label = f.open_arm_label.value.trim();
    // optional bounds: blank -> null (no bound). Keep the standard dose inside them.
    const lo = parseFloat(f.min_dose.value), hi = parseFloat(f.max_dose.value);
    payload.min_dose = lo > 0 ? lo : null;
    payload.max_dose = hi > 0 ? hi : null;
    if (payload.min_dose != null && payload.max_dose != null && payload.min_dose > payload.max_dose) {
      alert("Min dose must be less than or equal to max dose."); return;
    }
    if ((payload.min_dose != null && payload.default_dose < payload.min_dose) ||
        (payload.max_dose != null && payload.default_dose > payload.max_dose)) {
      alert("The standard dose must be between the min and max dose."); return;
    }
  }
  try {
    if (editingId) STATE = await api(`/api/interventions/${editingId}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    else STATE = await post("/api/interventions", payload);
    closeModal(); renderGrid();
  } catch (err) { alert("Error: " + err.message); }
});

$("#add-btn").addEventListener("click", () => openModal(null));
$("#modal-close").addEventListener("click", closeModal);
$("#iv-cancel").addEventListener("click", closeModal);
$("#modal-backdrop").addEventListener("click", (e) => { if (e.target === $("#modal-backdrop")) closeModal(); });
// row-label click opens the editor for that intervention
$("#grid").addEventListener("click", (e) => {
  if (READ_ONLY) return;
  const lab = e.target.closest(".rowlab");
  if (!lab) return;
  const id = lab.parentElement.dataset.ivId;   // group/HRV rows have none
  const iv = id && STATE && STATE.interventions.find((x) => String(x.id) === id);
  if (iv) openModal(iv);
});

// =====================================================================
// OURA SYNC
// =====================================================================
let OURA = null;   // last /api/oura/status payload

function ouraResultText(r) {
  if (!r) return "";
  if (r.error) return ` — <span class="oura-err">${esc(r.error)}</span>`;
  const parts = [];
  if (r.filled) parts.push(`${r.filled} filled`);
  if (r.updated) parts.push(`${r.updated} updated`);
  return " — " + (parts.length ? parts.join(", ") : "everything already up to date");
}

function renderOuraUI() {
  const btn = $("#oura-btn");
  btn.innerHTML = OURA && OURA.connected ? "💍 Oura ✓" : "💍 Oura";
  const st = $("#oura-status");
  if (!OURA) { st.textContent = "Loading…"; return; }
  if (OURA.connected) {
    st.innerHTML = `<b class="oura-ok">Connected.</b> Last sync: ` +
      (OURA.last_sync ? esc(OURA.last_sync.replace("T", " ")) : "never") +
      ouraResultText(OURA.last_result);
  } else if (OURA.configured) {
    st.innerHTML = "Credentials saved — <b>not connected yet.</b> Click “Save &amp; connect” to authorize.";
  } else {
    st.innerHTML = "Not set up yet. Follow the steps below.";
  }
  $("#oura-disconnect").hidden = !OURA.connected;
  $("#oura-sync-now").hidden = !OURA.connected;
  // once connected the primary button is redundant -- it comes back (as a reconnect)
  // only if the credential fields are edited
  $("#oura-save").hidden = OURA.connected;
  $("#oura-save").textContent = OURA.connected ? "Save & reconnect" : "Save & connect";
  const f = $("#oura-form");
  if (document.activeElement !== f.client_id) f.client_id.value = OURA.client_id || "";
  f.client_secret.placeholder = OURA.configured ? "(unchanged — leave blank to keep)" : "";
}

async function refreshOuraStatus() {
  try { OURA = await api("/api/oura/status"); } catch { OURA = null; }
  renderOuraUI();
}

// Quiet background sync on page load: only when connected; re-render the grid if
// anything was filled in or upgraded to Oura's precision.
async function ouraAutoSync() {
  await refreshOuraStatus();
  if (!OURA || !OURA.connected) return;
  try {
    const r = await post("/api/oura/sync", {});
    OURA.last_result = r;
    renderOuraUI();
    if (r.filled || r.updated) await loadGrid();
  } catch (e) { console.warn("Oura sync failed:", e.message); }
}

function openOuraModal() { renderOuraUI(); $("#oura-backdrop").hidden = false; refreshOuraStatus(); }
function closeOuraModal() { $("#oura-backdrop").hidden = true; }

for (const field of ["client_id", "client_secret"])
  $("#oura-form")[field].addEventListener("input", () => { $("#oura-save").hidden = false; });

$("#oura-btn").addEventListener("click", openOuraModal);
$("#oura-close").addEventListener("click", closeOuraModal);
$("#oura-cancel").addEventListener("click", closeOuraModal);
$("#oura-backdrop").addEventListener("click", (e) => { if (e.target === $("#oura-backdrop")) closeOuraModal(); });

$("#oura-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    OURA = await post("/api/oura/config", {
      client_id: f.client_id.value.trim(),
      client_secret: f.client_secret.value,   // blank = keep stored secret
    });
    if (!OURA.configured) { renderOuraUI(); return; }
    window.location.href = "/oura/connect";   // hand off to Oura's authorize page
  } catch (err) { alert("Error: " + err.message); }
});

$("#oura-sync-now").addEventListener("click", async () => {
  const btn = $("#oura-sync-now");
  btn.disabled = true; btn.textContent = "Syncing…";
  try {
    const r = await post("/api/oura/sync", { force: true });
    OURA.last_result = r;
    await refreshOuraStatus();
    if (r.filled || r.updated) await loadGrid();
  } catch (e) { alert("Sync failed: " + e.message); }
  btn.disabled = false; btn.textContent = "Sync now";
});

$("#oura-disconnect").addEventListener("click", async () => {
  if (!confirm("Disconnect Oura?\n\nYour recorded nights are kept; the app just stops syncing.")) return;
  OURA = await post("/api/oura/disconnect", {});
  renderOuraUI();
});

// Returning from Oura's authorize page: /#oura=connected | denied | error
function handleOuraReturn() {
  const m = location.hash.match(/^#oura=(\w+)/);
  if (!m) return;
  history.replaceState(null, "", SB.base + "#grid");
  if (m[1] === "connected") openOuraModal();   // ouraAutoSync() follows in the init chain
  else alert(m[1] === "denied" ? "Oura connection was declined." : "Oura connection failed — try connecting again.");
}

// =====================================================================
// NIGHT-SHAPE TAGS (tags.py)
// =====================================================================
let TAGS = null;          // last /api/tags/status payload
let TAGS_POLL = null;     // timer while the background tagger runs

function renderTagsUI() {
  const st = $("#tags-status");
  const f = $("#tags-form");
  if (!TAGS) { st.textContent = "Loading…"; return; }
  const c = TAGS.counts, w = TAGS.worker;
  let html = TAGS.enabled
    ? `<b class="oura-ok">On.</b> `
    : (TAGS.configured ? `<b>Off</b> — key saved; tick the box to start tagging. ` : `<b>Off</b> — paste a Claude API key to start. `);
  html += `${c.tagged} tagged · ${TAGS.pending} pending of ${c.low} low night${c.low === 1 ? "" : "s"} ` +
    `(${c.recorded} recorded, ${c.series} with raw series) · model <code>${esc(TAGS.model)}</code>` +
    (TAGS.key_from_env ? " · key from ANTHROPIC_API_KEY" : "");
  if (w.running) html += `<br><b>Tagging…</b> ${w.done} of ${w.total}` +
    (w.last_date ? ` — last: ${esc(shortDate(w.last_date))} → ${(w.last_tags || []).map(tagLabel).map(esc).join(", ") || "no tags"}` : "");
  else if (w.started && w.done) html += `<br>Last run: ${w.done} night${w.done === 1 ? "" : "s"} tagged.`;
  if (w.error) html += `<br><span class="oura-err">${esc(w.error)}</span>`;
  st.innerHTML = html;
  if (document.activeElement !== f.enabled) f.enabled.checked = !!TAGS.enabled;
  if (document.activeElement !== f.threshold) f.threshold.value = TAGS.threshold;
  f.api_key.placeholder = TAGS.configured ? "(saved — leave blank to keep)" : "sk-ant-…";
  $("#tags-run").hidden = !TAGS.configured || !TAGS.pending || w.running;
  $("#tags-run").textContent = `Tag ${TAGS.pending} pending night${TAGS.pending === 1 ? "" : "s"} now`;
  $("#tags-clear").hidden = !c.tagged;
  $("#tags-vocab-n").textContent = `· ${TAGS.vocab.length} tags`;
  $("#tags-vocab").innerHTML = TAGS.vocab.map((v) =>
    `<li><span class="ntag">${esc(v.label)}</span> <b>${v.n}</b> night${v.n === 1 ? "" : "s"}` +
    `${v.created_by !== "seed" ? ` <span class="muted">· added by ${esc(v.created_by)}</span>` : ""}<br>${esc(v.description)}</li>`).join("");
  // keep polling while the background tagger runs, then refresh the grid once
  if (w.running && !TAGS_POLL) {
    TAGS_POLL = setInterval(async () => {
      try { TAGS = await api("/api/tags/status"); } catch { return; }
      renderTagsUI();
      if (!TAGS.worker.running) { clearInterval(TAGS_POLL); TAGS_POLL = null; loadGrid(); }
    }, 3000);
  }
}

async function refreshTagsStatus() {
  try { TAGS = await api("/api/tags/status"); } catch { TAGS = null; }
  renderTagsUI();
}

function openTagsModal() { renderTagsUI(); $("#tags-backdrop").hidden = false; refreshTagsStatus(); }
function closeTagsModal() { $("#tags-backdrop").hidden = true; }

$("#tags-btn").addEventListener("click", openTagsModal);
$("#tags-close").addEventListener("click", closeTagsModal);
$("#tags-cancel").addEventListener("click", closeTagsModal);
$("#tags-backdrop").addEventListener("click", (e) => { if (e.target === $("#tags-backdrop")) closeTagsModal(); });

$("#tags-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    TAGS = await post("/api/tags/config", {
      api_key: f.api_key.value.trim(),      // blank = keep the stored key
      threshold: Number(f.threshold.value),
      enabled: f.enabled.checked,
    });
    f.api_key.value = "";
    renderTagsUI();
    await loadGrid();   // the row appears/disappears with the switch
  } catch (err) { alert("Error: " + err.message); }
});

$("#tags-run").addEventListener("click", async () => {
  try { TAGS = await post("/api/tags/run", {}); renderTagsUI(); }
  catch (err) { alert("Error: " + err.message); }
});

$("#tags-clear").addEventListener("click", async () => {
  if (!confirm("Clear every night's tags?\n\nThe vocabulary is kept. Low nights will be re-read on the next run.")) return;
  try { TAGS = await post("/api/tags/clear", {}); renderTagsUI(); await loadGrid(); }
  catch (err) { alert("Error: " + err.message); }
});

// ---- hosted mode: what each face of the site shows -------------------------
if (READ_ONLY) {
  // public view: no editing controls anywhere (and no way to reach the admin page)
  $("#add-btn").hidden = true;
  $("#oura-btn").hidden = true;
  $("#tags-btn").hidden = true;
  const exp = $('.toolbar a[href="/api/export"]'); if (exp) exp.hidden = true;
  const hint = $("#view-grid .hint");
  if (hint) hint.textContent = "Each night the app chooses which interventions to run (and at what dose/option), weighted toward what's raised HRV so far. Highlighted cells were done; the bottom rows are each morning's measurements.";
}
if (SB.mode === "admin") {
  $("#logout-btn").hidden = false;
  $("#logout-btn").addEventListener("click", async () => {
    try { await post("/api/logout", {}); } catch (e) { /* cookie clears anyway */ }
    location.href = "/";
  });
}
if (SB.hosted) {
  // the Oura redirect URI shown in the setup text is the site's, not localhost
  $$("#oura-backdrop code").forEach((c) => { if (c.textContent.includes("/oura/callback")) c.textContent = SB.redirect_uri; });
}

if (SB.mode === "login") {
  // admin path, not signed in: only the sign-in form; nothing else loads
  $("#login-backdrop").hidden = false;
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#login-error"); err.hidden = true;
    try {
      await post("/api/login", { password: e.target.password.value });
      location.reload();
    } catch (ex) {
      err.textContent = ex.message || "Sign-in failed."; err.hidden = false;
    }
  });
  $("#login-form").password.focus();
} else {
  loadGrid()
    .then(() => {
      handleOuraReturn();
      switchView(location.hash.replace("#", "") || "grid");
      if (!READ_ONLY) ouraAutoSync();
    })
    .catch((e) => { document.body.innerHTML = `<pre style="padding:40px;color:#ff7a7a">Failed to load: ${e.message}</pre>`; });
}
