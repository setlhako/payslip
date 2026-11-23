/* Small enhancements. The site works without this file; it only adds convenience.
   Self-hosted and free of inline code, so the strict Content-Security-Policy stays in place.
   The only network request it makes is "Show IDs": a CSRF-protected POST to the reveal address that the server puts in the page. Full ID numbers
   are never in the page HTML; they are fetched on demand, put into the page text, and removed again by Hide IDs,
   after 60 seconds, when the tab is hidden, and when the page is restored by the back button. */
(function () {
  "use strict";

  var root = document.documentElement;
  root.classList.add("js");

  var MASK = "\u2022\u2022\u2022\u2022";
  var AUTO_HIDE_MS = 60000;
  var hideTimer = null;

  /* ---- Show IDs / Hide IDs ---- */
  function setIdsVisible(show) {
    document.querySelectorAll("[data-secret]").forEach(function (el) {
      el.textContent = show ? el.getAttribute("data-secret") : MASK;
      el.classList.toggle("is-revealed", show);
    });
    if (!show) { clearFullIds(); }
    document.querySelectorAll("[data-toggle-ids]").forEach(function (button) {
      button.setAttribute("aria-pressed", show ? "true" : "false");
      var label = button.querySelector("[data-toggle-label]");
      if (label) {
        label.textContent = show ? (button.getAttribute("data-hide-label") || "Hide IDs")
                                 : (button.getAttribute("data-show-label") || "Show IDs");
      }
      var use = button.querySelector("use");
      if (use) { use.setAttribute("href", "/static/icons.svg#i-" + (show ? "eye-off" : "eye")); }
    });
    window.clearTimeout(hideTimer);
    if (show) { hideTimer = window.setTimeout(function () { setIdsVisible(false); }, AUTO_HIDE_MS); }
  }

  /* Full ID numbers: put into the page only after the server has sent them; taken out again when hidden. */
  function fillFullIds(ids) {
    document.querySelectorAll("[data-id-slot]").forEach(function (el) {
      var value = ids[el.getAttribute("data-id-slot")];
      if (typeof value === "string") {
        if (!el.hasAttribute("data-mask")) { el.setAttribute("data-mask", el.textContent); }
        el.textContent = value;
        el.classList.add("is-revealed");
      }
    });
  }

  function clearFullIds() {
    document.querySelectorAll("[data-id-slot]").forEach(function (el) {
      if (el.hasAttribute("data-mask")) { el.textContent = el.getAttribute("data-mask"); }
      el.classList.remove("is-revealed");
    });
  }

  function showRevealError(message) {
    var box = document.querySelector("[data-reveal-error]");
    var text = document.querySelector("[data-reveal-error-text]");
    if (box && text) { text.textContent = message; box.hidden = false; }
  }

  function showIds(button) {
    var form = document.querySelector("form[data-reveal-form]");
    var errorBox = document.querySelector("[data-reveal-error]");
    if (errorBox) { errorBox.hidden = true; }
    if (!form || !document.querySelector("[data-id-slot]") || !window.fetch) { setIdsVisible(true); return; }
    button.disabled = true;
    window.fetch(form.getAttribute("action"), {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(new FormData(form)).toString()
    }).then(function (response) {
      if (!response.ok) { var error = new Error("request failed"); error.status = response.status; throw error; }
      return response.json();
    }).then(function (data) {
      fillFullIds((data && data.ids) || {});
      setIdsVisible(true);
    }).catch(function (error) {
      showRevealError(error && error.status === 429
        ? "Too many requests. Please wait a minute and try again."
        : "The ID numbers could not be shown. Reload the page and try again.");
    }).then(function () { button.disabled = false; });
  }

  /* ---- menus (<details class="menu">): only one open, closes on outside click or Escape ---- */
  function closeMenus(except) {
    document.querySelectorAll("details.menu[open]").forEach(function (menu) {
      if (menu !== except) { menu.removeAttribute("open"); }
    });
  }

  document.addEventListener("click", function (event) {
    var toggle = event.target.closest("[data-toggle-ids]");
    if (toggle) {
      if (toggle.getAttribute("aria-pressed") === "true") { setIdsVisible(false); } else { showIds(toggle); }
      return;
    }
    var dismiss = event.target.closest("[data-dismiss]");
    if (dismiss) {
      var message = dismiss.closest(".flash");
      if (message) { message.remove(); }
      return;
    }
    closeMenus(event.target.closest("details.menu"));
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      var open = document.querySelector("details.menu[open]");
      if (open) {
        open.removeAttribute("open");
        var summary = open.querySelector("summary");
        if (summary) { summary.focus(); }
      }
    }
  });

  /* ---- selects that apply immediately (the form also has a normal button for when scripts are off) ---- */
  document.addEventListener("change", function (event) {
    var select = event.target.closest("[data-autosubmit]");
    if (select && select.form) { select.form.submit(); }
  });

  /* ---- stop double clicks on save buttons ---- */
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (form.hasAttribute && form.hasAttribute("data-once")) {
      window.setTimeout(function () {
        form.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = true; b.classList.add("is-loading"); b.setAttribute("aria-busy", "true"); });
      }, 0);
    }
  });

  var reduceMotion = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  /* ---- toasts: success and info messages leave by themselves (not while hovered or focused); errors stay until dismissed ---- */
  function leave(toast) {
    toast.classList.add("is-leaving");
    window.setTimeout(function () { toast.remove(); }, reduceMotion ? 0 : 220);
  }
  document.querySelectorAll(".toasts .flash").forEach(function (toast) {
    if (toast.classList.contains("flash-error")) { return; }
    var timer = null;
    function start() { window.clearTimeout(timer); timer = window.setTimeout(function () { leave(toast); }, 7000); }
    function pause() { window.clearTimeout(timer); }
    toast.addEventListener("mouseenter", pause);
    toast.addEventListener("mouseleave", start);
    toast.addEventListener("focusin", pause);
    toast.addEventListener("focusout", start);
    start();
  });

  /* ---- "Are you sure?" dialog for risky buttons (data-confirm="..."); without scripts the button just submits ---- */
  var dialog = document.getElementById("confirm-dialog");
  var pending = null;
  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-confirm]");
    if (!trigger || !dialog || typeof dialog.showModal !== "function") { return; }
    event.preventDefault();
    event.stopPropagation();
    pending = trigger;
    dialog.returnValue = "";
    dialog.querySelector("[data-confirm-title]").textContent = trigger.getAttribute("data-confirm-title") || "Are you sure?";
    dialog.querySelector("[data-confirm-text]").textContent = trigger.getAttribute("data-confirm");
    dialog.querySelector("[data-confirm-ok]").textContent = trigger.getAttribute("data-confirm-label") || "Confirm";
    closeMenus(null);
    dialog.showModal();
  }, true);
  if (dialog) {
    dialog.addEventListener("close", function () {
      var trigger = pending;
      pending = null;
      if (dialog.returnValue === "ok" && trigger && trigger.form) { trigger.form.requestSubmit(trigger); }
      else if (trigger) { trigger.focus(); }
    });
  }

  /* ---- menus: arrow keys move between items ---- */
  document.addEventListener("keydown", function (event) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") { return; }
    var menu = event.target.closest && event.target.closest("details.menu");
    if (!menu) { return; }
    var items = [].slice.call(menu.querySelectorAll(".menu-item"));
    if (!items.length) { return; }
    if (!menu.open) { menu.setAttribute("open", ""); }
    var index = items.indexOf(document.activeElement);
    var next = event.key === "ArrowDown" ? (index + 1) % items.length : (index <= 0 ? items.length - 1 : index - 1);
    event.preventDefault();
    items[next].focus();
  });

  /* ---- copy button (data-copy-target="#id"): copies that element's text ---- */
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy-target]");
    if (!button) { return; }
    var source = document.querySelector(button.getAttribute("data-copy-target"));
    if (!source) { return; }
    var text = source.textContent.trim();
    var done = function () {
      var label = button.querySelector("[data-copy-label]");
      if (label) { label.textContent = "Copied"; window.setTimeout(function () { label.textContent = "Copy"; }, 2500); }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { selectText(source); });
    } else {
      selectText(source);
    }
  });
  function selectText(element) {
    var range = document.createRange();
    range.selectNodeContents(element);
    var selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  /* ---- safety: hide IDs when the tab is hidden or restored from the back button ---- */
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) { setIdsVisible(false); }
  });
  window.addEventListener("pageshow", function (event) {
    if (event.persisted) {
      setIdsVisible(false);
      document.querySelectorAll("form[data-once] button[type=submit]").forEach(function (b) { b.disabled = false; b.classList.remove("is-loading"); b.removeAttribute("aria-busy"); });
    }
  });
})();
