"use strict";
/* Logs surface: the form model (pure; decides each outbox
 * event's level, category and day group from its served fields)
 * and the view that binds it. Loads after app.js — uses its
 * helpers, the merged state S, and the logs vocabulary
 * (LOG_LEVEL_BY_KIND / LOG_KINDS, which stay in app.js with the
 * other served-contract tables). poll.js feeds this module the
 * outbox portion through renderLogs / logsEvents. */

/* ---------- logs form model (pure; no DOM) ----------
 * Made the monitor's way: form follows the served shape. These
 * functions decide each outbox event's level, category and day
 * group from its served fields only; the view below binds the
 * model and owns labels and formatting.
 * The served contract is colab-lease-logs/v1: {seq, ts, kind,
 * detail} — level and shape derive from the event as served. */

function logLevel(ev) {
  return LOG_LEVEL_BY_KIND[ev.kind] || "info";
}

function logShape(ev) {
  const d = ev.detail || {};
  const r = d.result || {};
  // New events carry the shape; events spooled before the
  // retirement carry the legacy profile id — read both.
  return d.shape || d.profile || (r.record && (r.record.shape || r.record.profile)) || r.shape || r.profile || null;
}

function logSession(ev) {
  const d = ev.detail || {};
  const r = d.result || {};
  return d.session || (r.record && r.record.session) || d.new_session || null;
}

function logCategory(ev) {
  if (/^grant-/.test(ev.kind)) return "grants";
  if (/^budget/.test(ev.kind)) return "budget";
  return "leases";
}

