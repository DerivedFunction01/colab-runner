"use strict";
/* Start controls: the spec pickers and projected cost for the
 * normal and two-tier modes, and their wiring. Loads after
 * app.js — uses sel, the specs helpers and renderGrant from
 * app.js; theme.js calls the wire* functions at bootstrap. */

function renderSpecList(containerId, key) {
  const specs = specsFromState();
  const box = $(containerId);
  if (!sel[key] || !specs.some((s) => s.id === sel[key])) {
    sel[key] = (specs[0] && specs[0].id) || null;
  }
  box.innerHTML = specs.map((s) => `
    <button type="button" class="spec${s.id === sel[key] ? " selected" : ""}" data-spec="${esc(s.id)}">
      <span><b>${esc(s.label || s.id)}</b> · ${esc(specDescriptor(s))}</span>
      <span class="cost num">${esc(specRateText(s))}</span>
    </button>`).join("");
}

function renderControls() {
  renderSpecList("shape-specs", "shape");
  renderSpecList("burst-specs", "burst");
  const chosen = specById(sel.shape);
  const rate = chosen ? chosen.rate_cu_per_hour : null;
  $("normal-projected").textContent =
    rate == null ? "Cost unconfirmed" : `${fmtNum(rate * sel.normalHours)} CU`;
  renderGrant();
}

/* ---------- controls wiring ---------- */

function wireSpecList(containerId, key) {
  $(containerId).addEventListener("click", (e) => {
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
  act(e.currentTarget, () => api("/api/lease/start", { shape: sel.shape, hours: sel.normalHours }));
});
$("grant-btn").addEventListener("click", (e) => {
  act(e.currentTarget, () => api("/api/grant", { hours: sel.grantHours, burst: sel.burst }));
});
