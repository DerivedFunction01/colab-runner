"use strict";
/* Progressive loading: the section render dispatch and the
 * portion poll machinery. Loads after app.js and logs.js —
 * consumes the PARTS table from app.js (the portion URLs are
 * the served contract), merges landed portions into S, and
 * feeds logs.js its outbox portion. theme.js starts the first
 * poll at bootstrap. */

function renderSections(names) {
  // Sections render the moment their own portion lands; renders
  // are idempotent reads of the merged S, so a cross-dependent
  // section (controls read records + specs, leases read specs for
  // labels) simply re-renders when the other portion arrives.
  if (names.includes("budget")) renderBudget();
  if (names.includes("records") || names.includes("specs") || names.includes("listeners")) {
    renderLeases();
    renderAccess();
    renderControls();
  }
  if (names.includes("grant")) renderGrant();
  // renderOrphans lives in orphans.js (loaded after this file);
  // the specs portion landing re-renders it because the orphan
  // cards label shapes and candidates from the served catalog.
  if ((names.includes("orphans") || names.includes("specs")) && typeof renderOrphans === "function") {
    renderOrphans();
  }
}

/* ---------- progressive loading ----------
 * Each portion of the state read loads, fails and retries on its
 * own cadence; S is the merge of the portions landed so far, and a
 * section renders the moment its portion lands — no section waits
 * on another portion's read. A failed refresh after a successful
 * load keeps the section's data with an age label (labeled
 * staleness, never stale-as-live); a portion that has never loaded
 * shows its own error with a retry. The degraded card is only the
 * everything-down state, and it no longer hides the layout. */
const partState = {};
for (const name of Object.keys(PARTS)) {
  partState[name] = { data: null, loadedAt: 0, failed: false, due: 0 };
}

function mergeParts() {
  const merged = {};
  for (const name of ["records", "specs", "budget", "grant", "orphans", "listeners"]) {
    if (partState[name].data) Object.assign(merged, partState[name].data);
  }
  S = merged;
}

function renderPartStatuses() {
  let anyLoaded = false;
  let allFailed = true;
  for (const name of Object.keys(PARTS)) {
    const st = partState[name];
    if (st.loadedAt) { anyLoaded = true; allFailed = false; }
    if (!st.failed) allFailed = false;
    const el = document.querySelector(`[data-part-status="${name}"]`);
    if (!el) continue;
    let html = "";
    let stamp = "";
    if (!st.loadedAt && st.failed) {
      stamp = "error";
      html = `Couldn’t load. <button type="button" class="part-retry" data-part-retry="${name}">Retry</button>`;
    } else if (st.loadedAt && st.failed) {
      stamp = `stale-${Math.floor((Date.now() - st.loadedAt) / 60000)}`;
      html = `Update failed — last updated ${fmtAge(new Date(st.loadedAt).toISOString())}`;
    } else if (!st.loadedAt) {
      stamp = "loading";
      html = "Loading…";
    }
    if (el.getAttribute("data-stamp") !== stamp) {
      el.setAttribute("data-stamp", stamp);
      el.innerHTML = html;
      el.classList.toggle("err", stamp === "error" || stamp.startsWith("stale"));
    }
  }
  $("degraded").hidden = !(allFailed && !anyLoaded);
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-part-retry]");
  if (btn) fetchPart(btn.getAttribute("data-part-retry"));
});

async function fetchPart(name) {
  const st = partState[name];
  try {
    const data = await api(PARTS[name].url);
    st.data = data;
    st.loadedAt = Date.now();
    st.failed = false;
    if (name === "logs") {
      logsEvents = data.events || [];
      renderLogs(logsEvents);
    } else {
      mergeParts();
      renderSections([name]);
    }
    renderPartStatuses();
    return true;
  } catch (err) {
    st.failed = true;
    renderPartStatuses();
    return false;
  }
}

async function poll(force) {
  const now = Date.now();
  const due = Object.keys(PARTS).filter((name) => force || now >= partState[name].due);
  const results = await Promise.all(due.map(async (name) => {
    const ok = await fetchPart(name);
    partState[name].due = Date.now() + PARTS[name].cadence;
    return ok;
  }));
  return results.some(Boolean);
}

$("retry-btn").addEventListener("click", (e) => act(e.currentTarget, () => poll(true)));

async function restartServer(btn) {
  btn.disabled = true;
  try {
    // The restart stops the very server answering this, so a failed
    // POST is not fatal: what matters is the server coming back.
    await api("/api/server/restart", {}).catch(() => {});
    for (let i = 0; i < 20; i++) {
      await sleep(1500);
      if (await poll(true)) break;
    }
  } finally {
    btn.disabled = false;
  }
}

$("restart-btn").addEventListener("click", (e) => restartServer(e.currentTarget));
$("settings-restart-btn").addEventListener("click", (e) => restartServer(e.currentTarget));
