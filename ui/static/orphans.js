"use strict";
/* Orphaned sessions: live runtimes with no lease record, served
 * by the core's orphans portion of /api/state. The section renders
 * only what the portion serves — the derived deadline with its
 * evidence source, the conflicts the derivation found, the
 * evidenced shape or the candidate list — and Adopt posts to the
 * core's adopt verb through the panel endpoint. Rendering lives
 * here rather than in app.js: this module loads after the app.js
 * split (app.js, logs.js, controls.js, poll.js, theme.js) and
 * uses app.js's helpers and the merged state S; poll.js's
 * renderSections calls renderOrphans when the orphans or specs
 * portion lands. */

/* The operator's shape pick for a class-ambiguous orphan lives
 * outside the render (same rule as the extend pickers), keyed by
 * session, so a poll re-render never loses it. */
const orphanShapePick = {};

/* The jobs-expander open state, keyed by session — the same
 * rule as the shape pick: a portion re-render never collapses
 * an expander the operator opened, and one card's state never
 * renders on another. */
const orphanJobsOpen = {};

/* Age of a served timestamp, in countdownText's grammar
 * without the "left": 8m · 1h 44m · 2d 3h. */
function fmtAge(iso) {
  if (!iso) return "—";
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms) || ms < 0) return "—";
  const mins = Math.round(ms / 60000);
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
  return h > 0 ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`;
}

function jobRowsHTML(entries) {
  return (entries || []).map((e) =>
    `<p class="jobrow"><span class="jid">${esc(e.id)}</span> · ${esc(e.kind)} · ${esc(e.state)} · ${esc(fmtAge(e.started_at))}</p>`).join("");
}

/* The in-flight jobs block: the served count as a disclosure
 * line, the kinded entries listed in place when opened. The
 * count is a number the server computed — the client never
 * derives it from the entries, and the entries render only
 * inside this expander. */
function orphanJobsHTML(o) {
  const open = orphanJobsOpen[o.session] ? " open" : "";
  return `<details class="jobs" data-jobs-for="${esc(o.session)}"${open}>
    <summary>In-flight jobs <b>${esc(o.jobs_in_flight ?? 0)}</b></summary>
    <div class="jobrows">${jobRowsHTML(o.jobs_in_flight_entries)}</div>
  </details>`;
};

function adoptRefusalText(res) {
  switch (res.action) {
    case "adopt-deadline-past":
      return "the derived deadline has already passed — this orphan needs a planted deadline (CLI)";
    case "adopt-deadline-shortens":
      return "a planted deadline may not precede the evidenced one — adoption restores; shorten after adopting";
    case "adopt-no-deadline":
      return "no deadline evidence and none planted — adoption from the panel needs a derived deadline";
    case "adopt-shape-ambiguous":
      return "the shape class matches several catalog shapes — pick the shape first";
    case "adopt-shape-conflict":
      return "the picked shape contradicts the evidenced shape";
    case "adopt-shape-unknown":
      return "the picked shape is not in the catalog";
    case "adopt-has-record":
      return "a lease record now exists for this session — it is not an orphan anymore";
    case "adopt-absent":
      return "the session is no longer live";
    case "adopt-unknown":
      return "session state unreadable — nothing changed";
    case "adopt-handover-pending":
      return "a handover child — its parent record takes it over by swap";
    case "adopt-bridge-managed":
      return "bridge-managed — not the lease system's to adopt";
    case "refused-budget":
      return "budget refused the planted extension";
    case "lease-disabled":
      return "the lease is disabled in config";
    default:
      return res.action || "failed";
  }
}

function reportAdopt(res, orphan) {
  if (res.action === "adopted") {
    delete orphanShapePick[orphan.session];
    const deadline = (res.record && res.record.deadline) || orphan.derived_deadline;
    notice(`Adopted — ${orphan.session} is a lease again${deadline ? `, deadline ${fmtTime(deadline)}` : ""}`);
  } else {
    notice(`Adoption refused: ${adoptRefusalText(res)}`);
  }
}

function orphanConflictValue(v) {
  const s = String(v ?? "—");
  return /^\d{4}-\d{2}-\d{2}T/.test(s) ? fmtTime(s) : s;
}

function orphanShapeHTML(o) {
  if (o.shape_evidenced) {
    return `<p class="kvline">Shape <b>${esc(specLabel(o.shape))}</b> · evidenced</p>`;
  }
  const candidates = Array.isArray(o.shape_candidates) ? o.shape_candidates : [];
  const pick = orphanShapePick[o.session] || (candidates.length === 1 ? candidates[0] : null);
  const rows = candidates.map((id) => `
    <button type="button" class="spec${id === pick ? " selected" : ""}" data-orphan-shape="${esc(o.session)}:${esc(id)}">
      <span><b>${esc(specLabel(id))}</b></span>
      <span class="cost num">${esc(specRateText(specById(id) || {}))}</span>
    </button>`).join("");
  return `<p class="kvline">Shape class <b>${esc(o.shape_class || "unknown")}</b> — not evidenced; pick the shape to adopt:</p>
    <div class="spec-list">${rows || `<p class="kvline muted">No catalog candidates for this class.</p>`}</div>`;
}

function orphanCardHTML(o) {
  const conflicts = (Array.isArray(o.conflicts) ? o.conflicts : []).map((c) => {
    const values = (Array.isArray(c.values) ? c.values : [])
      .map((v) => `${esc(v.source)} says <b>${esc(orphanConflictValue(v.value))}</b>`)
      .join(" · ");
    return `<p class="kvline">Conflict — ${esc(c.field)}: ${values}</p>`;
  }).join("");
  /* The headline: a future deadline counts down (the ticker
   * owns [data-countdown] elements); a past one is a fact, not
   * a countdown — the deadline itself as a time with a
   * past-deadline chip, and no data-countdown attribute, so
   * the ticker never overwrites it with "expired". */
  const dl = o.derived_deadline;
  const past = dl && new Date(dl).getTime() <= Date.now();
  const headline = !dl
    ? ""
    : past
      ? `<p class="big num">${esc(fmtTime(dl))}<span class="chip warn">past deadline</span></p>`
      : `<p class="big num" data-countdown="${esc(dl)}">${esc(countdownText(dl))}</p>`;
  const jobsCount = o.jobs_in_flight ?? 0;
  return `<div class="card">
    <div class="row"><b>${esc(o.session || "session")}</b><span class="chip warn">Orphaned</span></div>
    ${headline}
    <p class="kvline">First seen <b>${esc(fmtTime(o.first_seen))}</b>${o.acquired_at ? ` · Acquired <b>${esc(fmtTime(o.acquired_at))}</b>` : ""}${jobsCount === 0 ? ` · In-flight jobs <b>0</b>` : ""}${o.kit_installed ? ` · Renewal kit <b>installed</b>` : ""}</p>
    ${jobsCount > 0 ? orphanJobsHTML(o) : ""}
    ${orphanShapeHTML(o)}
    <p class="kvline">Creator caller <b>${esc(o.creator_caller || "—")}</b>${o.first_claim && o.first_claim !== o.creator_caller ? ` · first claim <b>${esc(o.first_claim)}</b>` : ""}</p>
    ${o.derived_deadline
      ? `<p class="kvline">Derived deadline <b>${esc(fmtTime(o.derived_deadline))}</b> · from <b>${esc(o.deadline_source || "—")}</b>${o.deadline_stale_possible ? ` <span class="chip warn">possibly stale</span>` : ""}</p>`
      : `<p class="kvline">Derived deadline <b>none</b> — no source carries one for this session</p>`}
    ${conflicts}
    ${o.handover_pending ? `<p class="kvline"><span class="chip warn">Handover pending</span> its parent record takes it over by swap — adoption is refused.</p>` : ""}
    <button type="button" class="primary" data-adopt="${esc(o.session || "")}"${o.handover_pending ? " disabled" : ""}>Adopt</button>
  </div>`;
}

function renderOrphans() {
  const section = $("slot-orphans");
  const box = $("orphan-cards");
  if (!section || !box) return;
  const orphans = (S && S.orphans) || [];
  section.hidden = !orphans.length;
  if (!orphans.length) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = orphans.map(orphanCardHTML).join("");
  box.querySelectorAll("details.jobs").forEach((d) => {
    d.addEventListener("toggle", () => {
      orphanJobsOpen[d.getAttribute("data-jobs-for")] = d.open;
    });
  });
  box.querySelectorAll("[data-orphan-shape]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const [session, id] = btn.getAttribute("data-orphan-shape").split(":");
      orphanShapePick[session] = id;
      renderOrphans();
    });
  });
  box.querySelectorAll("[data-adopt]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const session = btn.getAttribute("data-adopt");
      const orphan = orphans.find((o) => o.session === session);
      if (!orphan) return;
      const body = { session };
      if (!orphan.shape_evidenced) {
        const candidates = Array.isArray(orphan.shape_candidates) ? orphan.shape_candidates : [];
        const pick = orphanShapePick[session] || (candidates.length === 1 ? candidates[0] : null);
        if (!pick) {
          notice("Adoption refused: the shape class matches several catalog shapes — pick the shape first");
          return;
        }
        body.shape = pick;
      }
      act(btn, async () => reportAdopt(await apiResult("/api/lease/adopt", body), orphan));
    });
  });
}
