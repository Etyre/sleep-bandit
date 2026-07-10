"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.status === 204 ? null : r.json();
};
const post = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const el = (tag, cls, html) => { const n = document.createElement(tag); if (cls) n.className = cls; if (html != null) n.innerHTML = html; return n; };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const signed = (x) => (x >= 0 ? "+" : "") + Number(x).toFixed(1);
const pct = (p) => Math.round(p * 100) + "%";
const numLabel = (d) => (Number.isInteger(d) ? d : +d.toFixed(3));
const shortDate = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: "short", day: "numeric" }); };
const weekday = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "short" }); };

let STATE = null;

const CATEGORY_ORDER = ["supplement", "behavioral"];
const CATEGORY_LABEL = { supplement: "Supplements", behavioral: "Behavioral" };

// outcome metrics shown as rows under the interventions (HRV is the target)
const METRICS = [
  { key: "hrv", label: "HRV", hint: "higher is better" },
  { key: "lrhr", label: "Resting HR", hint: "lower is better · bpm" },
];

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
  if (/[^aeiou]y$/i.test(noun)) return noun.slice(0, -1) + "ies";
  return noun + (/s$/i.test(noun) ? "" : "s");
}

// how a dose amount is written: as a count of unit-doses ("3 pills") if the
// intervention names its unit dose, otherwise as the raw amount ("144 mg").
function doseText(iv, amount) {
  const u = iv.unit_dose || 0, noun = (iv.dose_noun || "").trim();
  if (u > 0 && noun) {
    const count = amount / u;
    return `${numLabel(count)} ${esc(pluralize(noun, count))}`;
  }
  return iv.unit ? `${numLabel(amount)} ${esc(iv.unit)}` : "on";
}

// options are {id, label}; look one up by its stable id
function optLabel(iv, id) {
  const o = (iv.options || []).find((x) => x.id === Math.round(id));
  return o ? o.label : null;
}

// label shown in a grid cell (null = not activated / grayed out)
function cellLabel(iv, assigned) {
  if (iv.kind === "choice") {
    const label = optLabel(iv, assigned);
    return label != null ? esc(label) : null;
  }
  if (iv.kind === "boolean") return assigned > 0 ? "yes" : null;
  if (assigned > 0) return doseText(iv, assigned);
  return null;
}

function ivMeta(iv) {
  if (iv.kind === "choice") return (iv.options || []).map((o) => esc(o.label)).join(" · ");
  if (iv.kind === "boolean") return "yes / no";
  return doseText(iv, iv.default_dose);
}

// =====================================================================
// TRACKER GRID
// =====================================================================
async function loadGrid() {
  STATE = await api("/api/state");
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
    head += `<th class="night${n.is_today ? " today" : ""}">` +
      `${n.is_today ? "Tonight" : weekday(n.date)}<span class="ndate">${shortDate(n.date)}</span></th>`;
  }
  head += `</tr></thead>`;

  let body = `<tbody>`;

  // intervention rows, grouped by category
  for (const [cat, listRaw] of groupByCategory(interventions)) {
    const list = listRaw.slice().sort((a, b) =>
      a.active === b.active ? a.name.localeCompare(b.name) : (a.active ? -1 : 1));
    body += `<tr class="group-row"><th class="rowlab group-lab">${CATEGORY_LABEL[cat]}</th>` +
      `<td class="group-fill" colspan="${nights.length}"></td></tr>`;
    for (const iv of list) {
      const meta = ivMeta(iv) + (iv.active ? "" : " · paused");
      body += `<tr class="${iv.active ? "" : "paused"}" data-iv-id="${iv.id}">` +
        `<th class="rowlab">${esc(iv.name)}<span class="rmeta">${meta}</span></th>`;
      for (const n of nights) {
        const c = (cells[n.date] || {})[iv.id];
        const todayCls = n.is_today ? " col-today" : "";
        if (!c) { body += `<td class="cell na${todayCls}"></td>`; continue; }
        const label = cellLabel(iv, c.assigned_dose);
        if (label != null) {
          body += `<td class="cell active${c.done ? " done" : ""}${todayCls}" data-date="${n.date}" data-iv="${iv.id}">` +
            `<span class="dose">${label}</span><span class="check">✓</span></td>`;
        } else {
          body += `<td class="cell off${todayCls}">–</td>`;
        }
      }
      body += `</tr>`;
    }
  }

  // separator between interventions and the outcome metrics
  body += `<tr class="group-row outcomes-row"><th class="rowlab group-lab">Outcomes</th>` +
    `<td class="group-fill" colspan="${nights.length}"></td></tr>`;

  // outcome metric rows (HRV = target; resting HR = secondary, lower is better)
  for (const m of METRICS) {
    body += `<tr class="metric-row"><th class="rowlab metric-lab">${m.label}` +
      `<span class="rmeta">${m.hint}</span></th>`;
    for (const n of nights) {
      body += `<td class="hrvcell${n.is_today ? " col-today" : ""}">` +
        `<input type="number" step="any" min="0" data-date="${n.date}" data-metric="${m.key}" ` +
        `value="${n[m.key] == null ? "" : n[m.key]}" placeholder="—"></td>`;
    }
    body += `</tr>`;
  }
  body += `</tbody>`;
  table.innerHTML = head + body;

  $$(".cell.active", table).forEach((td) => td.addEventListener("click", () => toggleCell(td)));
  $$(".hrvcell input", table).forEach((inp) => inp.addEventListener("change", () => saveMetric(inp)));

  // Tonight is the right-most column, so start scrolled all the way over to it.
  const wrap = table.closest(".grid-wrap");
  if (wrap) wrap.scrollLeft = wrap.scrollWidth;
}

