/* Shared helpers for the dashboard and the Ask AI page. No dependencies. */
"use strict";
const $ = id => document.getElementById(id);
const api = async (path, opts) => (await fetch(path, opts)).json();
const esc = s => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
                                .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const fmt = (v, d = 1) => (v == null || Number.isNaN(Number(v))) ? "–"
  : Number(v).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: 0 });
const pct = (v, d = 1) => v == null ? "–" : fmt(v, d) + "%";
const money = v => {
  if (v == null) return "–";
  const a = Math.abs(v);
  if (a >= 1e7) return "₹" + fmt(v / 1e7, 1) + " cr";
  if (a >= 1e5) return "₹" + fmt(v / 1e5, 1) + " lakh";
  return "₹" + fmt(v, 0);
};
const C = { s1: "#2a78d6", s2: "#eb6834", s3: "#1baf7a", grid: "#eef1f5", axis: "#c3c2b7", ink3: "#8b93a1" };

/* "FY27 Q1" -> "Apr–Jun 2026" (fiscal year runs April-March, label is the ending year) */
function quarterRange(q) {
  const m = /FY(\d\d) Q([1-4])/.exec(q || "");
  if (!m) return q || "";
  const fyEnd = 2000 + Number(m[1]), n = Number(m[2]);
  const starts = ["Apr–Jun", "Jul–Sep", "Oct–Dec", "Jan–Mar"][n - 1];
  return `${starts} ${n === 4 ? fyEnd : fyEnd - 1}`;
}

/* Column name -> plain-English header */
const LABELS = {
  outlet_code: "Outlet", outlet_name: "Name", city: "City", channel: "Channel",
  fill_pct: "Fill rate", fill_rate_pct: "Fill rate", case_fill_pct: "Fill rate (cases)",
  ordered_each: "Units", ordered_case: "Cases", units_ordered: "Units",
  otif_pct: "OTIF", on_time_pct: "On time", in_full_pct: "In full",
  avg_delay_min: "Avg delay (min)", deliveries: "Deliveries",
  route_code: "Route", route_name: "Route name", warehouse_name: "Warehouse", warehouse_code: "Warehouse",
  region_name: "Region", dispatched_cr: "Dispatched (₹ cr)",
  late_over_2h: "Late >2 h", pct_late_over_2h: "Share late >2 h",
  month_label: "Month", excursions_per_100_chilled: "Per 100 chilled", excursions: "Excursions",
  chilled_deliveries: "Chilled deliveries", chilled_on_ambient_vehicle: "On non-reefer vehicle",
  category: "Category", returns_lakh: "Returns (₹ lakh)", top_reason: "Main reason",
  return_reason: "Reason", credit_notes: "Credit notes",
  at_risk_lakh: "At risk (₹ lakh)", cases: "Cases",
  sku_code: "SKU", product_name: "Product", discontinued_date: "Discontinued on",
  lines_after_discontinuation: "Order lines after", value_lakh: "Value (₹ lakh)", value_cr: "Value (₹ cr)",
  delivered_cases: "Cases delivered", freight_per_case_inr: "Freight per case (₹)",
  carrier_name: "Carrier", invoices: "Invoices", avg_invoice_inr: "Avg invoice (₹)", disputed: "Disputed",
  sales_cr: "Sales (₹ cr)", our_mrp: "Our MRP (₹)", lowest_competitor_price: "Lowest competitor (₹)",
  gap_pct: "Gap vs MRP", skus_observed: "SKUs seen", avg_gap_vs_mrp_pct: "Avg gap vs MRP",
  exclude_reason: "Excluded because", outlets: "Outlets",
  listing_id: "Listing", retailer: "Retailer", listing_title: "Listing title", pack: "Pack",
  category_site: "Site category", price_inr: "Price (₹)",
  built_at: "Built at", source_db: "Source DB", table_name: "Table", rows: "Rows",
  freight_confirmed_cr: "Confirmed (₹ cr)", freight_disputed_cr: "Disputed (₹ cr)",
  disputed_invoices: "Disputed invoices", disputed_pct: "Disputed",
};
const label = c => LABELS[c] || c.replace(/_/g, " ").replace(/\bpct\b/, "%");

/* Format a cell by its column name */
function cell(col, v) {
  if (v == null) return "–";
  if (typeof v !== "number") return esc(v);
  if (/(_pct|^pct_)/.test(col)) return pct(v, 1);
  if (/_inr$/.test(col)) return fmt(v, 0);
  if (/_(lakh|cr)$/.test(col)) return fmt(v, 2);
  if (Number.isInteger(v)) return fmt(v, 0);
  return fmt(v, 2);
}

/* Generic table.
   opts.bars   {col: maxValue}  -> inline magnitude bar in that column
   opts.rank   true             -> leading rank column
   opts.flag   {col, test(v), text, level} -> status pill when test passes
   opts.total  colName          -> rows whose col === 'TOTAL' get bold styling
   opts.max    n                -> cap rows shown */
