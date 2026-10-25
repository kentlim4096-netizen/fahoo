// Isolated-world relay: page (inject.js) -> extension background, plus the worker's activity signals.
//   * who is logged in  - read from the (unverified) claims of the KR883 login token; the token itself
//     is never sent anywhere
//   * heartbeat         - one small "active" ping per 30 s in which the person actually used this tab
//     (mouse / keyboard / scroll / touch). Only THAT something happened is recorded, never what was typed.
//   * page              - which KR883 screen is open
(function () {
  var HB_MS = 30000;
  var lastInput = 0, lastPath = null, worker = "";

  function whoami() {
    try {
      var t = localStorage.getItem("kw388_access");
      if (!t) return worker;
      var c = JSON.parse(atob(t.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
      var w = c.username || c.user_name || c.name || c.email || c.preferred_username || c.user_id || c.sub;
      if (w) worker = String(w);
    } catch (e) {}
    return worker;
  }
  function send(ev) {
    ev.worker = whoami();
    ev.host = location.hostname;
    try { chrome.runtime.sendMessage({ type: "kr883-activity", event: ev }); } catch (err) {}
  }

  window.addEventListener("message", function (e) {
    if (e.source !== window || !e.data || e.data.source !== "kr883-activity") return;
    send(e.data.event);
  });

  ["mousemove", "mousedown", "keydown", "scroll", "touchstart", "wheel"].forEach(function (n) {
    window.addEventListener(n, function () { lastInput = Date.now(); }, { passive: true, capture: true });
  });

  setInterval(function () {                       // page / screen changes (single-page app, so poll)
    var p = location.pathname;
    if (p !== lastPath) {
      lastPath = p;
      send({ type: "page", path: p, title: document.title.slice(0, 80), at: new Date().toISOString() });
    }
  }, 1000);

  setInterval(function () {
    if (document.visibilityState === "visible" && document.hasFocus() && Date.now() - lastInput < HB_MS) {
      send({ type: "heartbeat", path: location.pathname, at: new Date().toISOString() });
    }
  }, HB_MS);

  send({ ping: true, page: location.hostname });
})();