async function toggleCell(td) {
  const done = !td.classList.contains("done");
  td.classList.toggle("done", done);                       // optimistic
  const cell = (STATE.cells[td.dataset.date] || {})[td.dataset.iv];
  if (cell) cell.done = done;
  try {
    await post("/api/cell", { date: td.dataset.date, intervention_id: td.dataset.iv, done });
  } catch (e) {
    td.classList.toggle("done", !done);
    if (cell) cell.done = !done;
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
    if (night) night[metric] = num;
  } catch (e) {
    alert("Could not save: " + e.message);
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

async function loadReports() {
  const box = $("#reports-body");
  box.innerHTML = `<p class="empty">Crunching the numbers…</p>`;
  let data;
  try { data = await api("/api/reports"); }
  catch (e) { box.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`; return; }

  box.innerHTML = "";
  box.appendChild(el("p", "rep-stat", data.n_completed === 0
    ? "No nights logged yet — reports fill in as you enter your HRV on the Tracker."
    : `Based on <b>${data.n_completed}</b> logged night${data.n_completed === 1 ? "" : "s"}. “Effect” is the change in HRV from taking the intervention, in your HRV units. Anything reliably positive is worth keeping — even a small effect.`));
  if (!data.interventions.length) {
    box.appendChild(el("p", "empty", "No interventions yet."));
    return;
  }
  for (const [cat, list] of groupByCategory(data.interventions)) {
    box.appendChild(el("h3", "rep-group", CATEGORY_LABEL[cat]));
    for (const iv of list.slice().sort((a, b) => a.name.localeCompare(b.name)))
      box.appendChild(reportCard(iv));
  }
}

function reportCard(iv) {
  const r = iv.readout || {};
  const isChoice = iv.kind === "choice";
  const nOn = r.n_on || 0, nOff = r.n_off || 0, nObs = nOn + nOff;
  const card = el("div", "iv" + (iv.active ? "" : " inactive"));

  const top = el("div", "iv-top");
  top.appendChild(el("span", "iv-name", esc(iv.name)));
  if (isChoice) {
    if (nOn < 4) top.appendChild(el("span", "verdict v-new", "🌱 gathering data"));
  } else {
    const [vc, vt] = verdict(r);
    top.appendChild(el("span", "verdict " + vc, vt));
  }
  top.appendChild(el("span", "iv-meta",
    isChoice ? `${(iv.options || []).length} options`
    : iv.kind === "boolean" ? "yes / no"
    : `standard ${doseText(iv, iv.default_dose)}`));
  card.appendChild(top);

  const lObs = (iv.readout_lrhr?.n_on || 0) + (iv.readout_lrhr?.n_off || 0);

  // No real data on EITHER metric -> stay empty instead of showing prior noise.
  if (nObs === 0 && lObs === 0) {
    card.appendChild(el("p", "iv-meta",
      "No logged nights yet — enter your HRV or resting HR on the Tracker and this report will fill in."));
    return card;
  }

  if (nObs > 0) {
    const dataNote = `${nOn} night${nOn === 1 ? "" : "s"} on, ${nOff} off.`;
    // Not enough of a contrast yet -> no recommendation, just say so.
    if (!enough(nOn, nOff, isChoice)) {
      const need = isChoice ? `at least ${MIN_CHOICE} option-nights`
        : `at least ${MIN_ON} on and ${MIN_OFF} off`;
      card.appendChild(el("p", "iv-meta",
        `📈 <b>HRV</b>: gathering data — ${dataNote} Needs ${need} before it can compare.`));
    } else {
      const doses = (r.doses || []).slice().sort((a, b) => b.p_best - a.p_best);
      if (isChoice) {
        const best = optLabel(iv, r.recommended_dose);
        card.appendChild(el("p", "iv-meta",
          (best ? `📈 Best for HRV: <b>${esc(best)}</b> — effects relative to your average night. ` : "📈 ") +
          `Based on ${dataNote}`));
        if (doses.length) {
          const table = el("table", "dose-table");
          table.innerHTML = "<tr><th>Option</th><th>HRV effect</th><th>P(best)</th><th></th></tr>";
          for (const d of doses) {
            const rec = Math.round(d.dose) === Math.round(r.recommended_dose) && r.recommended_dose > 0;
            table.appendChild(doseRow(d, esc(optLabel(iv, d.dose) || "(removed)"), rec, false));
          }
          card.appendChild(table);
        }
      } else {
        if (r.on_effect != null) {
          const cls = r.on_effect >= 0 ? "pos" : "neg";
          card.appendChild(el("p", "iv-meta",
            `📈 <b>HRV</b>: <span class="effect ${cls}">${signed(r.on_effect)}</span> ` +
            `<span class="ci">[${signed(r.on_ci_low)}, ${signed(r.on_ci_high)}]</span> on average — ` +
            `<b>${pct(r.p_helps)}</b> chance it's a real positive effect` +
            `${r.p_meaningful >= 0.85 ? " (likely sizable)" : ""}. Based on ${dataNote}`));
        }
        // Dose effects are secondary to "does it work": show them as a graph, and
        // only crown a recommended dose once there's real contrast (enough on-nights
        // AND 2+ distinct doses tried). r.doses is already in ascending-dose order.
        if ((r.doses || []).length) {
          const showRec = enoughForDoseRec(r);
          card.appendChild(el("p", "chart-cap",
            'Dose → HRV change over baseline · <span class="mk-dot">●</span> mean · <span class="mk-ci">│</span> 90% CI'));
          card.appendChild(doseChart(iv, r.doses, r, showRec));
          if (!showRec) {
            card.appendChild(el("p", "iv-meta",
              "No dose recommended yet — needs more on-nights across at least two different doses."));
          }
        }
      }
    }
  }

  appendLrhr(card, iv);
  return card;
}

