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
import asyncio, base64, glob, json, os, re, time

KEYS_COPIED = ("kw388_access", "kw388_profile")     # refresh token intentionally excluded
KEYS_READ = ("kw388_access", "kw388_refresh", "kw388_profile")


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
        localStorage read; no navigation, no clicks, no new tabs."""
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                b = await p.chromium.connect_over_cdp(self.cdp_url, timeout=4000)
                try:
                    for ctx in b.contexts:
                        for pg in ctx.pages:
                            if "kr883" not in pg.url or "/login" in pg.url:
                                continue
                            ls = await pg.evaluate(
                                "(keys) => Object.fromEntries(keys.map(k => [k, localStorage.getItem(k)]).filter(([k, v]) => v))",
                                list(KEYS_READ))
                            if ls.get("kw388_access"):
                                return ls
                finally:
                    await b.close()          # disconnect only; the browser keeps running
        except Exception as e:
            self.log(f"[sync] live read unavailable: {type(e).__name__}")
        return None

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