function table(el, rows, opts = {}) {
  const host = typeof el === "string" ? $(el) : el;
  if (!rows || !rows.length) { host.innerHTML = `<div class="empty">${esc(opts.empty || "Nothing to show for this selection.")}</div>`; return; }
  if (opts.max) rows = rows.slice(0, opts.max);
  const cols = Object.keys(rows[0]);
  const numeric = cols.map(c => rows.every(r => r[c] == null || typeof r[c] === "number"));
  let h = "<table><thead><tr>" + (opts.rank ? "<th></th>" : "") + cols.map((c, i) =>
    `<th class="${numeric[i] ? "num" : ""}">${esc(label(c))}</th>`).join("") + "</tr></thead><tbody>";
  rows.forEach((r, ri) => {
    const isTotal = opts.total && String(r[opts.total]).toUpperCase() === "TOTAL";
    h += "<tr>" + (opts.rank ? `<td class="rank">${ri + 1}</td>` : "");
    cols.forEach((c, i) => {
      let inner = cell(c, r[c]);
      if (opts.bars && c in opts.bars && typeof r[c] === "number") {
        const w = Math.max(0, Math.min(100, r[c] * 100 / (opts.bars[c] || 100)));
        inner = `<span class="cellbar"><span class="track"><span class="fill" style="width:${w.toFixed(1)}%"></span></span>${inner}</span>`;
      }
      if (opts.flag && c === opts.flag.col && opts.flag.test(r[c]))
        inner += ` <span class="pill ${opts.flag.level || "critical"}">${esc(opts.flag.text)}</span>`;
      h += `<td class="${numeric[i] ? "num" : ""}${isTotal ? " total" : ""}">${inner}</td>`;
    });
    h += "</tr>";
  });
  host.innerHTML = h + "</tbody></table>";
}

/* ---------- SVG line chart: one y-axis (% values), hairline grid, 2px lines,
   end markers with a surface ring, crosshair + tooltip ---------- */
function lineChart(el, rows, xKey, series, opts = {}) {
  const host = typeof el === "string" ? $(el) : el;
  if (!rows || !rows.length) { host.innerHTML = '<div class="empty">No data.</div>'; return; }
  const W = Math.max(host.clientWidth || 640, 420), H = opts.h || 240, P = { l: 40, r: 14, t: 12, b: 26 };
  const xs = rows.map(r => r[xKey]);
  let vals = [];
  series.forEach(s => rows.forEach(r => { if (r[s.key] != null) vals.push(r[s.key]); }));
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (opts.zero) lo = Math.min(lo, 0);
  const pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
  if (opts.zero && Math.min(...vals) >= 0) lo = 0;          /* zero-floored: never show a negative axis */
  const X = i => P.l + i * (W - P.l - P.r) / Math.max(xs.length - 1, 1);
  const Y = v => P.t + (hi - v) * (H - P.t - P.b) / (hi - lo);
  let g = "";
  for (let k = 0; k <= 4; k++) {
    const v = lo + (hi - lo) * k / 4, y = Y(v);
    g += `<line x1="${P.l}" y1="${y}" x2="${W - P.r}" y2="${y}" stroke="${C.grid}"/>` +
         `<text x="${P.l - 7}" y="${y + 3.5}" text-anchor="end">${fmt(v, 1)}</text>`;
  }
  const step = Math.ceil(xs.length / 9);
  xs.forEach((x, i) => { if (i % step === 0 || i === xs.length - 1)
    g += `<text x="${X(i)}" y="${H - 7}" text-anchor="middle">${esc(String(x).slice(2))}</text>`; });
  series.forEach(s => {
    const pts = rows.map((r, i) => r[s.key] == null ? null : [X(i), Y(r[s.key])]).filter(Boolean);
    const d = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
    g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    const e = pts[pts.length - 1];
    if (e) g += `<circle cx="${e[0]}" cy="${e[1]}" r="4" fill="${s.color}" stroke="#fff" stroke-width="2"/>`;
  });
  g += `<line class="xh" x1="0" y1="${P.t}" x2="0" y2="${H - P.b}" stroke="${C.axis}" style="display:none"/>`;
  series.forEach((s, si) => { g += `<circle class="dot${si}" r="4.5" fill="${s.color}" stroke="#fff" stroke-width="2" style="display:none"/>`; });
  host.innerHTML = `<svg width="100%" viewBox="0 0 ${W} ${H}" style="display:block;overflow:visible">${g}</svg>`;
  const svg = host.firstChild, tip = $("tip");
  svg.addEventListener("mousemove", ev => {
    const box = svg.getBoundingClientRect();
    const mx = (ev.clientX - box.left) * W / box.width;
    const i = Math.max(0, Math.min(xs.length - 1, Math.round((mx - P.l) / ((W - P.l - P.r) / Math.max(xs.length - 1, 1)))));
    const xh = svg.querySelector(".xh"); xh.setAttribute("x1", X(i)); xh.setAttribute("x2", X(i)); xh.style.display = "";
    let html = `<b>${esc(xs[i])}</b>`;
    series.forEach((s, si) => {
      const d = svg.querySelector(".dot" + si), v = rows[i][s.key];
      if (v == null) { d.style.display = "none"; return; }
      d.setAttribute("cx", X(i)); d.setAttribute("cy", Y(v)); d.style.display = "";
      html += `<br><span style="color:${s.color}">●</span> ${esc(s.name)}: ${fmt(v, 2)}${opts.unit || ""}`;
    });
    tip.innerHTML = html; tip.style.display = "block";
    tip.style.left = (ev.clientX + 14) + "px"; tip.style.top = (ev.clientY + 10) + "px";
  });
  svg.addEventListener("mouseleave", () => {
    tip.style.display = "none"; svg.querySelector(".xh").style.display = "none";
    series.forEach((s, si) => svg.querySelector(".dot" + si).style.display = "none");
  });
}

