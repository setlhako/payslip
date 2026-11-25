/* Friendly inactivity warning. The server ends a session after a period without requests; one minute before that, this
   script opens a small dialog ("You will be signed out in 1 minute. Stay signed in?"). It only ever asks the server
   how much time is left (GET /session/status, which does not count as activity) and, when the person presses
   "Stay signed in", sends POST /session/keepalive with the page's CSRF token. It never reads or stores anything personal.
   Self-hosted, no inline code (strict CSP). Without scripts nothing changes: the session still ends on time. */
(function () {
  "use strict";

  var dialog = document.getElementById("idle-dialog");
  if (!dialog || typeof dialog.showModal !== "function" || !window.fetch) { return; }

  var IDLE = parseInt(dialog.getAttribute("data-idle-seconds"), 10) || 1800;
  var WARN = 60;                                   /* seconds before the end at which the dialog opens */
  var deadline = Date.now() + IDLE * 1000;         /* the page that just loaded counted as activity */
  var checkTimer = null;
  var tickTimer = null;
  var left = dialog.querySelector("[data-idle-left]");
  var errorBox = dialog.querySelector("[data-idle-error]");
  var stayButton = dialog.querySelector("[data-idle-stay]");

  function csrf() {
    var input = document.querySelector('form[action="/logout"] input[name="csrf_token"]');
    return input ? input.value : "";
  }

  function toLogin() { window.location.assign("/login"); }

  function secondsLeft() { return Math.max(0, Math.ceil((deadline - Date.now()) / 1000)); }

  function schedule() {
    window.clearTimeout(checkTimer);
    checkTimer = window.setTimeout(check, Math.max(1000, deadline - Date.now() - WARN * 1000));
  }

  function words(seconds) {
    if (seconds >= 60) { return "1 minute"; }
    return seconds + (seconds === 1 ? " second" : " seconds");
  }

  function tick() {
    var s = secondsLeft();
    if (s <= 0) { window.clearInterval(tickTimer); toLogin(); return; }
    if (left) { left.textContent = words(s); }
  }

  function openWarning() {
    if (!dialog.open) {
      if (errorBox) { errorBox.hidden = true; }
      dialog.showModal();
      if (stayButton) { stayButton.focus(); }
    }
    window.clearInterval(tickTimer);
    tick();
    tickTimer = window.setInterval(tick, 1000);
  }

  function closeWarning() {
    window.clearInterval(tickTimer);
    if (dialog.open) { dialog.close(); }
  }

  /* Ask the server how long is left (it may have been extended by another tab or by background polling). */
  function check() {
    window.fetch("/session/status", {
      credentials: "same-origin", cache: "no-store", redirect: "manual", headers: { "Accept": "application/json" }
    }).then(function (response) {
      if (response.type === "opaqueredirect" || response.status === 401 || response.status === 403) { return { gone: true }; }
      if (!response.ok) { throw new Error("status"); }
      return response.json();
    }).then(function (data) {
      if (data.gone || !data || data.remaining <= 0) { toLogin(); return; }
      deadline = Date.now() + data.remaining * 1000;
      if (data.remaining > WARN + 1) { closeWarning(); schedule(); } else { openWarning(); }
    }).catch(function () {
      /* could not ask (offline?): trust the local clock */
      if (secondsLeft() <= WARN) { openWarning(); } else { schedule(); }
    });
  }

  function stay() {
    if (stayButton) { stayButton.disabled = true; }
    window.fetch("/session/keepalive", {
      method: "POST", credentials: "same-origin", cache: "no-store", redirect: "manual",
      headers: { "Accept": "application/json", "X-CSRF-Token": csrf() }
    }).then(function (response) {
      if (response.type === "opaqueredirect" || response.status === 401) { toLogin(); return null; }
      if (!response.ok) { throw new Error("keepalive"); }
      return response.json();
    }).then(function (data) {
      if (!data) { return; }
      deadline = Date.now() + data.remaining * 1000;
      closeWarning();
      schedule();
    }).catch(function () {
      if (errorBox) { errorBox.hidden = false; }
    }).then(function () {
      if (stayButton) { stayButton.disabled = false; }
    });
  }

  if (stayButton) { stayButton.addEventListener("click", stay); }
  /* Escape means "I am here": same as pressing Stay signed in */
  dialog.addEventListener("cancel", function (event) { event.preventDefault(); stay(); });

  /* timers sleep in hidden tabs: look again as soon as the tab is back */
  document.addEventListener("visibilitychange", function () { if (!document.hidden) { check(); } });

  schedule();
})();
