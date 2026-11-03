"use strict";
/* Lease panel. Everything displayed is served by the lease core via
 * /api/state; the only client-side derivations are countdown
 * arithmetic on served timestamps and projected-cost arithmetic over
 * the served rate table. Layout and content follow the approved
 * mockup (ui/mockups/2026-11-01-lease-lifecycle-mockup.html). */

let S = null; // last /api/state payload
const sel = {
  mode: "normal",
  profile: null, // chosen spec id, normal mode
  burst: null, // chosen spec id, grant mode
  normalHours: 8,
  grantHours: 8,
};

/* Extend pickers (lease cards + grant card) reuse the shared
 * duration set; selection and open state live outside the render so
 * a poll re-render does not lose them. */
const EXT_DURATIONS = [1, 2, 4, 8, 12, 16, 24, 48, 72];
const extHours = {}; // state key -> hours
const extOpen = new Set(); // state keys whose picker is open

/* Budget cap editor: open state and the typed drafts live outside
 * the render (same rule as the extend pickers), so a poll re-render
 * never loses them. A draft is the input's string; "" means no cap. */
let capEditOpen = false;
const capDraft = { daily: "", monthly: "", standing: "" };
const CAP_FIELDS = { daily: "daily_cu_budget", monthly: "monthly_cu_budget", standing: "standing_daily_cu_cap" };

function capText(v) {
  return v == null ? "no cap" : `${fmtNum(v)} CU`;
}

function extKey(kind, key) {
  return kind === "grant" ? "grant" : `lease:${key}`;
}