/* ---------- horizontal bars: one hue = magnitude, <=18px thick, value at the tip ---------- */
function barChart(el, rows, labelKey, valKey, opts = {}) {
  const host = typeof el === "string" ? $(el) : el;
  if (!rows || !rows.length) { host.innerHTML = '<div class="empty">No data.</div>'; return; }
  const max = Math.max(...rows.map(r => r[valKey] || 0)) || 1;
  host.innerHTML = rows.map(r => `
    <div class="hbar" title="${esc(r[labelKey])}: ${fmt(r[valKey], 1)}${opts.unit || ""}">
      <div class="lab">${esc(r[labelKey])}</div>
      <div class="track"><div class="fill" style="width:${((r[valKey] || 0) * 100 / max).toFixed(1)}%"></div></div>
      <div class="v">${fmt(r[valKey], 1)}${opts.unit || ""}</div>
    </div>`).join("");
}

/* ---------- status from config targets ---------- */
function statusOf(key, v, targets) {
  const t = targets && targets[key];
  if (!t || v == null) return { level: "none", text: "no target" };
  const better = t.higher_is_better;
  const good = better ? v >= t.good : v <= t.good;
  const warn = better ? v >= t.warn : v <= t.warn;
  if (good) return { level: "good", text: "On target" };
  if (warn) return { level: "warn", text: "Watch" };
  return { level: "critical", text: "Needs attention" };
}

/* ---------- signed delta vs a previous value ---------- */
function deltaHtml(cur, prev, { unit = "", digits = 1, higherIsBetter = true, label = "vs last quarter" } = {}) {
  if (cur == null || prev == null) return `<span class="delta flat">no prior period</span>`;
  const d = cur - prev;
  if (Math.abs(d) < Math.pow(10, -digits) / 2) return `<span class="delta flat">unchanged ${esc(label)}</span>`;
  const goodDir = higherIsBetter ? d > 0 : d < 0;
  return `<span class="delta ${goodDir ? "up" : "down"}">${d > 0 ? "▲" : "▼"} ${fmt(Math.abs(d), digits)}${unit}</span> <span class="muted">${esc(label)}</span>`;
}

/* ---------- rows -> HTML table string (for chat cards) ---------- */
function rowsToTable(rows, opts) { const d = document.createElement("div"); table(d, rows, opts); return d.innerHTML; }

/* ---------- small, safe Markdown -> HTML (headings, bold, code, lists,
   blockquotes, paragraphs). Escapes first, then builds real tags around the
   escaped text, so nothing in the source can inject markup. ---------- */
function mdToHtml(md) {
  let s = esc(md ?? "");
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");
  s = s.replace(/^### (.*)$/gm, "<h3>$1</h3>").replace(/^## (.*)$/gm, "<h2>$1</h2>").replace(/^# (.*)$/gm, "<h2>$1</h2>");
  s = s.replace(/^&gt; (.*)$/gm, "<blockquote>$1</blockquote>");
  const listBlock = (re, tag) => { s = s.replace(re, (m, pre, block) => {
    const items = block.trim().split("\n").map(l => `<li>${l.replace(/^\s*(?:[-*]|\d+\.)\s+/, "")}</li>`).join("");
    return `${pre}<${tag}>${items}</${tag}>`; }); };
  listBlock(/(^|\n)((?:[ \t]*[-*] .*(?:\n|$))+)/g, "ul");
  listBlock(/(^|\n)((?:[ \t]*\d+\. .*(?:\n|$))+)/g, "ol");
  return s.split(/\n{2,}/).map(p => p.trim()).filter(Boolean)
    .map(p => /^<(h\d|ul|ol|blockquote)/.test(p) ? p : `<p>${p.replace(/\n/g, "<br>")}</p>`)
    .join("");
}
