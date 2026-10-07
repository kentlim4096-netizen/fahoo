"""SessionSync - reuse the ALREADY-VALID KR883 session from the normal (worker's) Chrome in the
dedicated Playwright profile, instead of logging in again.

Why: KR883 allows one active session per account. A second login (password + OTP) supersedes the
first ("Signed in on another device"), which logs the worker out. So the scraper must never log in
on its own; it mirrors the session the worker already has.

Sources, in order (read-only - nothing in the worker's Chrome is clicked, navigated, typed into,
modified or closed):
  1. the live localStorage of an ALREADY OPEN KR883 tab, read over Chrome's debug port
  2. Chrome's on-disk localStorage (LevelDB) for the profile - may lag behind the live state

What is copied: the short-lived ACCESS token (+ the app's profile blob). The REFRESH token is
deliberately NOT copied and the dedicated context blocks token-refresh calls (see
credit_report_provider), so the mirror can never rotate the session's refresh token and break the
worker's copy. When the access token ages out, the mirror simply re-syncs from Chrome (which does
its own refreshing).

Security: tokens are never printed or logged; nothing is hard-coded; a source is only used if the
KR883 server itself accepts its access token.
"""
import asyncio, base64, glob, hashlib, hmac, json, os, re, struct, time

KEYS_COPIED = ("kw388_access", "kw388_profile")     # refresh token intentionally excluded
KEYS_READ = ("kw388_access", "kw388_refresh", "kw388_profile")


def _totp_now(secret, digits=6, period=30, algo="sha1"):
    """-> (code, seconds left in this window). Same sha1/6/30 TOTP KR883 itself uses."""
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    counter = int(time.time() // period)
    h = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, algo, hashlib.sha1)).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits), period - int(time.time() % period)


def jwt_claims(tok):
    try:
        p = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}


def seconds_left(tok):
    exp = jwt_claims(tok).get("exp")
    return (exp - time.time()) if exp else None


