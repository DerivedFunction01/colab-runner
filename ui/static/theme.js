"use strict";
/* Theme (below) plus the page bootstrap: this module loads
 * last among the app.js split (orphans.js follows it), so the
 * control wiring, first poll and tickers at its tail run with
 * every module defined. */

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

wireSpecList("shape-specs", "shape");
wireSpecList("burst-specs", "burst");
wirePresets("normal-presets", "normal-custom", "normalHours");
wirePresets("grant-presets", "grant-custom", "grantHours");
poll(true);
setInterval(() => poll(false), 5000);
setInterval(() => {
  renderPartStatuses();
  document.querySelectorAll("[data-countdown]").forEach((el) => {
    el.textContent = countdownText(el.getAttribute("data-countdown"));
  });
  document.querySelectorAll("[data-age]").forEach((el) => {
    el.textContent = fmtAge(el.getAttribute("data-age"));
  });
}, 1000);