// Resting-HR line (LRHR fit is on -LRHR, so flip the sign back to bpm; negative = lower = good)
function appendLrhr(card, iv) {
  const rl = iv.readout_lrhr || {};
  const nOn = rl.n_on || 0, nOff = rl.n_off || 0;
  if (nOn + nOff === 0) return;
  if (!enough(nOn, nOff, iv.kind === "choice") || rl.on_effect == null) {
    card.appendChild(el("p", "iv-meta",
      `🫀 <b>Resting HR</b>: gathering data — ${nOn} night${nOn === 1 ? "" : "s"} on, ${nOff} off.`));
    return;
  }
  const effBpm = -rl.on_effect;
  const cls = effBpm <= 0 ? "pos" : "neg";  // lower resting HR is good -> green
  card.appendChild(el("p", "iv-meta",
    `🫀 <b>Resting HR</b>: <span class="effect ${cls}">${signed(effBpm)}</span> bpm ` +
    `<span class="ci">[${signed(-rl.on_ci_high)}, ${signed(-rl.on_ci_low)}]</span> on average ` +
    `(lower is better) — <b>${pct(rl.p_helps)}</b> chance it lowers your resting HR.`));
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

// A dose axis tick ("3" pills / "3000" mg) and the unit caption under the axis.
function doseTick(iv, dose) {
  const u = iv.unit_dose || 0, noun = (iv.dose_noun || "").trim();
  return numLabel(u > 0 && noun ? dose / u : dose);
}
function doseUnitCaption(iv) {
  const u = iv.unit_dose || 0, noun = (iv.dose_noun || "").trim();
  if (u > 0 && noun) return pluralize(noun, 2);
  return iv.unit || "";
}

// Dose interventions get a graph, not a horse-race table: dose on x, mean HRV
// change over baseline on y, with a 90% credible-interval whisker per dose (the
// uncertainty matters — it's huge early). Dots are colored by sign, baseline at 0.
function doseChart(iv, rows, r, showRec) {
  const n = rows.length;
  const W = Math.max(230, 54 + n * 72), H = 150;
  const mL = 42, mR = 16, mT = 16, mB = 30;
  const pL = mL, pR = W - mR, pT = mT, pB = H - mB, pH = pB - pT, pW = pR - pL;

  let lo = 0, hi = 0;
  for (const d of rows) { lo = Math.min(lo, d.ci_low); hi = Math.max(hi, d.ci_high); }
  if (hi - lo < 1) { hi += 1; lo -= 1; }
  const pad = (hi - lo) * 0.08; hi += pad; lo -= pad;
  const py = (v) => pT + (hi - v) / (hi - lo) * pH;
  const colW = pW / n, cx = (i) => pL + colW * (i + 0.5);
  const y0 = py(0);
  const topLbl = (hi >= 0 ? "+" : "") + Math.round(hi);
  const botLbl = (lo >= 0 ? "+" : "") + Math.round(lo);

  const parts = [];
  // y extents + baseline
  parts.push(`<text x="${pL - 6}" y="${pT + 4}" text-anchor="end" font-size="10" style="fill:var(--muted)">${topLbl}</text>`);
  parts.push(`<text x="${pL - 6}" y="${pB}" text-anchor="end" font-size="10" style="fill:var(--muted)">${botLbl}</text>`);
  parts.push(`<line x1="${pL}" y1="${y0}" x2="${pR}" y2="${y0}" stroke-width="1.5" stroke-dasharray="4 4" style="stroke:var(--line)"/>`);
  parts.push(`<text x="${pL - 6}" y="${y0 + 3.5}" text-anchor="end" font-size="10" style="fill:var(--text)">0</text>`);

  rows.forEach((d, i) => {
    const x = cx(i), col = d.mean_effect >= 0 ? "var(--good)" : "var(--bad)";
    const yTop = py(d.ci_high), yBot = py(d.ci_low), yM = py(d.mean_effect);
    // whisker + caps
    parts.push(`<line x1="${x}" y1="${yTop}" x2="${x}" y2="${yBot}" stroke-width="2" style="stroke:var(--muted);opacity:.5"/>`);
    parts.push(`<line x1="${x - 5}" y1="${yTop}" x2="${x + 5}" y2="${yTop}" stroke-width="2" style="stroke:var(--muted);opacity:.5"/>`);
    parts.push(`<line x1="${x - 5}" y1="${yBot}" x2="${x + 5}" y2="${yBot}" stroke-width="2" style="stroke:var(--muted);opacity:.5"/>`);
    // recommended ring (only when the crown is earned)
    if (showRec && Math.abs(d.dose - r.recommended_dose) < 1e-6 && r.recommended_dose > 0) {
      parts.push(`<circle cx="${x}" cy="${yM}" r="8.5" fill="none" stroke-width="2" style="stroke:var(--accent-2)"/>`);
    }
    // mean dot with a surface ring, plus a native hover tooltip
    parts.push(`<circle cx="${x}" cy="${yM}" r="5" stroke-width="2" style="fill:${col};stroke:var(--panel)">` +
      `<title>${esc(doseText(iv, d.dose))}: ${signed(d.mean_effect)} HRV [${signed(d.ci_low)}, ${signed(d.ci_high)}]</title></circle>`);
    // direct value label, flipped to the left on the last column so it never overflows
    const left = i === n - 1 && n > 1;
    parts.push(`<text x="${x + (left ? -9 : 9)}" y="${yM + 3.5}" text-anchor="${left ? "end" : "start"}" font-size="11" font-weight="600" style="fill:${col}">${signed(d.mean_effect)}</text>`);
    // x tick
    parts.push(`<text x="${x}" y="${pB + 15}" text-anchor="middle" font-size="10.5" style="fill:var(--muted)">${esc(String(doseTick(iv, d.dose)))}</text>`);
  });

  const cap = doseUnitCaption(iv);
  if (cap) parts.push(`<text x="${(pL + pR) / 2}" y="${H - 2}" text-anchor="middle" font-size="9.5" style="fill:var(--muted)">${esc(cap)}</text>`);

  const svg = `<svg viewBox="0 0 ${W} ${H}" width="100%" style="max-width:${W}px;display:block" role="img" ` +
    `aria-label="Dose versus HRV change over baseline for ${esc(iv.name)}">${parts.join("")}</svg>`;
  return el("div", "dose-chart", svg);
}

// =====================================================================
// NAV + MODAL
// =====================================================================
function switchView(view) {
  if (view !== "grid" && view !== "reports") view = "grid";
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  $("#view-grid").hidden = view !== "grid";
  $("#view-reports").hidden = view !== "reports";
  if (("#" + view) !== location.hash) location.hash = view;
  if (view === "reports") loadReports();
}
$$(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));
window.addEventListener("hashchange", () => switchView(location.hash.replace("#", "")));

