"""Record a manual KR883 workflow (Menu -> Member List -> search IC -> 3-dot -> Credit Report) in a
DEDICATED persistent Playwright profile, so the steps after authentication can be analysed.

  python tools/record_workflow.py            # opens the dedicated profile, headed
  python tools/record_workflow.py --check    # only report whether the profile is authenticated

What it does
  1. Opens data/kw_profile_workflow (its own Chrome profile - never your everyday Chrome and never
     the worker's browser; no remote-debugging attach).
  2. Checks whether that profile is already authenticated (valid KR883 JWT in localStorage and the
     app does not bounce to /login).
       - authenticated  -> goes straight to the dashboard. No login page, no OTP.
       - not authenticated -> STOPS and tells you to log in by hand in the window. It never types a
         password or OTP. Once you have logged in it carries on and keeps the profile.
  3. Records what YOU do: page navigations, clicks (element text / role / aria-label / selector),
     dialogs that appear after a click (trimmed HTML), and the /api/ calls the app makes (method,
     URL, status, response shape). Tokens / cookies / Authorization headers are never written.
  4. Stops when you close the window, or create the file  data/recordings/STOP.

Output: data/recordings/<timestamp>/events.jsonl and summary.md
"""
import argparse, asyncio, base64, datetime, json, os, re, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = (os.environ.get("KW388_BASE_URL") or "https://admin.kr883.com").rstrip("/")
PROFILE = os.environ.get("KW388_WORKFLOW_PROFILE", os.path.join(HERE, "data", "kw_profile_workflow"))
REC_ROOT = os.path.join(HERE, "data", "recordings")
STEALTH = ["--disable-blink-features=AutomationControlled", "--exclude-switches=enable-automation"]

SECRET_KEYS = re.compile(r"token|authorization|cookie|password|secret|otp|jwt", re.I)

# Injected into every page: reports clicks and any dialog/popup that appears afterwards.
INIT_JS = r"""
(() => {
  if (window.__kwRec) return; window.__kwRec = true;
  const cssPath = el => {
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 5) {
      let s = el.tagName.toLowerCase();
      if (el.id) { s += '#' + el.id; parts.unshift(s); break; }
      const cls = (el.getAttribute('class') || '').split(/\s+/).filter(c => c && !/^(v-|data-v)/.test(c)).slice(0, 2);
      if (cls.length) s += '.' + cls.join('.');
      parts.unshift(s); el = el.parentElement;
    }
    return parts.join(' > ');
  };
  const describe = el => {
    const t = el.closest('button,a,[role=menuitem],[role=button],li,tr,td,th,label,input,select') || el;
    return {
      tag: t.tagName.toLowerCase(), role: t.getAttribute('role') || '',
      text: (t.innerText || t.value || '').trim().replace(/\s+/g, ' ').slice(0, 80),
      aria: t.getAttribute('aria-label') || '', title: t.getAttribute('title') || '',
      testid: t.getAttribute('data-testid') || '', css: cssPath(t),
      row: (t.closest('tr') ? (t.closest('tr').innerText || '').trim().replace(/\s+/g, ' ').slice(0, 100) : ''),
    };
  };
  const dialogs = () => [...document.querySelectorAll('[role=dialog],[aria-modal=true],.modal,[class*=modal],[class*=popup],[class*=drawer],[class*=dropdown-menu],[role=menu]')]
      .filter(e => e.offsetParent !== null || getComputedStyle(e).position === 'fixed');
  document.addEventListener('click', e => {
    const d = describe(e.target);
    window.__kwEmit && window.__kwEmit({ type: 'click', ...d, x: e.clientX, y: e.clientY });
    setTimeout(() => {
      const ds = dialogs().slice(-2).map(x => ({ css: cssPath(x), text: (x.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 600), html: x.outerHTML.slice(0, 5000) }));
      if (ds.length) window.__kwEmit && window.__kwEmit({ type: 'overlay_after_click', after: d.text || d.css, overlays: ds });
    }, 1500);
  }, true);
  document.addEventListener('change', e => {
    const t = e.target; if (!t || !t.tagName) return;
    const isPw = t.type === 'password';
    window.__kwEmit && window.__kwEmit({ type: 'input', tag: t.tagName.toLowerCase(), name: t.name || '', placeholder: t.placeholder || '', inputType: t.type || '',
      valueLength: isPw ? undefined : (t.value || '').length, css: cssPath(t) });
  }, true);
})();
"""


def jwt_exp(tok):
    try:
        b = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4))).get("exp", 0)
    except Exception:
        return 0


async def auth_state(page):
    """('authenticated'|'expired'|'no_session', detail)"""
    url = page.url
    try:
        ls = await page.evaluate("() => ({a: localStorage.getItem('kw388_access')||'', r: localStorage.getItem('kw388_refresh')||''})")
    except Exception:
        ls = {"a": "", "r": ""}
    if "/login" in url:
        return "no_session" if not (ls["a"] or ls["r"]) else "expired", url
    if ls["a"]:
        left = jwt_exp(ls["a"]) - time.time()
        return "authenticated", f"access token valid {int(left)}s more" if left > 0 else "access token past expiry but app did not redirect to login (refresh token in use)"
    return "no_session", url


