/* Live progress for a batch that is being sent. Polls a small JSON endpoint every 2 seconds and updates the numbers,
   status badges and messages by changing text only. The URL comes from the page (data-status-url); nothing is hard-coded.
   Without scripts the page still works (it refreshes itself every few seconds). Self-hosted: no inline code (strict CSP). */
(function () {
  "use strict";

  var table = document.querySelector("[data-status-url]");
  if (!table) { return; }
  var url = table.getAttribute("data-status-url");
  var note = document.querySelector("[data-live-note]");
  var reloading = false;
  var wasActive = table.getAttribute("data-active") === "1";
  var ribbon = document.querySelector("[data-ribbon]");
  var order = ["pending", "queued", "sending", "sent", "delivered", "read", "failed", "uncertain"];

  function needsAction(status) { return status === "failed" || status === "uncertain"; }

  function updateRibbon(counts) {
    if (!ribbon) { return; }
    var total = counts.total || 0;
    var x = 0;
    order.forEach(function (key) {
      var seg = ribbon.querySelector('[data-seg="' + key + '"]');
      var width = total ? (counts[key] || 0) * 100 / total : 0;
      if (seg) { seg.setAttribute("x", String(x)); seg.setAttribute("width", String(width)); }
      x += width;
    });
    var done = (counts.delivered || 0) + (counts.read || 0);
    ribbon.setAttribute("aria-label", done + " of " + total + " delivered or read");
  }

  function apply(data) {
    document.querySelectorAll("[data-count]").forEach(function (el) {
      var value = data.counts[el.getAttribute("data-count")];
      el.textContent = String(value || 0);
    });
    updateRibbon(data.counts);
    var reload = data.batch.send_state !== table.getAttribute("data-send-state");
    data.rows.forEach(function (row) {
      var tr = table.querySelector('[data-row-id="' + row.id + '"]');
      if (!tr) { return; }
      var before = tr.getAttribute("data-status");
      if (needsAction(before) !== needsAction(row.status) || (before === "uncertain") !== (row.status === "uncertain")) { reload = true; }
      tr.setAttribute("data-status", row.status);
      var badge = tr.querySelector("[data-badge]");
      if (badge) { badge.className = "badge badge-" + row.status; }
      var label = tr.querySelector("[data-label]");
      if (label) { label.textContent = row.label; }
      var detail = tr.querySelector("[data-detail]");
      if (detail) { detail.textContent = row.detail; }
    });
    if (wasActive && !data.batch.active) { reload = true; }          // finished: reload once to show the summary and what to do next
    if (reload && !reloading) { reloading = true; window.location.reload(); return false; }
    if (note) { note.textContent = data.batch.active ? "Updating..." : "Finished."; }
    return data.batch.active;
  }

  function poll() {
    window.fetch(url, { credentials: "same-origin", cache: "no-store", headers: { "Accept": "application/json" } })
      .then(function (response) {
        if (!response.ok) { throw new Error("request failed"); }
        return response.json();
      })
      .then(function (data) {
        if (apply(data)) { window.setTimeout(poll, 2000); }
      })
      .catch(function () {
        if (note) { note.textContent = "Could not update. Trying again..."; }
        window.setTimeout(poll, 4000);
      });
  }

  poll();
})();
