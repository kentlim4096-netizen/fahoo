// Runs inside the KR883 page. Watches the API calls behind a customer search - the Member List search
// (/api/members/) and the Credit Report (/api/loans/credit-report/) - and every save/update action
// (POST / PUT / PATCH / DELETE to /api/...), and hands each to relay.js. Read-only: it never changes a
// request or a response. Login / 2FA / token calls are never captured, and password / OTP / token fields
// are blanked out of anything that is.
(function () {
  if (window.__kr883ActivityHook) return;
  window.__kr883ActivityHook = true;
  var WANTED = /\/api\/(members\/?(\?|$)|loans\/credit-report)/;
  var API = /\/api\//;
  var AUTH = /login|2fa|otp|token|logout|password|verify|auth/i;
  var SECRET_KEY = /pass|otp|token|secret|pin|code|authorization/i;

  function redact(v, depth) {
    if (depth > 6 || v === null || typeof v !== "object") return v;
    var out = Array.isArray(v) ? [] : {};
    for (var k in v) out[k] = SECRET_KEY.test(k) ? "***" : redact(v[k], depth + 1);
    return out;
  }
  function cleanBody(b) {
    if (b == null) return null;
    if (typeof b !== "string") { try { b = JSON.stringify(b); } catch (e) { return null; } }
    try { return redact(JSON.parse(b), 0); } catch (e) {}
    return /pass|otp|token|secret/i.test(b) ? "(not captured)" : b.slice(0, 500);
  }
  function pathOf(url) { try { return new URL(url, location.href).pathname; } catch (e) { return String(url); } }

  function emit(url, method, status, text, reqBody) {
    try {
      method = String(method || "GET").toUpperCase();
      if (method === "GET") {
        if (!WANTED.test(url)) return;
        var body = null;
        try { body = JSON.parse(text); } catch (e) { return; }
        window.postMessage({ source: "kr883-activity", event: {
          url: new URL(url, location.href).href, status: status, body: body, at: new Date().toISOString()
        } }, "*");
        return;
      }
      if (!API.test(url) || AUTH.test(pathOf(url))) return;           // actions only, never auth calls
      var resp = null;
      try { resp = redact(JSON.parse(text), 0); } catch (e) { resp = String(text || "").slice(0, 300); }
      window.postMessage({ source: "kr883-activity", event: {
        type: "action", method: method, url: new URL(url, location.href).href, path: pathOf(url),
        status: status, request: cleanBody(reqBody), response: resp, at: new Date().toISOString()
      } }, "*");
    } catch (e) {}
  }

  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      var p = origFetch.apply(this, arguments);
      try {
        var url = typeof input === "string" ? input : (input && input.url) || "";
        var method = (init && init.method) || (input && input.method) || "GET";
        if (WANTED.test(url) || (API.test(url) && String(method).toUpperCase() !== "GET")) {
          var reqBody = init && init.body;
          p.then(function (r) { r.clone().text().then(function (t) { emit(url, method, r.status, t, reqBody); }); }).catch(function () {});
        }
      } catch (e) {}
      return p;
    };
  }

  var open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__kr = { method: method, url: String(url) };
    return open.apply(this, arguments);
  };
  var send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.send = function (body) {
    var x = this;
    if (x.__kr) x.__kr.body = body;
    x.addEventListener("load", function () {
      try {
        var k = x.__kr;
        if (!k || !(WANTED.test(k.url) || API.test(k.url))) return;
        if (x.responseType === "" || x.responseType === "text") emit(k.url, k.method, x.status, x.responseText, k.body);
        else if (x.responseType === "json") emit(k.url, k.method, x.status, JSON.stringify(x.response), k.body);
        else emit(k.url, k.method, x.status, "", k.body);
      } catch (e) {}
    });
    return send.apply(this, arguments);
  };
})();
