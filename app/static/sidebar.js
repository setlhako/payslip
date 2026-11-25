/* Sidebar behaviour: collapsed / expanded (remembered in this browser), the "/" shortcut for the search box, and the
   off-canvas drawer on small screens (focus trap, Escape to close, focus returned to the menu button).
   Loaded in the <head> WITHOUT defer so the remembered choice is applied before the first paint (no flash).
   Self-hosted, no inline code (strict CSP), makes no network requests. localStorage is wrapped in try/catch (it may be blocked). */
(function () {
  "use strict";

  var KEY = "payslip-sidebar";
  var root = document.documentElement;
  var narrow = window.matchMedia ? window.matchMedia("(max-width: 900px)") : { matches: false };

  function saved() {
    try {
      return window.localStorage.getItem(KEY) === "collapsed" ? "collapsed" : "expanded";
    } catch (error) {
      return "expanded";
    }
  }

  function setState(state, remember) {
    root.setAttribute("data-sidebar", state);
    document.querySelectorAll("[data-sidebar-toggle]").forEach(function (button) {
      button.setAttribute("aria-expanded", state === "expanded" ? "true" : "false");
      var label = state === "expanded" ? "Collapse sidebar" : "Expand sidebar";
      button.setAttribute("aria-label", label);
      button.setAttribute("title", label);
    });
    if (remember) {
      try { window.localStorage.setItem(KEY, state); } catch (error) { /* the choice just is not remembered */ }
    }
  }

  root.setAttribute("data-sidebar", saved());

  document.addEventListener("DOMContentLoaded", function () {
    var sidebar = document.getElementById("sidebar");
    if (!sidebar) { return; }
    var content = document.querySelector(".content");
    var opener = document.querySelector("[data-drawer-open]");
    var backdrop = document.querySelector(".drawer-backdrop");
    var searchInput = document.getElementById("global-search");
    var peeked = false;
    var lastFocus = null;

    setState(root.getAttribute("data-sidebar") || "expanded", false);

    /* ---- collapse / expand (wide screens) ---- */
    document.addEventListener("click", function (event) {
      var toggle = event.target.closest("[data-sidebar-toggle]");
      if (!toggle) { return; }
      peeked = false;
      setState(root.getAttribute("data-sidebar") === "collapsed" ? "expanded" : "collapsed", true);
    });

    /* ---- drawer (small screens) ---- */
    function isOpen() { return root.getAttribute("data-drawer") === "open"; }

    function focusables() {
      return [].slice.call(sidebar.querySelectorAll("a[href], button:not([disabled]), input:not([type=hidden]):not([disabled]), summary"))
        .filter(function (el) { return el.offsetParent !== null; });
    }

    function openDrawer() {
      if (isOpen()) { return; }
      lastFocus = document.activeElement;
      root.setAttribute("data-drawer", "open");
      sidebar.setAttribute("role", "dialog");
      sidebar.setAttribute("aria-modal", "true");
      sidebar.setAttribute("aria-label", "Main menu");
      if (backdrop) { backdrop.hidden = false; }
      if (content) { content.setAttribute("inert", ""); }
      if (opener) { opener.setAttribute("aria-expanded", "true"); }
      var first = sidebar.querySelector(".side-close") || focusables()[0];
      if (first) { first.focus(); }
    }

    function closeDrawer(returnFocus) {
      if (!isOpen()) { return; }
      root.removeAttribute("data-drawer");
      sidebar.removeAttribute("role");
      sidebar.removeAttribute("aria-modal");
      sidebar.setAttribute("aria-label", "Sidebar");
      if (backdrop) { backdrop.hidden = true; }
      if (content) { content.removeAttribute("inert"); }
      if (opener) { opener.setAttribute("aria-expanded", "false"); }
      if (returnFocus !== false) {
        var target = opener && opener.offsetParent !== null ? opener : lastFocus;
        if (target && target.focus) { target.focus(); }
      }
    }

    document.addEventListener("click", function (event) {
      if (event.target.closest("[data-drawer-open]")) { openDrawer(); return; }
      if (event.target.closest("[data-drawer-close]")) { closeDrawer(); }
    });

    document.addEventListener("keydown", function (event) {
      if (!isOpen()) { return; }
      if (event.key === "Escape") { event.preventDefault(); closeDrawer(); return; }
      if (event.key !== "Tab") { return; }
      var items = focusables();
      if (!items.length) { return; }
      var first = items[0];
      var last = items[items.length - 1];
      if (event.shiftKey && (document.activeElement === first || !sidebar.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !sidebar.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
      }
    });

    function leaveDrawerWhenWide() { if (!narrow.matches) { closeDrawer(false); } }
    if (narrow.addEventListener) { narrow.addEventListener("change", leaveDrawerWhenWide); }
    else if (narrow.addListener) { narrow.addListener(leaveDrawerWhenWide); }

    /* ---- search: press "/" to focus it (the page's own search box when there is one) ---- */
    function editable(target) {
      if (!target || !target.tagName) { return false; }
      var tag = target.tagName.toLowerCase();
      return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
    }

    function focusSearch() {
      var own = document.querySelector("[data-page-search]");
      if (own && !narrow.matches) { own.focus(); own.select(); return; }
      if (!searchInput) { return; }
      if (narrow.matches) { openDrawer(); }
      else if (root.getAttribute("data-sidebar") === "collapsed") { setState("expanded", false); peeked = true; }
      searchInput.focus();
      searchInput.select();
    }

    document.addEventListener("keydown", function (event) {
      if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey || editable(event.target)) { return; }
      if (document.querySelector("dialog[open]")) { return; }
      event.preventDefault();
      focusSearch();
    });

    var searchButton = document.querySelector("[data-search-open]");
    if (searchButton) {
      searchButton.addEventListener("click", function (event) {
        /* collapsed: the icon opens the search box instead of submitting an empty search */
        if (root.getAttribute("data-sidebar") === "collapsed" && !narrow.matches) { event.preventDefault(); focusSearch(); }
      });
    }
    if (searchInput) {
      searchInput.addEventListener("blur", function () {
        if (peeked) { peeked = false; setState("collapsed", false); }
      });
    }
  });
})();