def scrub(v):
    if isinstance(v, dict):
        return {k: ("<redacted>" if SECRET_KEYS.search(k) else scrub(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [scrub(x) for x in v[:3]]
    return v


def shape(v, depth=0):
    """Compact structural description of a JSON body (keys + types, first list item)."""
    if isinstance(v, dict):
        return {k: (shape(x, depth + 1) if depth < 3 else type(x).__name__) for k, x in list(v.items())[:40]}
    if isinstance(v, list):
        return [shape(v[0], depth + 1)] if v else []
    return type(v).__name__


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="only report authentication state, then exit")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--login-only", action="store_true", help="just restore the authenticated profile (log in by hand), no recording")
    a = ap.parse_args()

    from playwright.async_api import async_playwright
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(REC_ROOT, stamp)
    os.makedirs(out_dir, exist_ok=True)
    stop_file = os.path.join(REC_ROOT, "STOP")
    if os.path.exists(stop_file):
        os.remove(stop_file)
    events_path = os.path.join(out_dir, "events.jsonl")
    ev_f = open(events_path, "a", encoding="utf-8")
    t0 = time.time()

    def emit(ev):
        ev["t"] = round(time.time() - t0, 2)
        ev_f.write(json.dumps(ev, ensure_ascii=False) + "\n"); ev_f.flush()

    async with async_playwright() as p:
        launch = dict(headless=a.headless or a.check, viewport=None if not (a.headless or a.check) else {"width": 1400, "height": 1000},
                      args=STEALTH + ([] if (a.headless or a.check) else ["--start-maximized"]), channel="chrome")
        try:
            ctx = await p.chromium.launch_persistent_context(PROFILE, **launch)
        except Exception:
            launch.pop("channel")
            ctx = await p.chromium.launch_persistent_context(PROFILE, **launch)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def on_emit(_src, ev):
            emit(ev)
        await ctx.expose_binding("__kwEmit", on_emit)
        await ctx.add_init_script(INIT_JS)

        # ---- network: only the app's API calls; bodies reduced to structure ----
        async def on_response(r):
            u = r.url
            if "/api/" not in u:
                return
            rec = {"type": "api", "method": r.request.method, "url": re.sub(r"(token|key)=[^&]+", r"\1=<redacted>", u), "status": r.status}
            try:
                pd = r.request.post_data
                if pd:
                    rec["request_body"] = scrub(json.loads(pd)) if pd.strip().startswith(("{", "[")) else "<non-json>"
            except Exception:
                pass
            try:
                if "json" in (r.headers.get("content-type") or ""):
                    rec["response_shape"] = shape(await r.json())
            except Exception:
                pass
            emit(rec)
        ctx.on("response", lambda r: asyncio.ensure_future(on_response(r)))
        page.on("framenavigated", lambda f: f == page.main_frame and emit({"type": "navigate", "url": f.url}))

        await page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)
        state, detail = await auth_state(page)
        print(f"AUTH STATE: {state} ({detail})")
        emit({"type": "auth_state", "state": state, "detail": detail, "url": page.url})

        if a.check:
            await ctx.close(); ev_f.close()
            sys.exit(0 if state == "authenticated" else 2)

        if state != "authenticated":
            print("\nNot authenticated in the dedicated profile. Log in BY HAND in the browser window that just opened")
            print("(nothing here types your password or OTP). Waiting for you...  Ctrl+C to give up.")
            emit({"type": "manual_login_required"})
            while True:
                if not ctx.pages:
                    print("Window closed before login."); ev_f.close(); return
                await asyncio.sleep(3)
                try:
                    state, detail = await auth_state(ctx.pages[0])
                except Exception:
                    continue
                if state == "authenticated":
                    print("Authenticated - profile saved. Recording starts now.")
                    emit({"type": "manual_login_done"}); break
        if a.login_only:
            print("Profile is authenticated and saved. Closing.")
            await ctx.close(); ev_f.close(); return
        print("\nRECORDING. Now do, by hand:  Menu -> Member List -> type the IC -> search -> 3 dots -> Credit Report -> wait for popup.")
        print("Stop by closing the window (or create data/recordings/STOP).")

        try:
            while ctx.pages and not os.path.exists(stop_file):
                await asyncio.sleep(1)
                if int(time.time() - t0) % 5 == 0:
                    pass
            # one last page snapshot of whatever is open (popup still showing)
            try:
                pg = ctx.pages[0]
                emit({"type": "final_page", "url": pg.url, "text": (await pg.inner_text("body"))[:3000]})
            except Exception:
                pass
        finally:
            try:
                await ctx.close()
            except Exception:
                pass
    ev_f.close()

    # ---- summary ----
    evs = [json.loads(l) for l in open(events_path, encoding="utf-8")]
    lines = ["# Recorded workflow", "", f"Events: {len(evs)}  |  folder: {out_dir}", "", "## Step sequence (clicks / navigation / inputs)", ""]
    n = 0
    for e in evs:
        if e["type"] == "navigate":
            lines.append(f"- [{e['t']}s] NAVIGATE {e['url']}")
        elif e["type"] == "click":
            n += 1
            what = e.get("text") or e.get("aria") or e.get("title") or e.get("css")
            lines.append(f"- [{e['t']}s] CLICK #{n}: <{e['tag']}{' role=' + e['role'] if e['role'] else ''}> \"{what}\"" + (f"  (row: {e['row']})" if e.get("row") else "") + f"  css: `{e['css']}`")
        elif e["type"] == "input":
            lines.append(f"- [{e['t']}s] INPUT <{e['tag']} type={e['inputType']} name={e['name']} placeholder=\"{e['placeholder']}\"> length={e.get('valueLength')}")
        elif e["type"] == "overlay_after_click":
            for o in e["overlays"]:
                lines.append(f"- [{e['t']}s] OVERLAY after \"{e['after']}\": `{o['css']}` — {o['text'][:160]}")
        elif e["type"] == "api":
            lines.append(f"- [{e['t']}s] API {e['method']} {e['url']} -> {e['status']}")
    with open(os.path.join(out_dir, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nSaved {events_path}\nSaved {os.path.join(out_dir, 'summary.md')}")


if __name__ == "__main__":
    asyncio.run(main())