class SessionSync:
    def __init__(self, base_url, api_base="https://api.kw388.com", cdp_url="http://127.0.0.1:9222",
                 chrome_leveldb=None, log=None):
        self.base = base_url.rstrip("/")
        self.api_base = api_base.rstrip("/")
        self.cdp_url = cdp_url
        self.leveldb = chrome_leveldb or os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data", "Profile 1", "Local Storage", "leveldb")
        self.log = log or (lambda *_: None)
        self.syncs = 0                      # successful copies into the dedicated profile
        self.last = {"source": None, "at": None, "ok": None}

    # ---------------------------------------------------------------- sources (read-only)
    async def read_from_chrome_live(self):
        """localStorage of an already-open KR883 tab in the normal Chrome. Read-only: a single
        localStorage read; no navigation, no clicks, no new tabs.

        Talks raw CDP (the debug port's own HTTP + WebSocket protocol) instead of going through
        Playwright's page discovery: Playwright's connect_over_cdp builds its page list from
        browser-context auto-attach events, which can silently miss a tab that was already open
        before it connected (observed on this exact tab - /json/list sees it fine, Playwright's
        ctx.pages does not). Raw CDP sees every target /json/list reports, regardless."""
        from aiohttp import ClientSession, ClientTimeout
        try:
            async with ClientSession(timeout=ClientTimeout(total=4)) as s:
                async with s.get(self.cdp_url.rstrip("/") + "/json/list") as r:
                    targets = await r.json()
        except Exception as e:
            self.log(f"[sync] live read unavailable: {type(e).__name__}")
            return None
        candidates = [t for t in targets if t.get("type") == "page"
                      and "kr883" in t.get("url", "") and "/login" not in t.get("url", "")
                      and t.get("webSocketDebuggerUrl")]
        expr = ("JSON.stringify(Object.fromEntries(%s.map(k => [k, localStorage.getItem(k)])"
                ".filter(([k, v]) => v)))") % json.dumps(list(KEYS_READ))
        for t in candidates:
            try:
                async with ClientSession(timeout=ClientTimeout(total=4)) as s:
                    async with s.ws_connect(t["webSocketDebuggerUrl"]) as ws:
                        await ws.send_json({"id": 1, "method": "Runtime.evaluate",
                                            "params": {"expression": expr, "returnByValue": True}})
                        async for msg in ws:
                            data = json.loads(msg.data)
                            if data.get("id") != 1:
                                continue
                            val = data.get("result", {}).get("result", {}).get("value")
                            if val:
                                ls = json.loads(val)
                                if ls.get("kw388_access"):
                                    return ls
                            break
            except Exception:
                continue
        return None

    # ---------------------------------------------------------------- genuine re-login
    async def auto_relogin_cdp(self, username, password, totp_secret):
        """When the worker's OWN Chrome session has genuinely logged out (not just unreadable),
        finish signing back in on that SAME tab/profile - not a second, competing login elsewhere.
        Because it refreshes the one real session in place (exactly what the worker would do by
        hand), it cannot trigger KR883's one-session-per-account kick-out the way a separate login
        would. Driven over raw CDP (Runtime.evaluate / Page.navigate), not Playwright - see
        read_from_chrome_live for why Playwright's page discovery can't be trusted on this tab.
        Credentials/secret are never logged."""
        if not (username and password and totp_secret):
            return False
        from aiohttp import ClientSession, ClientTimeout
        try:
            async with ClientSession(timeout=ClientTimeout(total=4)) as s:
                async with s.get(self.cdp_url.rstrip("/") + "/json/list") as r:
                    targets = await r.json()
        except Exception as e:
            self.log(f"[relogin] cannot reach Chrome debug port: {type(e).__name__}")
            return False
        t = next((x for x in targets if x.get("type") == "page" and "kr883" in x.get("url", "")
                  and x.get("webSocketDebuggerUrl")), None)
        if not t:
            # Chrome is up but has no KR883 tab (e.g. it wasn't open when Chrome last closed) -
            # open one, same as the worker opening a new tab themselves. Newer Chrome requires PUT
            # for /json/new (a CSRF hardening change); older Chrome only accepts GET - try PUT first.
            url = self.cdp_url.rstrip("/") + "/json/new?" + self.base + "/login"
            try:
                async with ClientSession(timeout=ClientTimeout(total=10)) as s:
                    async with s.put(url) as r:
                        if r.status == 405:
                            async with s.get(url) as r2:
                                t = await r2.json()
                        else:
                            t = await r.json()
                self.log("[relogin] no KR883 tab was open - opened a new one")
            except Exception as e:
                self.log(f"[relogin] could not open a new tab: {type(e).__name__}")
                return False
        if not t or not t.get("webSocketDebuggerUrl"):
            return False

        counter = {"n": 0}

        async def call(ws, method, params=None):
            counter["n"] += 1
            cid = counter["n"]
            await ws.send_json({"id": cid, "method": method, "params": params or {}})
            async for msg in ws:
                data = json.loads(msg.data)
                if data.get("id") == cid:
                    return data.get("result", {})
            return {}

        async def ev(ws, expr):
            r = await call(ws, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
            return r.get("result", {}).get("value")

        set_val_js = (
            "(function(sel, val) { const el = document.querySelector(sel); if (!el) return false; "
            "const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').set; "
            "setter.call(el, val); el.dispatchEvent(new Event('input', {bubbles: true})); "
            "el.dispatchEvent(new Event('change', {bubbles: true})); return true; })(%s, %s)")
        click_js = (
            "(function(label) { const b = Array.from(document.querySelectorAll('button')).find("
            "x => x.textContent.trim().toLowerCase().includes(label) && !x.disabled); "
            "if (b) { b.click(); return true; } return false; })(%s)")

        try:
            async with ClientSession(timeout=ClientTimeout(total=30)) as s:
                async with s.ws_connect(t["webSocketDebuggerUrl"]) as ws:
                    await call(ws, "Page.navigate", {"url": self.base + "/login"})
                    await asyncio.sleep(1.8)
                    url = await ev(ws, "location.href") or ""
                    if "login" not in url:
                        self.log("[relogin] already logged in once the page settled")
                        return True

                    await ev(ws, set_val_js % (json.dumps("input[type=text]"), json.dumps(username)))
                    await ev(ws, set_val_js % (json.dumps("input[type=password]"), json.dumps(password)))
                    if not await ev(ws, click_js % json.dumps("sign in")):
                        await ev(ws, "document.querySelector('button[type=submit]')?.click()")
                    await asyncio.sleep(2.5)
                    url = await ev(ws, "location.href") or ""

                    if "login" in url:          # 2FA step
                        for _attempt in range(2):   # a code can straddle a 30s boundary
                            code, left = _totp_now(totp_secret)
                            if left < 4:
                                await asyncio.sleep(left + 0.5)
                                code, left = _totp_now(totp_secret)
                            await ev(ws, set_val_js % (json.dumps("input[type=text]"), json.dumps(code)))
                            await asyncio.sleep(0.5)
                            for label in ("verify", "submit", "continue", "sign in"):
                                if await ev(ws, click_js % json.dumps(label)):
                                    break
                            await asyncio.sleep(2.5)
                            url = await ev(ws, "location.href") or ""
                            if "login" not in url:
                                break

                    ok = "login" not in url
                    self.log(f"[relogin] {'succeeded' if ok else 'failed'} on the same Chrome tab")
                    return ok
        except Exception as e:
            self.log(f"[relogin] error: {type(e).__name__}: {str(e)[:120]}")
            return False

    def read_from_chrome_disk(self):
        """Newest unexpired access token found in Chrome's on-disk localStorage files."""
        best, prof = ("", 0), ""
        if not os.path.isdir(self.leveldb):
            return None
        for f in sorted(glob.glob(os.path.join(self.leveldb, "*")), key=os.path.getmtime):
            if not f.endswith((".ldb", ".log")):
                continue
            try:
                with open(f, "rb") as fh:
                    blob = fh.read()
            except OSError:
                continue
            for m in re.findall(rb"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", blob):
                tok = m.decode("ascii", "ignore")
                c = jwt_claims(tok)
                if c.get("token_type") == "access" and c.get("exp", 0) > best[1]:
                    best = (tok, c["exp"])
        return {"kw388_access": best[0]} if best[0] else None

    async def server_accepts(self, access):
        """Does KR883 itself accept this access token right now? (cheap read, creates no session)"""
        from aiohttp import ClientSession, ClientTimeout
        try:
            async with ClientSession(headers={"Authorization": "Bearer " + access, "Accept": "application/json",
                                              "User-Agent": "Mozilla/5.0"}, timeout=ClientTimeout(total=15)) as http:
                async with http.get(self.api_base + "/api/members/", params={"page": 1, "page_size": 1}) as r:
                    return r.status == 200
        except Exception:
            return False

    async def find_source(self):
        """-> (localStorage subset, 'chrome-live'|'chrome-disk') for a currently valid session, else None."""
        for name, getter in (("chrome-live", self.read_from_chrome_live), ("chrome-disk", None)):
            ls = await getter() if getter else self.read_from_chrome_disk()
            acc = (ls or {}).get("kw388_access", "")
            left = seconds_left(acc) if acc else None
            if acc and left is not None and left > 120 and await self.server_accepts(acc):
                self.log(f"[sync] valid session found via {name} (access token good for ~{int(left)}s)")
                return ls, name
            if acc:
                self.log(f"[sync] {name}: token present but expired / rejected by server")
        return None

    # ---------------------------------------------------------------- targets
    async def inject_into_page(self, page, ls):
        """Write the access token (+ profile blob) into the dedicated profile's localStorage on the
        KR883 origin. The refresh token is removed/not copied so the mirror can never refresh."""
        items = {k: ls[k] for k in KEYS_COPIED if ls.get(k)}
        await page.evaluate(
            "(items) => { localStorage.removeItem('kw388_refresh'); for (const [k, v] of Object.entries(items)) localStorage.setItem(k, v); }",
            items)
        self.syncs += 1
        self.last = {"source": "chrome", "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": True}

    def save_api_session(self, session_file, ls, user_agent=""):
        """Point the API side (kw_session.json) at the same session (access token only)."""
        os.makedirs(os.path.dirname(session_file), exist_ok=True)
        with open(session_file, "w", encoding="utf-8") as f:
            json.dump({"userAgent": user_agent, "origin": self.base, "localStorage": {k: ls[k] for k in KEYS_COPIED if ls.get(k)},
                       "cookies": [], "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": "normal-chrome"}, f, indent=2)