let editingId = null;
function syncKind() {
  const k = $("#iv-kind").value;
  $("#dose-fields").hidden = k !== "dose";
  $("#choice-fields").hidden = k !== "choice";
}
$("#iv-kind").addEventListener("change", syncKind);

// ---- multiple-choice options editor (each option keeps a stable id) ----
let modalOptions = [];  // [{id: number|null, label}]

function renderOptions() {
  const list = $("#options-list");
  list.innerHTML = "";
  if (!modalOptions.length) {
    list.appendChild(el("p", "field-hint", "No options yet — add at least two below."));
  }
  modalOptions.forEach((o, i) => {
    const row = el("div", "opt-row");
    row.appendChild(el("span", "opt-label", esc(o.label)));
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
}

function addOptionFromInput() {
  const inp = $("#new-option");
  const v = inp.value.trim();
  if (!v) return;
  modalOptions.push({ id: null, label: v });   // id minted server-side on save
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
  if (window.confirm(`Remove “${modalOptions[i].label}”?\nIt won't be offered on future nights, but its past nights are kept.`)) {
    modalOptions.splice(i, 1);
    renderOptions();
  }
}

$("#add-option-btn").addEventListener("click", addOptionFromInput);
$("#new-option").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); addOptionFromInput(); }
});

function openModal(iv) {
  editingId = iv ? iv.id : null;
  $("#modal-title").textContent = iv ? "Edit intervention" : "Add intervention";
  const f = $("#iv-form");
  f.name.value = iv ? iv.name : "";
  f.category.value = iv && iv.category === "behavioral" ? "behavioral" : "supplement";
  f.kind.value = iv ? iv.kind : "dose";
  f.unit.value = iv ? iv.unit : "";
  f.default_dose.value = iv ? iv.default_dose : 1;
  f.unit_dose.value = iv && iv.unit_dose ? iv.unit_dose : "";
  f.dose_noun.value = iv ? (iv.dose_noun || "") : "";
  f.explore_doses.checked = iv ? iv.explore_doses !== false : true;
  f.note.value = iv ? iv.note : "";
  modalOptions = iv && iv.kind === "choice" ? (iv.options || []).map((o) => ({ id: o.id, label: o.label })) : [];
  $("#new-option").value = "";
  renderOptions();
  syncKind();
  const del = $("#iv-delete");
  del.hidden = !iv;
  if (iv) { del.textContent = "Delete"; del.onclick = () => deleteIv(iv); }
  $("#modal-backdrop").hidden = false;
  f.name.focus();
}
function closeModal() { $("#modal-backdrop").hidden = true; editingId = null; }