function logDayKey(iso) {
  const t = new Date(iso);
  return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`;
}

function logDayLabel(key) {
  const today = logDayKey(new Date().toISOString());
  const yesterday = logDayKey(new Date(Date.now() - 86400000).toISOString());
  if (key === today) return "Today";
  if (key === yesterday) return "Yesterday";
  return new Date(`${key}T12:00:00`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

function groupByDay(events) {
  const groups = new Map();
  events.forEach((ev) => {
    const key = logDayKey(ev.ts);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(ev);
  });
  return [...groups.entries()]
    .sort((a, b) => (a[0] < b[0] ? 1 : -1))
    .map(([key, evs]) => ({ key, label: logDayLabel(key), events: evs.slice().sort((a, b) => b.seq - a.seq) }));
}

/** The variable part of an event — the kind is its own column and
 * the shape its own column, so this carries only what differs
 * per occurrence. */
function logDetailText(ev) {
  const d = ev.detail || {};
  const r = d.result || {};
  switch (ev.kind) {
    case "lease-started": {
      const rec = r.record || {};
      return [d.hours != null ? fmtDur(d.hours) : null,
        rec.deadline ? `deadline ${fmtTime(rec.deadline)}` : null].filter(Boolean).join(" · ");
    }
    case "lease-extended":
      return `+${fmtDur(d.hours)} · new deadline ${fmtTime(d.new_deadline)}`;
    case "lease-adopted":
      return d.deadline ? `deadline ${fmtTime(d.deadline)}` : "";
    case "keepalive-stale":
      return [d.stale_s != null ? `${d.stale_s}s stale` : null,
        d.pulse ? `pulse ${esc(d.pulse)}` : null].filter(Boolean).join(" · ");
    case "budget-refused":
      return [d.projected_cu != null ? `${fmtNum(d.projected_cu)} CU projected` : null,
        d.daily_cap_cu != null ? `daily ${fmtNum(d.daily_used_cu)} / ${fmtNum(d.daily_cap_cu)} CU` : null].filter(Boolean).join(" · ");
    case "grant-started":
      return `${fmtDur(d.hours)}, burst ${specLabel(d.burst || r.burst_shape || r.burst_profile)}`;
    case "envelope-lease-released":
      return d.reason ? esc(d.reason) : "";
    case "ui-restored":
      return [d.active_leases != null ? `${d.active_leases} active` : null,
        d.balance_cu != null ? `balance ${fmtNum(d.balance_cu)} CU` : null].filter(Boolean).join(" · ");
    default:
      return "";
  }
}

/* ---------- logs view (binds the form model) ---------- */

const logsView = { filter: "all", openEntries: new Set() };
try {
  logsView.filter = localStorage.getItem("lease-ui-logs-filter") || "all";
} catch (e) { /* defaults */ }
let logsEvents = [];

function fmtClock(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function fmtAge(iso) {
  const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function levelDot(level) {
  return `<span class="level-dot ${level}" role="img" aria-label="${level}" title="${level}"></span>`;
}

function renderLogsLegend() {
  $("logs-legend").innerHTML = ["ok", "info", "warn", "error"].map((l) =>
    `<span class="legend-item">${levelDot(l)} ${l}</span>`).join("");
}

/* The shipped form (owner's pick from the mockup, 2026-11-03):
 * timeline, grouped by day, relative time shown, compact density.
 * The pick is the form's constants, not a user-facing toggle. */
const LOG_VIEW = { form: "timeline", group_by_day: true, relative_time: true, compact: true };

function logDayBand(key) {
  const label = logDayLabel(key);
  const date = new Date(`${key}T12:00:00`).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  return `${label} · ${date}`;
}

function logEntryHtml(ev) {
  const shape = logShape(ev);
  const session = logSession(ev);
  const d = ev.detail || {};
  const caller = d.caller || (d.result || {}).caller || ((d.result || {}).record || {}).caller || null;
  const open = logsView.openEntries.has(ev.seq);
  const detail = logDetailText(ev);
  return `<div class="tl-entry">
    <button type="button" class="tl-row" data-seq="${ev.seq}" aria-expanded="${open ? "true" : "false"}">
      <span class="tl-time num">${fmtClock(ev.ts)}<span class="age muted" data-age="${esc(ev.ts)}">${fmtAge(ev.ts)}</span></span>
      <span class="tl-rail">${levelDot(logLevel(ev))}</span>
      <span class="tl-main">
        <span class="tl-top"><span class="kind-code">${esc(ev.kind)}</span></span>
        <span class="tl-who">${shape ? esc(specLabel(shape)) : '<span class="muted">—</span>'}${session ? `<span class="muted"> · ${esc(session)}</span>` : ""}${caller ? `<span class="muted"> · caller ${esc(caller)}</span>` : ""}</span>
        ${detail ? `<span class="tl-detail">${detail}</span>` : ""}
      </span>
    </button>
    ${open ? `<pre class="log-json">${esc(JSON.stringify(ev, null, 2))}</pre>` : ""}
  </div>`;
}

function renderLogs(events) {
  logsEvents = (events || []).filter((ev) => LOG_KINDS.has(ev.kind));
  renderLogsLegend();
  document.querySelectorAll("[data-lfilter]").forEach((b) =>
    b.classList.toggle("selected", b.getAttribute("data-lfilter") === logsView.filter));
  const shown = logsEvents.filter((ev) =>
    logsView.filter === "all" || logCategory(ev) === logsView.filter ||
    (logsView.filter === "errors" && logLevel(ev) === "error"));
  const body = $("logs-body");
  body.classList.toggle("logs-compact", LOG_VIEW.compact);
  if (!logsEvents.length) {
    body.innerHTML = `<div class="log-empty muted">No events yet.</div>`;
  } else if (!shown.length) {
    body.innerHTML = `<div class="log-empty muted">No events in this filter.</div>`;
  } else {
    body.innerHTML = groupByDay(shown).map((g) =>
      `<div class="tl-day">
        <div class="tl-band">${esc(logDayBand(g.key))}<span class="muted">${g.events.length} event${g.events.length === 1 ? "" : "s"}</span></div>
        ${g.events.map(logEntryHtml).join("")}
      </div>`).join("");
  }
  $("logs-foot").textContent = `Showing ${shown.length} of ${logsEvents.length}`;
}

$("logs-body").addEventListener("click", (e) => {
  const row = e.target.closest("[data-seq]");
  if (row) {
    const seq = Number(row.getAttribute("data-seq"));
    if (logsView.openEntries.has(seq)) logsView.openEntries.delete(seq);
    else logsView.openEntries.add(seq);
    renderLogs(logsEvents);
  }
});

$("logs-filters").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-lfilter]");
  if (!btn) return;
  logsView.filter = btn.getAttribute("data-lfilter");
  try { localStorage.setItem("lease-ui-logs-filter", logsView.filter); } catch (err) { /* session only */ }
  renderLogs(logsEvents);
});
