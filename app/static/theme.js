/* Light / dark theme. Loaded in the <head> WITHOUT defer, so the saved choice is applied before the first paint (no flash).
   The choice is remembered in this browser only (localStorage, wrapped in try/catch: it may be blocked or unavailable).
   With no saved choice the system setting decides. Self-hosted, no inline code (strict CSP), makes no network requests. */
(function () {
  "use strict";

  var KEY = "payslip-theme";
  var root = document.documentElement;

  function saved() {
    try {
      var value = window.localStorage.getItem(KEY);
      return value === "dark" || value === "light" ? value : null;
    } catch (error) {
      return null;
    }
  }

  function systemPrefersDark() {
    return !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
  }

  function apply(theme) {
    root.setAttribute("data-theme", theme);
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.setAttribute("aria-pressed", theme === "dark" ? "true" : "false");
      button.setAttribute("aria-label", theme === "dark" ? "Dark theme (on)" : "Dark theme (off)");
    });
  }

  apply(saved() || (systemPrefersDark() ? "dark" : "light"));

  document.addEventListener("DOMContentLoaded", function () {
    apply(root.getAttribute("data-theme") || "light");
    document.addEventListener("click", function (event) {
      var button = event.target.closest("[data-theme-toggle]");
      if (!button) { return; }
      var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
      apply(next);
      try { window.localStorage.setItem(KEY, next); } catch (error) { /* the choice just is not remembered */ }
    });
  });
})();