async function deleteIv(iv) {
  if (!confirm(`Delete “${iv.name}” and its history?`)) return;
  await api(`/api/interventions/${iv.id}`, { method: "DELETE" });
  closeModal(); loadGrid();
}

$("#iv-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const kind = ["choice", "boolean"].includes(f.kind.value) ? f.kind.value : "dose";
  const payload = { name: f.name.value.trim(), kind, category: f.category.value, note: f.note.value.trim() };
  if (kind === "choice") {
    payload.options = modalOptions;
  } else if (kind === "dose") {
    payload.unit = f.unit.value.trim();
    payload.default_dose = parseFloat(f.default_dose.value) || 1;
    payload.unit_dose = parseFloat(f.unit_dose.value) || 0;
    payload.dose_noun = f.dose_noun.value.trim();
    payload.explore_doses = f.explore_doses.checked;
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
  const lab = e.target.closest(".rowlab");
  if (!lab) return;
  const id = lab.parentElement.dataset.ivId;   // group/HRV rows have none
  const iv = id && STATE && STATE.interventions.find((x) => String(x.id) === id);
  if (iv) openModal(iv);
});

loadGrid()
  .then(() => switchView(location.hash.replace("#", "") || "grid"))
  .catch((e) => { document.body.innerHTML = `<pre style="padding:40px;color:#ff7a7a">Failed to load: ${e.message}</pre>`; });