const $ = (id) => document.getElementById(id);

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtNum(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  return String(Math.round(Number(v) * 100) / 100);
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? String(iso)
    : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function fmtDur(hours) {
  const h = Number(hours);
  if (!Number.isFinite(h)) return "—";
  return h >= 24 && h % 24 === 0 ? `${h / 24}d` : `${h}h`;
}

function countdownText(iso) {
  if (!iso) return "—";
  const ms = new Date(iso).getTime() - Date.now();
  if (Number.isNaN(ms)) return "—";
  if (ms <= 0) return "expired";
  const mins = Math.round(ms / 60000);
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h left`;
  return h > 0 ? `${h}h ${String(m).padStart(2, "0")}m left` : `${m}m left`;
}

async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const resp = await fetch(path, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

function notice(msg) {
  const el = $("notice");
  el.hidden = !msg;
  el.textContent = msg || "";
}

async function act(btn, fn) {
  btn.disabled = true;
  notice("");
  try {
    await fn();
  } catch (err) {
    notice(String(err.message || err));
  } finally {
    btn.disabled = false;
    await poll();
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ---------- specs ---------- */

/* Specs come from the core's state.specs — every grantable tier with
 * its served label, shape, and rate (null when the core cannot price
 * it). The fallback derives the same shape from profiles + caps for
 * an older core; a label falls back to the profile id, matching the
 * core's own fallback in specs(). */
function specsFromState() {
  if (S && Array.isArray(S.specs) && S.specs.length) return S.specs;
  const profiles = ((S && S.profiles) || {}).profiles || {};
  const rates = ((S && S.caps) || {}).rate_cu_per_hour || {};
  return Object.entries(profiles).map(([id, p]) => ({
    id,
    label: p.label || id,
    session: p.session,
    gpu: p.gpu || null,
    tpu: p.tpu || null,
    high_mem: !!p.high_mem,
    rate_cu_per_hour: p.gpu ? null : p.high_mem ? rates.high_mem : rates.standard,
    timeout: p.timeout,
  }));
}

function specById(id) {
  return specsFromState().find((s) => s.id === id) || null;
}

function specLabel(id) {
  const spec = specById(id);
  return spec ? spec.label || spec.id : id || "—";
}

/* "High-RAM · CPU" / "Lobby · CPU · Standard" / "T4 · GPU · T4": the
 * descriptor names kind + shape, except that a CPU shape already
 * named by the label is not repeated. */
function specDescriptor(spec) {
  const kind = spec.gpu ? "GPU" : spec.tpu ? "TPU" : "CPU";
  const shape = spec.gpu || spec.tpu || (spec.high_mem ? "High-RAM" : "Standard");
  return !spec.gpu && !spec.tpu && shape === (spec.label || spec.id) ? kind : `${kind} · ${shape}`;
}

function specTitle(spec) {
  return `${spec.label || spec.id} · ${specDescriptor(spec)}`;
}

function specRateText(spec) {
  return spec.rate_cu_per_hour != null ? `${fmtNum(spec.rate_cu_per_hour)} CU/h` : "Cost unconfirmed";
}

/* ---------- rendering ---------- */

function capInputRow(label, key) {
  return `<div class="row cap-row"><span class="lab">${label}</span>
    <input class="cap-input num" data-cap="${key}" type="number" min="0" step="0.5" placeholder="No cap" value="${esc(capDraft[key])}"></div>`;
}

function renderBudget() {
  const b = (S && S.budget) || {};
  const caps = b.caps || {};
  const month = new Date().toLocaleString([], { month: "long" });
  const spent = (val, cap) =>
    val == null ? "—" : `${fmtNum(val)}${cap != null ? ` of ${fmtNum(cap)}` : ""} CU`;
  const editor = capEditOpen
    ? `<div class="cap-box">
        ${capInputRow("Daily cap", "daily")}
        ${capInputRow("Monthly cap", "monthly")}
        ${capInputRow("Standing daily cap", "standing")}
        <p class="kvline">Blank = no cap. Caps gate new leases and extensions; a running lease is never cut off by a cap change.</p>
        <button type="button" class="primary block" id="cap-save">Save caps</button>
        <button type="button" class="ghost" id="cap-cancel">Cancel</button>
      </div>`
    : `<button type="button" class="ghost" id="cap-edit-toggle">Edit caps</button>`;
  $("budget-card").innerHTML = `<div class="card">
    <div class="row"><span class="lab">Balance</span><b class="num">${b.balance == null ? "—" : `${fmtNum(b.balance)} CU`}</b></div>
    <div class="row"><span class="lab">Today</span><span class="num">${spent(b.day_spent, caps.daily)}</span></div>
    <div class="row"><span class="lab">${esc(month)}</span><span class="num">${spent(b.month_spent, caps.monthly)}</span></div>
    ${editor}
  </div>`;
  const chip = $("balance-chip");
  chip.hidden = b.balance == null;
  chip.textContent = `Balance ${fmtNum(b.balance)} CU`;
  const toggle = $("cap-edit-toggle");
  if (toggle) {
    toggle.addEventListener("click", () => {
      capDraft.daily = caps.daily == null ? "" : String(caps.daily);
      capDraft.monthly = caps.monthly == null ? "" : String(caps.monthly);
      capDraft.standing = caps.standing_daily == null ? "" : String(caps.standing_daily);
      capEditOpen = true;
      renderBudget();
    });
  }
  $("budget-card").querySelectorAll("[data-cap]").forEach((input) => {
    input.addEventListener("input", (e) => {
      capDraft[e.target.getAttribute("data-cap")] = e.target.value;
    });
  });
  const cancel = $("cap-cancel");
  if (cancel) {
    cancel.addEventListener("click", () => {
      capEditOpen = false;
      renderBudget();
    });
  }
  const save = $("cap-save");
  if (save) {
    save.addEventListener("click", () => {
      const body = {};
      for (const [key, field] of Object.entries(CAP_FIELDS)) {
        const raw = String(capDraft[key] ?? "").trim();
        if (raw === "") {
          body[field] = null;
          continue;
        }
        const v = Number(raw);
        if (!Number.isFinite(v) || v < 0) {
          notice("Caps not saved — each cap must be a non-negative number, or blank for no cap.");
          return;
        }
        body[field] = v;
      }
      act(save, async () => {
        const res = await api("/api/config/budget", body);
        capEditOpen = false;
        const c = res.caps || {};
        notice(`Budget caps updated — daily ${capText(c.daily)}, monthly ${capText(c.monthly)}, standing ${capText(c.standing_daily)}`);
      });
    });
  }
}

function leaseChip(entry) {
  const rec = entry.record || {};
  if (rec.standing) return `<span class="chip ok">Standing</span>`;
  if (entry.action === "active") return `<span class="chip ok">Active</span>`;
  if (entry.action === "recorded-but-dead") return `<span class="chip bad">Recorded · dead</span>`;
  return `<span class="chip warn">${esc(entry.action || "unknown")}</span>`;
}

/* The extend picker: the shared duration chips plus a custom-hours
 * input and an apply button, rendered into a card when its Extend
 * toggle is open. kind is "lease" (key = profile) or "grant". */
function extendPickerHTML(kind, key) {
  const hours = extHours[extKey(kind, key)] ?? 8;
  const chips = EXT_DURATIONS.map((h) =>
    `<button type="button" data-xhours="${h}" class="${h === hours ? "selected" : ""}">${h === 24 ? "24h" : fmtDur(h)}</button>`).join("");
  return `<div class="extend-box">
    <div class="chips" data-extpicker="${esc(kind)}:${esc(key)}">
      ${chips}
      <input class="custom" data-xcustom type="number" min="0.25" step="0.25" placeholder="Custom (hours)">
    </div>
    <button type="button" class="primary block" data-xapply="${esc(kind)}:${esc(key)}" style="margin-top:10px">Extend by ${esc(fmtDur(hours))}</button>
  </div>`;
}

/* Refusals surface the core's reason — the extend endpoints answer
 * 200 with the refusal action, so the page reads it and says why. */
function extendRefusalText(res) {
  const budget = res.budget || {};
  switch (res.action) {
    case "refused-budget":
      return `budget refused (${budget.reason || "over cap"}${budget.projected != null ? `, projected ${fmtNum(budget.projected)} CU` : ""})`;
    case "extend-unknown":
      return "session state unreadable — nothing changed";
    case "extend-no-lease":
      return "no live lease to extend";
    case "extend-standing-managed":
      return "the lobby is grant-managed — extend the grant instead";
    case "grant-inactive":
      return "no active grant to extend";
    default:
      return res.action || "failed";
  }
}

function reportExtend(res, kind, key) {
  if (res.action === "extended") {
    extOpen.delete(extKey(kind, key));
    notice(`Extended — new deadline ${fmtTime(res.new_deadline)}`);
  } else if (res.action === "grant-extended") {
    extOpen.delete("grant");
    notice(`Grant extended — until ${fmtTime(res.new_until)}`);
  } else {
    notice(`Extension refused: ${extendRefusalText(res)}`);
  }
}

function wireExtendPickers(container) {
  container.querySelectorAll("[data-extpicker]").forEach((row) => {
    const [kind, key] = row.getAttribute("data-extpicker").split(":");
    const stateKey = extKey(kind, key);
    row.querySelectorAll("button[data-xhours]").forEach((btn) => {
      btn.addEventListener("click", () => {
        extHours[stateKey] = parseFloat(btn.getAttribute("data-xhours"));
        if (kind === "grant") renderGrant(); else renderLeases();
      });
    });
    const custom = row.querySelector("[data-xcustom]");
    if (custom) {
      custom.addEventListener("input", (e) => {
        const v = parseFloat(e.target.value);
        if (v > 0) {
          extHours[stateKey] = v;
          row.querySelectorAll("button").forEach((b) => b.classList.remove("selected"));
          const apply = container.querySelector(`[data-xapply="${kind}:${key}"]`);
          if (apply) apply.textContent = `Extend by ${fmtDur(v)}`;
        }
      });
    }
  });
  container.querySelectorAll("[data-xapply]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const [kind, key] = btn.getAttribute("data-xapply").split(":");
      const hours = extHours[extKey(kind, key)] ?? 8;
      if (kind === "grant") {
        act(btn, async () => reportExtend(await api("/api/grant/extend", { hours }), "grant", ""));
      } else {
        act(btn, async () => reportExtend(await api("/api/lease/extend", { profile: key, hours }), "lease", key));
      }
    });
  });
}

/* A toggled-on instance with no lease yet gets a dormant card with
 * its own Start; once it runs, its record renders the ordinary
 * lease card (deadline, Terminate, Extend) like any profile. Start
 * and stop ride the existing lease endpoints with the instance
 * profile id the core served — no instance endpoints exist. */
function dormantInstanceCards(records) {
  const live = new Set(records.map((e) => e.profile || (e.record || {}).profile));
  return specsFromState()
    .filter((s) => s.parallel && s.parallel.enabled && s.parallel.instance && !live.has(s.parallel.instance))
    .map((s) => {
      const inst = specById(s.parallel.instance);
      const title = inst ? specTitle(inst) : s.parallel.instance;
      return `<div class="card">
      <div class="row"><b>${esc(title)}</b><span class="chip warn">Not running</span></div>
      <p class="kvline">Parallel instance of <b>${esc(specLabel(s.id))}</b> · session <span class="mono">${esc(inst ? inst.session || "—" : "—")}</span> · Burn <b>${esc(inst ? specRateText(inst) : "—")}</b></p>
      <button type="button" class="primary" data-istart="${esc(s.parallel.instance)}">Start</button>
    </div>`;
    }).join("");
}

function renderLeases() {
  const records = (S && S.records) || [];
  const box = $("lease-cards");
  box.innerHTML = records.map((entry) => {
    const rec = entry.record || {};
    const profile = entry.profile || rec.profile || "";
    const spec = specById(profile);
    const title = spec ? specTitle(spec) : profile || "lease";
    const age = entry.keepalive_heartbeat_age_s;
    const pulse = rec.last_pulse_result;
    const burn = spec ? specRateText(spec) : "—";
    return `<div class="card">
      <div class="row"><b>${esc(title)}</b>${leaseChip(entry)}</div>
      <p class="big num" data-countdown="${esc(rec.deadline || "")}">${esc(countdownText(rec.deadline))}</p>
      <p class="kvline">Deadline <b>${esc(fmtTime(rec.deadline))}</b> · session <span class="mono">${esc(rec.session || "—")}</span></p>
      <p class="kvline">Heartbeat <b>${age == null ? "unknown" : `${Math.round(age)}s ago`}</b> · Pulse <b>${esc(pulse || "—")}</b> · Burn <b>${esc(burn)}</b></p>
      <button type="button" class="danger" data-stop="${esc(profile)}">Terminate</button>
      <button type="button" class="ghost" data-xtoggle="${esc(profile)}">Extend</button>
      ${extOpen.has(extKey("lease", profile)) ? extendPickerHTML("lease", profile) : ""}
    </div>`;
  }).join("") + dormantInstanceCards(records);
  box.querySelectorAll("[data-istart]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const profile = btn.getAttribute("data-istart");
      act(btn, () => api("/api/lease/start", { profile, hours: sel.normalHours }));
    });
  });
  box.querySelectorAll("[data-stop]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const profile = btn.getAttribute("data-stop");
      if (!window.confirm(`Terminate the ${specLabel(profile)} lease now?`)) return;
      act(btn, () => api("/api/lease/stop", { profile }));
    });
  });
  box.querySelectorAll("[data-xtoggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = extKey("lease", btn.getAttribute("data-xtoggle"));
      if (extOpen.has(key)) extOpen.delete(key); else extOpen.add(key);
      renderLeases();
    });
  });
  wireExtendPickers(box);
}

function renderGrant() {
  const grant = S && S.grant;
  const box = $("grant-card");
  const active = grant && typeof grant === "object" && grant.until;
  if (!active) {
    box.innerHTML = "";
    $("grant-form").hidden = false;
    $("grant-active").hidden = true;
    return;
  }
  const caps = ((S && S.budget) || {}).caps || {};
  // The envelope block is served by the core with the grant: its
  // scope (snapshot caps), its attributed leases, and spend versus
  // the snapshot caps. The card renders only what it serves.
  const env = (S && S.envelope) || null;
  const envCaps = (env && env.caps) || caps;
  const envLeases = env && Array.isArray(env.records) ? env.records : [];
  const envSpend = (env && env.spend) || {};
  const leaseRows = envLeases.length
    ? envLeases.map((rec) => `<p class="kvline">· <b>${esc(specLabel(rec.profile))}</b>${rec.via ? ` via <b>${esc(rec.via)}</b>` : ""} · until <b>${esc(fmtTime(rec.deadline))}</b></p>`).join("")
    : `<p class="kvline">No envelope leases yet.</p>`;
  box.innerHTML = `<div class="card">
    <div class="row"><b>Two-tier grant</b><span class="chip ok">Active</span></div>
    <p class="kvline" style="margin-top:8px">Until <b>${esc(fmtTime(grant.until))}</b> · Burst spec <b>${esc(specLabel(grant.burst_profile))}</b></p>
    <p class="kvline">${S.budget && S.budget.standing_day_spent != null ? `Standing spend today <b>${fmtNum(S.budget.standing_day_spent)} of ${envCaps.standing_daily == null ? "—" : fmtNum(envCaps.standing_daily)} CU</b>` : `Standing cap <b>${envCaps.standing_daily == null ? "—" : `${fmtNum(envCaps.standing_daily)} CU/day`}</b>`}</p>
    ${env ? `<p class="kvline">Envelope caps (at grant) — daily <b>${envCaps.daily == null ? "—" : `${fmtNum(envCaps.daily)} CU`}</b> · monthly <b>${envCaps.monthly == null ? "—" : `${fmtNum(envCaps.monthly)} CU`}</b></p>
    <p class="kvline">Envelope spend — today <b>${envSpend.day_spent == null ? "—" : `${fmtNum(envSpend.day_spent)} CU`}</b> · this month <b>${envSpend.month_spent == null ? "—" : `${fmtNum(envSpend.month_spent)} CU`}</b></p>
    <p class="kvline" style="margin-top:6px"><b>Envelope leases</b></p>
    ${leaseRows}` : ""}
    <button type="button" class="ghost" id="ungrant-btn">Revoke grant</button>
    <button type="button" class="ghost" id="grant-extend-toggle">Extend grant</button>
    ${extOpen.has("grant") ? extendPickerHTML("grant", "") : ""}
  </div>`;
  $("ungrant-btn").addEventListener("click", (e) => {
    if (!window.confirm("Revoke the two-tier grant now?")) return;
    act(e.currentTarget, () => api("/api/ungrant", {}));
  });
  $("grant-extend-toggle").addEventListener("click", () => {
    if (extOpen.has("grant")) extOpen.delete("grant"); else extOpen.add("grant");
    renderGrant();
  });
  wireExtendPickers(box);
  $("grant-form").hidden = true;
  $("grant-active").hidden = false;
  $("grant-until").textContent = fmtTime(grant.until);
}

/* The Parallel toggle rides the spec row of every eligible spec,
 * bound to the served parallel block (eligibility, state, instance
 * id are the core's; the page derives none of them). Ineligible
 * specs — every GPU/TPU shape, and instances themselves — render
 * no toggle. */
function parallelToggleHTML(s) {
  const p = s.parallel;
  if (!p || !p.eligible) return "";
  return `<span class="chip ptoggle${p.enabled ? " on" : ""}" data-parallel="${esc(s.id)}" title="Allow a second, parallel session of this spec">Parallel ${p.enabled ? "on" : "off"}</span>`;
}

function renderSpecList(containerId, key) {
  const specs = specsFromState();
  const box = $(containerId);
  if (!sel[key] || !specs.some((s) => s.id === sel[key])) {
    sel[key] = ((S && S.profiles) || {}).default_profile || (specs[0] && specs[0].id) || null;
  }
  box.innerHTML = specs.map((s) => `
    <button type="button" class="spec${s.id === sel[key] ? " selected" : ""}" data-spec="${esc(s.id)}">
      <span><b>${esc(s.label || s.id)}</b> · ${esc(specDescriptor(s))}</span>
      <span class="cost num">${esc(specRateText(s))}</span>${parallelToggleHTML(s)}
    </button>`).join("");
}

function renderControls() {
  renderSpecList("profile-specs", "profile");
  renderSpecList("burst-specs", "burst");
  const chosen = specById(sel.profile);
  const rate = chosen ? chosen.rate_cu_per_hour : null;
  $("normal-projected").textContent =
    rate == null ? "Cost unconfirmed" : `${fmtNum(rate * sel.normalHours)} CU`;
  renderGrant();
}

function eventText(ev) {
  const d = ev.detail || {};
  const r = d.result || {};
  const time = fmtTime(ev.ts);
  const profile = d.profile || (r.record && r.record.profile) || r.profile;
  const hours = d.hours != null ? `, ${fmtDur(d.hours)}` : "";
  switch (ev.kind) {
    case "lease-started":
      return `Lease started — ${specLabel(profile)}${hours} · ${time}`;
    case "lease-stopped":
      return `Lease released — ${specLabel(profile)}${r.reason ? `, ${r.reason}` : ""} · ${time}`;
    case "lease-start-failed":
      return `Lease start failed — ${specLabel(profile)} · ${time}`;
    case "lease-extended":
      return `Lease extended — ${specLabel(profile)} +${fmtDur(d.hours)} · new deadline ${fmtTime(d.new_deadline)} · ${time}`;
    case "lease-extend-failed":
      return `Lease extension failed — ${specLabel(profile)} · ${time}`;
    case "lease-renewed":
      return `Lease renewed — ${specLabel(profile)} · ${esc(d.old_session || "")} → ${esc(d.new_session || "")} · ${time}`;
    case "grant-started":
      return `Grant started — ${fmtDur(d.hours)}, burst ${specLabel(d.burst || r.burst_profile)} · ${time}`;
    case "grant-extended":
      return `Grant extended — +${fmtDur(d.hours)} · until ${fmtTime(d.new_until)} · ${time}`;
    case "grant-extend-failed":
      return `Grant extension failed · ${time}`;
    case "grant-ended":
      return `Grant ended · ${time}`;
    case "envelope-lease-started":
      return `Envelope lease started — ${specLabel(profile)}${d.via ? ` via ${esc(d.via)}` : ""} · ${time}`;
    case "envelope-lease-released":
      return `Envelope lease released — ${specLabel(profile)}${d.reason ? `, ${esc(d.reason)}` : ""} · ${time}`;
    case "parallel-changed":
      return `Parallel ${d.enabled ? "enabled" : "disabled"} — ${specLabel(profile)} · ${time}`;
    case "budget-caps-changed": {
      const c = d.caps || {};
      return `Budget caps changed — daily ${capText(c.daily)}, monthly ${capText(c.monthly)}, standing ${capText(c.standing_daily)} · ${time}`;
    }
    default:
      return `${ev.kind} · ${time}`;
  }
}

function renderEvents(events) {
  const list = $("event-list");
  if (!events.length) {
    list.innerHTML = `<div class="event muted">No events yet.</div>`;
    return;
  }
  list.innerHTML = events.slice(-20).reverse()
    .map((ev) => `<div class="event">${esc(eventText(ev))}</div>`).join("");
}

function render(events) {
  renderBudget();
  renderLeases();
  renderControls();
  if (events) renderEvents(events);
}

/* ---------- controls wiring ---------- */

function wireSpecList(containerId, key) {
  $(containerId).addEventListener("click", (e) => {
    const toggle = e.target.closest("[data-parallel]");
    if (toggle) {
      // The toggle is not a spec selection: it flips the served
      // toggle state through the config endpoint. A core refusal
      // answers 200 with its action (the extend pattern), so the
      // page reads it and says why.
      const profile = toggle.getAttribute("data-parallel");
      const spec = specById(profile);
      const enabled = !(spec && spec.parallel && spec.parallel.enabled);
      act(toggle.closest("button") || toggle, async () => {
        const res = await api("/api/config/parallel", { profile, enabled });
        if (res.action === "parallel-updated") {
          notice(`Parallel ${enabled ? "enabled" : "disabled"} — ${specLabel(profile)}`);
        } else {
          notice(`Parallel toggle refused: ${res.action || "failed"}`);
        }
      });
      return;
    }
    const row = e.target.closest("[data-spec]");
    if (!row) return;
    sel[key] = row.getAttribute("data-spec");
    renderControls();
  });
}

function wirePresets(rowId, customId, key) {
  const row = $(rowId);
  row.querySelectorAll("button[data-hours]").forEach((btn) => {
    btn.addEventListener("click", () => {
      sel[key] = parseFloat(btn.getAttribute("data-hours"));
      $(customId).value = "";
      row.querySelectorAll("button").forEach((b) => b.classList.toggle("selected", b === btn));
      renderControls();
    });
  });
  $(customId).addEventListener("input", (e) => {
    const v = parseFloat(e.target.value);
    if (v > 0) {
      sel[key] = v;
      row.querySelectorAll("button").forEach((b) => b.classList.remove("selected"));
      renderControls();
    }
  });
}

$("mode-normal").addEventListener("click", () => setMode("normal"));
$("mode-grant").addEventListener("click", () => setMode("grant"));
function setMode(mode) {
  sel.mode = mode;
  $("mode-normal").classList.toggle("selected", mode === "normal");
  $("mode-grant").classList.toggle("selected", mode === "grant");
  $("normal-controls").hidden = mode !== "normal";
  $("grant-controls").hidden = mode !== "grant";
}

$("start-btn").addEventListener("click", (e) => {
  act(e.currentTarget, () => api("/api/lease/start", { profile: sel.profile, hours: sel.normalHours }));
});
$("grant-btn").addEventListener("click", (e) => {
  act(e.currentTarget, () => api("/api/grant", { hours: sel.grantHours, burst: sel.burst }));
});

/* ---------- polling & the degraded state ---------- */

function setDegraded(on) {
  $("degraded").hidden = !on;
  $("layout").hidden = on;
  if (on) $("balance-chip").hidden = true;
}

async function poll() {
  try {
    const [state, outbox] = await Promise.all([api("/api/state"), api("/api/outbox?after=0")]);
    S = state;
    setDegraded(false);
    render(outbox.events || []);
    return true;
  } catch (err) {
    setDegraded(true);
    return false;
  }
}

$("retry-btn").addEventListener("click", (e) => act(e.currentTarget, () => poll()));

$("restart-btn").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  try {
    // The restart stops the very server answering this, so a failed
    // POST is not fatal: what matters is the server coming back.
    await api("/api/server/restart", {}).catch(() => {});
    for (let i = 0; i < 20; i++) {
      await sleep(1500);
      if (await poll()) break;
    }
  } finally {
    btn.disabled = false;
  }
});

/* ---------- theme (palette + mode; on-device only) ----------
 * Presentation state, not lease state: stored in localStorage,
 * applied as data-palette / data-mode (resolved) on <html>. The
 * boot script in index.html applies it before first paint; this
 * module owns changes and keeps the top-bar menu and the Settings
 * card in sync. The server never sees the theme. */

const THEME_PALETTES = [
  { id: "olive", name: "Olive — current default", dots: ["#edeee3", "#556b2f", "#22251a"] },
  { id: "colab", name: "Colab — Material + amber", dots: ["#f8f9fa", "#f9ab00", "#202124"] },
  { id: "google", name: "Google — Material blue", dots: ["#f8f9fa", "#1a73e8", "#202124"] },
  { id: "teal", name: "Teal — workbench second palette", dots: ["#e9f2f0", "#0f766e", "#12211e"] },
];
const theme = {
  palette: document.documentElement.getAttribute("data-palette") || "olive",
  mode: "system",
};
try { theme.mode = localStorage.getItem("lease-ui-mode") || "system"; } catch (e) { /* defaults */ }

function resolvedMode() {
  if (theme.mode !== "system") return theme.mode;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyTheme() {
  document.documentElement.setAttribute("data-palette", theme.palette);
  document.documentElement.setAttribute("data-mode", resolvedMode());
  try {
    localStorage.setItem("lease-ui-palette", theme.palette);
    localStorage.setItem("lease-ui-mode", theme.mode);
  } catch (e) { /* storage unavailable: theme lasts the session */ }
  document.querySelectorAll("[data-tmode]").forEach((b) =>
    b.classList.toggle("selected", b.getAttribute("data-tmode") === theme.mode));
  document.querySelectorAll("[data-palette-row]").forEach((row) => {
    const on = row.getAttribute("data-palette-row") === theme.palette;
    row.setAttribute("aria-checked", on ? "true" : "false");
    row.classList.toggle("selected", on);
    const check = row.querySelector(".check");
    if (check) check.hidden = !on;
  });
}

function paletteRow(p) {
  const dots = p.dots.map((c) =>
    `<span class="swatch" style="background:${c}"></span>`).join("");
  return `<button type="button" class="palette-row" role="radio" data-palette-row="${p.id}">
    <span class="swatches">${dots}</span><span class="palette-name">${p.name}</span>
    <span class="check" aria-hidden="true">✓</span></button>`;
}

$("theme-palette-list").innerHTML = THEME_PALETTES.map(paletteRow).join("");
$("settings-palette-list").innerHTML = THEME_PALETTES.map(paletteRow).join("");
document.querySelectorAll("[data-tmode]").forEach((b) =>
  b.addEventListener("click", () => { theme.mode = b.getAttribute("data-tmode"); applyTheme(); }));
document.querySelectorAll("[data-palette-row]").forEach((row) =>
  row.addEventListener("click", () => {
    theme.palette = row.getAttribute("data-palette-row");
    applyTheme();
  }));
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (theme.mode === "system") applyTheme();
});
applyTheme();

const themeMenu = $("theme-menu");
$("theme-btn").addEventListener("click", () => {
  const open = themeMenu.hidden;
  themeMenu.hidden = !open;
  $("theme-btn").setAttribute("aria-expanded", open ? "true" : "false");
});
document.addEventListener("click", (e) => {
  if (!themeMenu.hidden && !e.target.closest(".theme-anchor")) {
    themeMenu.hidden = true;
    $("theme-btn").setAttribute("aria-expanded", "false");
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !themeMenu.hidden) {
    themeMenu.hidden = true;
    $("theme-btn").setAttribute("aria-expanded", "false");
  }
});

wireSpecList("profile-specs", "profile");
wireSpecList("burst-specs", "burst");
wirePresets("normal-presets", "normal-custom", "normalHours");
wirePresets("grant-presets", "grant-custom", "grantHours");
poll();
setInterval(poll, 15000);
setInterval(() => {
  document.querySelectorAll("[data-countdown]").forEach((el) => {
    el.textContent = countdownText(el.getAttribute("data-countdown"));
  });
}, 1000);
