"use strict";
/* Lease panel. Everything displayed is served by the lease core via
 * /api/state; the only client-side derivations are countdown
 * arithmetic on served timestamps and projected-cost arithmetic over
 * the served rate table. Layout and content follow the approved
 * mockup (ui/mockups/2026-11-01-lease-lifecycle-mockup.html).
 *
 * Split by intent: this file keeps the core state and helpers,
 * the served-contract tables (the logs vocabulary and the
 * state-portion table) and the lease-section renderers (budget,
 * lease cards and pickers, access, grant). logs.js, controls.js,
 * poll.js and theme.js are sibling modules loaded after it in
 * that order; orphans.js loads last. */

let S = null; // merge of the state portions landed so far
const sel = {
  mode: "normal",
  shape: null, // chosen spec id, normal mode
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

/* Shorten pickers (lease cards): open state, the chosen pull-in
 * hours and any absolute-deadline draft live outside the render,
 * the same rule as the extend pickers. The last input touched
 * wins: choosing hours clears the deadline draft, and typing a
 * deadline supersedes the hours. */
const shortOpen = new Set(); // sessions whose shorten picker is open
const shortHours = {}; // session -> hours
const shortDeadline = {}; // session -> datetime-local string

/* In-flight action labels, keyed exactly per picker — the
 * extend key for extend, `shorten:<session>` for shorten. A
 * picker renders its busy label only when its own key is
 * present, so one session's extend or shorten never paints
 * progress onto another session's card. The map lives outside
 * the render for the same reason the picker state does: a poll
 * re-render must neither lose the label nor resurrect the
 * apply button mid-flight. */
const actBusy = {}; // picker key -> busy label

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

/* Adopt and shorten answer a core refusal as 409 with the
 * verdict JSON in the body; api() would reduce that to
 * "HTTP 409" and lose the reason, so their callers post through
 * apiResult: the parsed body returns on 200 and 409 alike, and
 * only a request error (400) or an unreachable core (502)
 * throws, with the server's error text. */
async function apiResult(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (resp.ok || resp.status === 409) return data;
  throw new Error(data.error || `HTTP ${resp.status}`);
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
    await poll(true);
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ---------- specs ---------- */

/* Specs come from the core's state.specs — every grantable shape
 * with its served label and rate (null when the core cannot price
 * it). There is no client-side fallback catalog: the core serves
 * the shapes or the section shows its load state. */
function specsFromState() {
  if (S && Array.isArray(S.specs) && S.specs.length) return S.specs;
  return [];
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
  let chip;
  if (rec.standing) chip = `<span class="chip ok">Standing</span>`;
  else if (entry.action === "active") chip = `<span class="chip ok">Active</span>`;
  else if (entry.action === "recorded-but-dead") chip = `<span class="chip bad">Recorded · dead</span>`;
  else chip = `<span class="chip warn">${esc(entry.action || "unknown")}</span>`;
  // A live-looking record whose work channel the census keeps
  // failing to reach is named beside its state, never instead
  // of it (the velvet shape: active chip, dead channel).
  if (entry.channel && entry.channel.verdict === "suspect") chip += ` <span class="chip warn">Channel suspect</span>`;
  return chip;
}

/* The extend picker: the shared duration chips plus a custom-hours
 * input and an apply button, rendered into a card when its Extend
 * toggle is open. kind is "lease" (key = session) or "grant". */
function extendPickerHTML(kind, key) {
  const hours = extHours[extKey(kind, key)] ?? 8;
  const busy = actBusy[extKey(kind, key)];
  const chips = EXT_DURATIONS.map((h) =>
    `<button type="button" data-xhours="${h}" ${busy ? "disabled" : ""} class="${h === hours ? "selected" : ""}">${h === 24 ? "24h" : fmtDur(h)}</button>`).join("");
  return `<div class="extend-box">
    <div class="chips" data-extpicker="${esc(kind)}:${esc(key)}">
      ${chips}
      <input class="custom" data-xcustom type="number" min="0.25" step="0.25" placeholder="Custom (hours)">
    </div>
    <button type="button" class="primary block" data-xapply="${esc(kind)}:${esc(key)}" ${busy ? "disabled" : ""} style="margin-top:10px">${busy || `Extend by ${esc(fmtDur(hours))}`}</button>
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
      return "the standing lease is grant-managed — extend the grant instead";
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

/* The shorten picker: extend's mirror on the lease cards. The
 * operator picks a pull-in from the shared duration set (chips +
 * custom hours) or an absolute deadline (a datetime-local input,
 * read as local wall-clock and posted as an ISO instant); the
 * apply posts through apiResult and the refusal text reads the
 * verdict's action, as reportExtend does for extend. */
function shortenPickerHTML(session) {
  const hours = shortHours[session] ?? 8;
  const deadline = shortDeadline[session] || "";
  const busy = actBusy[`shorten:${session}`];
  const chips = EXT_DURATIONS.map((h) =>
    `<button type="button" data-shours="${h}" ${busy ? "disabled" : ""} class="${h === hours && !deadline ? "selected" : ""}">${h === 24 ? "24h" : fmtDur(h)}</button>`).join("");
  return `<div class="extend-box shorten-box">
    <div class="chips" data-shortpicker="${esc(session)}">
      ${chips}
      <input class="custom" data-scustom type="number" min="0.25" step="0.25" placeholder="Custom (hours)">
    </div>
    <div class="deadline-row">
      <span class="lab">or end at</span>
      <input class="deadline-input" data-sdeadline type="datetime-local" value="${esc(deadline)}">
    </div>
    <button type="button" class="primary block" data-sapply="${esc(session)}" ${busy ? "disabled" : ""} style="margin-top:10px">${busy || (deadline ? "Shorten to the chosen time" : `Shorten by ${esc(fmtDur(hours))}`)}</button>
  </div>`;
}

function shortenRefusalText(res) {
  switch (res.action) {
    case "shorten-deadline-past":
      return "that target has already passed — Terminate ends the lease now";
    case "shorten-not-earlier":
      return "that is not earlier than the current deadline — Extend moves it later";
    case "shorten-standing-managed":
      return "the standing lease is grant-managed — its deadline follows the grant";
    case "shorten-unknown":
      return "session state unreadable — nothing changed";
    case "shorten-no-lease":
      return "no live lease to shorten";
    case "lease-disabled":
      return "the lease is disabled in config";
    default:
      return res.action || "failed";
  }
}

function reportShorten(res, session) {
  if (res.action === "shortened") {
    shortOpen.delete(session);
    delete shortDeadline[session];
    notice(`Shortened — new deadline ${fmtTime(res.new_deadline)}`);
  } else {
    notice(`Shorten refused: ${shortenRefusalText(res)}`);
  }
}

function wireShortenPickers(container) {
  container.querySelectorAll("[data-shortpicker]").forEach((row) => {
    const session = row.getAttribute("data-shortpicker");
    row.querySelectorAll("button[data-shours]").forEach((btn) => {
      btn.addEventListener("click", () => {
        shortHours[session] = parseFloat(btn.getAttribute("data-shours"));
        delete shortDeadline[session];
        renderLeases();
      });
    });
    const custom = row.querySelector("[data-scustom]");
    if (custom) {
      custom.addEventListener("input", (e) => {
        const v = parseFloat(e.target.value);
        if (v > 0) {
          shortHours[session] = v;
          delete shortDeadline[session];
          row.querySelectorAll("button").forEach((b) => b.classList.remove("selected"));
          const box = row.closest(".shorten-box");
          const apply = box && box.querySelector("[data-sapply]");
          if (apply) apply.textContent = `Shorten by ${fmtDur(v)}`;
          const dl = box && box.querySelector("[data-sdeadline]");
          if (dl) dl.value = "";
        }
      });
    }
  });
  container.querySelectorAll("[data-sdeadline]").forEach((input) => {
    input.addEventListener("input", (e) => {
      const box = e.target.closest(".shorten-box");
      const apply = box && box.querySelector("[data-sapply]");
      if (!apply) return;
      const session = apply.getAttribute("data-sapply");
      if (e.target.value) {
        shortDeadline[session] = e.target.value;
        box.querySelectorAll("button[data-shours]").forEach((b) => b.classList.remove("selected"));
        apply.textContent = "Shorten to the chosen time";
      } else {
        delete shortDeadline[session];
        apply.textContent = `Shorten by ${fmtDur(shortHours[session] ?? 8)}`;
      }
    });
  });
  container.querySelectorAll("[data-sapply]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const session = btn.getAttribute("data-sapply");
      const body = { session };
      const dl = shortDeadline[session];
      if (dl) {
        const when = new Date(dl);
        if (Number.isNaN(when.getTime())) {
          notice("Shorten refused: the chosen time is not a valid time");
          return;
        }
        body.deadline = when.toISOString();
      } else {
        body.hours = shortHours[session] ?? 8;
      }
      act(btn, async () => {
        const k = `shorten:${session}`;
        actBusy[k] = "Shortening…";
        renderLeases();
        try {
          reportShorten(await apiResult("/api/lease/shorten", body), session);
        } finally {
          delete actBusy[k];
        }
      });
    });
  });
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
      act(btn, async () => {
        const k = extKey(kind, key);
        actBusy[k] = "Extending…";
        if (kind === "grant") renderGrant(); else renderLeases();
        try {
          if (kind === "grant") {
            reportExtend(await api("/api/grant/extend", { hours }), "grant", "");
          } else {
            reportExtend(await api("/api/lease/extend", { session: key, hours }), "lease", key);
          }
        } finally {
          delete actBusy[k];
        }
      });
    });
  });
}

function listenerLines(session) {
  // The listeners portion is keyed by session and served ready
  // (suspect derivation included); the card names each listener
  // and flags suspects — honest absence (no census yet, or a
  // census with no listeners) renders no line at all.
  const census = (S && S.listeners && S.listeners[session]) || null;
  if (!census) return "";
  if (census.error) return `<p class="kvline">Listeners <b>census failed</b></p>`;
  return (census.listeners || []).map((l) => `<p class="kvline">Port <b>${esc(String(l.port))}</b> · pid ${esc(String(l.pid))} · <span class="mono">${esc(l.cwd || "—")}</span>${(l.suspect || []).length ? ` <span class="chip warn">suspect: ${esc(l.suspect.join(", "))}</span>` : ""}</p>`).join("");
}

function renderLeases() {
  const records = (S && S.records) || [];
  const box = $("lease-cards");
  box.innerHTML = records.map((entry) => {
    const rec = entry.record || {};
    const session = entry.session || rec.session || "";
    const spec = specById(rec.shape);
    const title = spec ? specTitle(spec) : session || "lease";
    const age = entry.keepalive_heartbeat_age_s;
    const pulse = rec.last_pulse_result;
    const burn = spec ? specRateText(spec) : "—";
    return `<div class="card">
      <div class="row"><b>${esc(title)}</b>${leaseChip(entry)}</div>
      <p class="big num" data-countdown="${esc(rec.deadline || "")}">${esc(countdownText(rec.deadline))}</p>
      <p class="kvline">Deadline <b>${esc(fmtTime(rec.deadline))}</b> · session <span class="mono">${esc(rec.session || "—")}</span>${rec.caller ? ` · Caller <b>${esc(rec.caller)}</b>` : ""}</p>
      <p class="kvline">Heartbeat <b>${age == null ? "unknown" : `${Math.round(age)}s ago`}</b> · Pulse <b>${esc(pulse || "—")}</b> · Burn <b>${esc(burn)}</b></p>
      ${entry.sync_claim ? `<p class="kvline">Sync claim <b>${esc(entry.sync_claim.caller || "—")}</b> · until <b>${esc(fmtTime(entry.sync_claim.expires_at))}</b></p>` : ""}
      ${listenerLines(session)}
      <button type="button" class="danger" data-stop="${esc(session)}">Terminate</button>
      <button type="button" class="ghost" data-xtoggle="${esc(session)}">Extend</button>
      <button type="button" class="ghost" data-stoggle="${esc(session)}">Shorten</button>
      ${extOpen.has(extKey("lease", session)) ? extendPickerHTML("lease", session) : ""}
      ${shortOpen.has(session) ? shortenPickerHTML(session) : ""}
    </div>`;
  }).join("");
  box.querySelectorAll("[data-stop]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const session = btn.getAttribute("data-stop");
      if (!window.confirm(`Terminate the ${session} lease now?`)) return;
      act(btn, () => api("/api/lease/stop", { session }));
    });
  });
  box.querySelectorAll("[data-xtoggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const key = extKey("lease", btn.getAttribute("data-xtoggle"));
      if (extOpen.has(key)) extOpen.delete(key); else extOpen.add(key);
      renderLeases();
    });
  });
  box.querySelectorAll("[data-stoggle]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const session = btn.getAttribute("data-stoggle");
      if (shortOpen.has(session)) shortOpen.delete(session); else shortOpen.add(session);
      renderLeases();
    });
  });
  wireExtendPickers(box);
  wireShortenPickers(box);
}

function renderAccess() {
  // The access list is served ready by the core (the records
  // portion): every live session with its caller, or flagged
  // unattributed. Unattributed entries carry the one-tap release;
  // attributed ones render without it — their lifecycle belongs
  // to their lease record or their owning thread.
  const box = $("access-list");
  if (!box) return;
  const access = (S && S.access) || [];
  if (!access.length) {
    box.innerHTML = "";
    return;
  }
  box.innerHTML = `<div class="card">
    <div class="row"><b>Session access</b></div>
    ${access.map((e) => `<p class="kvline"><span class="mono">${esc(e.session || "—")}</span> · ${esc(e.shape_class || "shape unknown")} · ${e.caller ? `Caller <b>${esc(e.caller)}</b>` : `<span class="chip warn">Unattributed</span>`} · since <b>${esc(fmtTime(e.first_seen))}</b>${e.unattributed ? ` <button type="button" class="danger" data-access-release="${esc(e.session || "")}">Release</button>` : ""}</p>`).join("")}
  </div>`;
  box.querySelectorAll("[data-access-release]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const session = btn.getAttribute("data-access-release");
      if (!window.confirm(`Release the unattributed session ${session} now?`)) return;
      act(btn, () => api("/api/access/release", { session }));
    });
  });
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
    ? envLeases.map((rec) => `<p class="kvline">· <b>${esc(specLabel(rec.shape))}</b>${rec.caller ? ` · caller <b>${esc(rec.caller)}</b>` : ""} · until <b>${esc(fmtTime(rec.deadline))}</b></p>`).join("")
    : `<p class="kvline">No envelope leases yet.</p>`;
  box.innerHTML = `<div class="card">
    <div class="row"><b>Two-tier grant</b><span class="chip ok">Active</span></div>
    <p class="kvline" style="margin-top:8px">Until <b>${esc(fmtTime(grant.until))}</b> · Burst spec <b>${esc(specLabel((grant.scope || {}).burst_shape))}</b></p>
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

/* ---------- served-contract tables ----------
 * The logs vocabulary (consumed by logs.js) and the
 * state-portion table below it (consumed by poll.js) stay in
 * this file: they are the two places the panel encodes the
 * core's served contracts. */

/* The logs vocabulary is closed: exactly the contract's eight
 * kinds (owner's ruling, 2026-11-03). The outbox records other
 * kinds server-side; they are not part of this surface and do
 * not render here. */
const LOG_LEVEL_BY_KIND = {
  "lease-started": "ok",
  "lease-adopted": "ok",
  "grant-started": "ok",
  "lease-extended": "info",
  "envelope-lease-released": "info",
  "ui-restored": "info",
  "keepalive-stale": "warn",
  "budget-refused": "error",
};

const LOG_KINDS = new Set(Object.keys(LOG_LEVEL_BY_KIND));

/* The state portions and their poll cadences; poll.js runs
 * the loading machinery over this table. */
const PARTS = {
  records: { url: "/api/state/records", cadence: 15000 },
  specs: { url: "/api/state/specs", cadence: 60000 },
  budget: { url: "/api/state/budget", cadence: 30000 },
  grant: { url: "/api/state/grant", cadence: 60000 },
  orphans: { url: "/api/state/orphans", cadence: 15000 },
  listeners: { url: "/api/state/listeners", cadence: 60000 },
  logs: { url: "/api/outbox?after=0", cadence: 15000 },
};
