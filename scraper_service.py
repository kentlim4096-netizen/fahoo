"""
credit report tool — runs in its own container, fully unattended. Login is TOTP-automated (KW388's
2FA is a standard authenticator-app code, computed here from the secret — no human ever needs to
type anything), so this can run on a schedule or on-demand with zero interaction.

Exposes a tiny internal HTTP API (never published outside the Docker network — apps/api proxies
/kw-leads/scrape through it):
  GET  /status              current run state (mirrors KwScrapeStatusDto in packages/shared)
  POST /scrape               start a run if one isn't already in progress. Optional JSON body
                             {mode: 'all'|'new'|'old', limit: number} scopes the candidate pool
                             to not-yet-known / already-known ICs and/or caps how many get
                             scraped; omitting the body (or any field) keeps today's behavior
                             (full pool, no cap) — the in-app cron scheduler relies on this.
                             Every mode also skips already-known customers who are 30+ days
                             chronically overdue (OVERDUE_SKIP_DAYS) — not worth re-scraping.
  GET  /candidates/preview   parses the uploaded candidate list and reports
                             {total, new, old, oldOverdueSkip} against the backend's current
                             kw_leads — no Playwright/KW388 involved.

One run = full re-scan of the candidate pool (one or more xlsx files uploaded via the backend's
"Upload List" button, merged + deduped — see load_ctm) -> everyone who took a loan today ->
POST straight to the backend's /kw-leads/import. Same-day progress is cached to disk so a crash
mid-run resumes instead of re-scraping from zero; a fresh container start always re-scans
(the cache lives in a volume keyed by date, so a genuinely new day starts clean on its own).
"""
import os, re, random, sys, json, asyncio, time, hmac, hashlib, struct, base64, datetime, functools
from collections import defaultdict
import urllib.parse
import openpyxl
from aiohttp import web, ClientSession, ClientTimeout
from playwright.async_api import async_playwright

# ---------------- .env loading (native/local runs) ----------------
# Under Docker, compose's `env_file:` populates the environment before this process starts. Run
# natively (python scraper_service.py) there's nothing doing that, so read .env next to this file
# ourselves. Real environment variables always win, so this never overrides an explicit export.
def _load_dotenv(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)

_load_dotenv()

# ---------------- CONFIG (env) ----------------
# All credentials are read with .get rather than [] so the service can START without them and
# report a clear error at the point of use. Previously a missing var raised KeyError at import
# time, which meant a local/backend-less install couldn't even boot far enough to serve /status.
KW_USER = os.environ.get("KW388_USERNAME", "")
KW_PASS = os.environ.get("KW388_PASSWORD", "")
KW_TOTP_SECRET = os.environ.get("KW388_TOTP_SECRET", "")
# KW388 has moved domain before (kw388.com -> kr883.com). Keep it configurable so the next move
# is an .env edit rather than a code change; strip any trailing slash so URL building is safe.
KW_BASE_URL = os.environ.get("KW388_BASE_URL", "https://admin.kr883.com").rstrip("/")
# Attach to a Chrome you already have open and logged in, instead of logging in ourselves.
# KR883 allows one session per account, so a separate scraper login kicks the human out (and
# vice versa). Sharing one browser session avoids that fight entirely - and needs no TOTP.
# Start Chrome with:  chrome.exe --remote-debugging-port=9222
KW_CDP_URL = os.environ.get("KW388_CDP_URL", "").strip()
# Captured browser session (cookies incl. Cloudflare cf_clearance, plus the exact user-agent it
# was issued for). KR883 sits behind Cloudflare bot protection; a fresh headless browser gets
# blocked, but replaying the user's cleared session + matching user-agent + same machine IP gets
# through. Written by tools/import_chrome_session.py.
KW_SESSION_FILE = os.environ.get("KW388_SESSION_FILE",
                                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "kw_session.json"))
# Use the real installed Chrome rather than bundled Chromium when available - far fewer automation
# fingerprints for Cloudflare to flag. Set KW388_BROWSER_CHANNEL="" to force bundled Chromium.
KW_BROWSER_CHANNEL = os.environ.get("KW388_BROWSER_CHANNEL", "chrome").strip()

def _shared_session_default():
    try:
        return os.path.exists(KW_SESSION_FILE)
    except Exception:
        return False



def load_session():
    """Captured cookies + user-agent, or None. Kept out of the persistent profile so a rotated
    cf_clearance can be refreshed by re-running the import tool without wiping the profile."""
    try:
        with open(KW_SESSION_FILE, encoding="utf-8") as f:
            d = json.load(f)
        # A KR883 session is localStorage JWTs (cookies list is empty), so accept EITHER.
        if d.get("cookies") or d.get("localStorage"):
            return d
    except Exception:
        pass
    return None


# Anti-automation launch args: drop the AutomationControlled blink feature so navigator.webdriver
# isn't trivially true, and quiet the "Chrome is being controlled by automated software" banner.
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled",
                "--exclude-switches=enable-automation"]


# ---------------- KR883 API host ----------------
# Credit reports are NOT fetched from here any more - they come only from the UI workflow (see
# credit_report_provider.py). The API host is still used for the session token checks / refresh
# helpers and the Members-page list scrape. It is separate from the admin UI host (did NOT rename).
KW_API_BASE = os.environ.get("KW388_API_BASE", "https://api.kw388.com").rstrip("/")
# Human-like pacing: one candidate at a time, with a random pause of KW_HUMAN_DELAY_MIN..MAX seconds
# between them (also used between Members-page scrape pages). 0 for both = no pause.
KW_HUMAN_DELAY_MIN = float(os.environ.get("KW_HUMAN_DELAY_MIN", 0))
KW_HUMAN_DELAY_MAX = float(os.environ.get("KW_HUMAN_DELAY_MAX", 0))

# ---- Credit-report provider mode -------------------------------------------------------------
#   CREDIT_REPORT_MODE=ui  (default) genuine Playwright workflow in ONE persistent dedicated profile:
#                          Member List -> IC -> Apply -> Actions -> Credit report -> popup -> extract.
#                          Never logs in / never touches OTP: an expired session pauses the queue with
#                          MANUAL_REAUTH_REQUIRED until a human logs in and presses Resume.
#   CREDIT_REPORT_MODE=api the existing direct-API pipeline (authorized/debug use, much faster).
# The direct-API credit-report path has been retired: the UI workflow is the only source.
if (os.environ.get("CREDIT_REPORT_MODE") or "ui").strip().lower() != "ui":
    print("CREDIT_REPORT_MODE=api is no longer supported - using the UI workflow", file=sys.stderr)
CREDIT_REPORT_MODE = "ui"
UI_PROFILE_DIR = os.environ.get("KW388_WORKFLOW_PROFILE",
                                os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "kw_profile_workflow"))
UI_HEADLESS = os.environ.get("KW388_UI_HEADLESS", "true").strip().lower() not in ("0", "false", "no", "off")
UI_SLOW_MO = int(os.environ.get("KW388_UI_SLOW_MO", 0))
_MYT = datetime.timedelta(hours=8)
def _iso_to_myt(iso):
    """API timestamps are ISO UTC; the browser scraper saw Malaysia local time. Convert + format
    to the '%d %b %Y' / '%H:%M' strings the existing parsers expect, so output is identical."""
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo:
            dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None) + _MYT
        return dt
    except Exception:
        return None


def api_report_to_shape(d):
    """Transform the credit-report API JSON into the {found,stats,loans,txs} shape the browser
    scraper produced, so kw388_signals/recent5/loans_by_*/overdue_streak_days run UNCHANGED.
    Verified field-for-field identical to the browser output across multiple ICs."""
    cd = d.get("customer_details") or {}
    ld = d.get("loan_details") or []
    tl = d.get("transaction_log") or []
    active = sum(1 for l in ld if l.get("loan_status") in ACTIVE_LOAN_STATUSES)
    stats = {
        "Total completed": str(cd.get("closed") or 0),
        "Total disbursed": str(active),
        "Blacklists": str(cd.get("blacklist_count") or 0),
        "Overdue days": str(cd.get("active_overdue_days") or 0),
    }
    loans = []
    last_disbursed = None
    last_loan_created = None
    for l in ld:
        dt = _iso_to_myt(l.get("created_on"))
        if not dt:
            continue
        status = l.get("loan_status") or ""
        # Newest loan of any status (ld is newest-first) — its creation date. Due date/amount/tenure
        # are NOT in this endpoint (only in /api/loans/, which is scoped to our own admin's book).
        if last_loan_created is None:
            last_loan_created = dt.strftime("%Y-%m-%d")
        # Who disbursed this loan (loan_details is newest-first). A disbursed loan carries EITHER a
        # named admin login OR a platform/channel code (FG/TN/DM...) when no named admin - never
        # both, never neither (verified across 420 disbursed loans). The API exposes no disbursed
        # AMOUNT anywhere (loan_details has no amount field and there is no Loan Disbursement
        # transaction), so we surface the disburser + date + remark only.
        if last_disbursed is None and status == "Disbursed":
            admin = l.get("admin") or ""
            last_disbursed = {
                "by": admin or (l.get("platform") or ""),
                "type": "admin" if admin else ("platform" if l.get("platform") else ""),
                "admin": admin,
                "platform": l.get("platform") or "",
                "date": dt.strftime("%Y-%m-%d"),
                "remark": l.get("remark") or "",
            }
        # c[0]=status (recent5 "Completed"), c[1]=status (loans_by_date), c[3]=remark (loans_by_remark)
        loans.append({"date": dt.strftime("%d %b %Y"), "time": dt.strftime("%H:%M"),
                      "c": [status, status, "", l.get("remark") or ""]})
    txs = []
    last_payment = None
    for t in tl:
        dt = _iso_to_myt(t.get("date"))
        if not dt:
            continue
        amt = t.get("credit") or t.get("debit") or 0
        # The most recent actual repayment (tl is newest-first) — carries the processing admin,
        # which api_report_to_shape otherwise drops. Lets the bulk scrape answer "who paid which
        # admin" without a separate per-customer lookup.
        if last_payment is None and (t.get("transaction_type") or "") == "Loan Repayment":
            last_payment = {
                "admin": t.get("admin") or "",
                "adminId": t.get("admin_identifier") or "",
                "date": dt.strftime("%Y-%m-%d"),
                "amount": amt,
            }
        txs.append({"date": dt.strftime("%d %b %Y"), "time": dt.strftime("%H:%M"),
                    "c": [t.get("transaction_type") or "", str(amt)]})
    return {"found": True, "stats": stats, "loans": loans, "txs": txs,
            "lastPayment": last_payment, "lastDisbursed": last_disbursed,
            "lastLoanCreated": last_loan_created}


def _api_headers():
    sess = load_session() or {}
    tok = (sess.get("localStorage") or {}).get("kw388_access", "")
    ua = sess.get("userAgent", "") or "Mozilla/5.0"
    return {"Authorization": f"Bearer {tok}", "User-Agent": ua, "Accept": "application/json"}, bool(tok)


# ---- autonomous token lifecycle (keeps the KR883 session alive with no human) ----
# KR883 uses rotating JWTs: a short access token and a ~1h refresh token that is replaced on each
# refresh. Refreshing well within the hour keeps the session alive indefinitely while running.
# If the refresh token is itself dead (PC was off too long), we log in fresh from the .env
# credentials + TOTP - so the scraper never needs a manual re-import. Endpoints reverse-engineered
# from the app bundle.
KW_API_ROOT = KW_API_BASE + "/api"
_TOKEN_LOCK = asyncio.Lock()


def _jwt_exp(tok):
    try:
        p = tok.split(".")[1]
        p += "=" * ((4 - len(p) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("exp", 0)
    except Exception:
        return 0


def _save_tokens(access, refresh):
    """Persist new tokens into the session file (same localStorage shape everything else reads)."""
    sess = load_session() or {}
    ls = sess.get("localStorage") or {}
    if access:
        ls["kw388_access"] = access
    if refresh:
        ls["kw388_refresh"] = refresh
    sess["localStorage"] = ls
    sess.setdefault("origin", KW_BASE_URL)
    sess["tokenRefreshedAt"] = now_iso()
    try:
        os.makedirs(os.path.dirname(KW_SESSION_FILE), exist_ok=True)
        tmp = KW_SESSION_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sess, f, indent=2)
        os.replace(tmp, KW_SESSION_FILE)
    except Exception:
        pass


async def api_refresh_token():
    """Use the refresh token to get a fresh access (+ rotated refresh). Returns access or None."""
    sess = load_session() or {}
    rt = (sess.get("localStorage") or {}).get("kw388_refresh", "")
    ua = sess.get("userAgent", "") or "Mozilla/5.0"
    if not rt:
        return None
    try:
        async with ClientSession(timeout=ClientTimeout(total=20)) as s:
            async with s.post(KW_API_ROOT + "/users/admin/token/refresh/",
                              json={"refresh": rt},
                              headers={"User-Agent": ua, "Accept": "application/json"}) as r:
                if r.status != 200:
                    return None
                d = await r.json()
        if d.get("access"):
            _save_tokens(d.get("access"), d.get("refresh"))
            return d["access"]
    except Exception:
        pass
    return None


async def api_login_fresh():
    """Full autonomous login from .env credentials + TOTP (username/password -> 2FA verify).
    Returns a new access token or raises. This creates a new session; harmless now that workers
    use the local /report page rather than the KR883 browser."""
    if not (KW_USER and KW_PASS and KW_TOTP_SECRET):
        raise RuntimeError("cannot auto-login: KW388_USERNAME/PASSWORD/TOTP_SECRET not set in .env")
    ua = (load_session() or {}).get("userAgent", "") or "Mozilla/5.0"
    hdr = {"User-Agent": ua, "Accept": "application/json"}
    async with ClientSession(timeout=ClientTimeout(total=30), headers=hdr) as s:
        async with s.post(KW_API_ROOT + "/users/admin/login/",
                          json={"username": KW_USER, "password": KW_PASS}) as r:
            body = await r.text()
            if r.status != 200:
                raise RuntimeError(f"KR883 login failed ({r.status}): {body[:150]}")
            d = json.loads(body)
        if d.get("requires_2fa"):
            temp = d.get("temp_token")
            data = None
            for _ in range(2):  # a TOTP code can straddle the 30s boundary - retry once
                async with s.post(KW_API_ROOT + "/users/admin/2fa/verify/",
                                  json={"temp_token": temp, "otp_code": totp(KW_TOTP_SECRET)}) as r:
                    if r.status == 200:
                        data = await r.json()
                        break
                    await asyncio.sleep(1.5)
            if not data:
                raise RuntimeError("KR883 2FA verify failed (bad TOTP secret?)")
            d = data
        if not d.get("access"):
            raise RuntimeError("KR883 login returned no access token")
        _save_tokens(d.get("access"), d.get("refresh"))
        return d["access"]


# ---- follow-the-browser session (scraper rides the human's Chrome login) ----
# KR883 allows one session per account. Instead of the scraper logging in (which evicts the
# human's browser), it COPIES whatever tokens the browser currently holds and uses those - same
# access token, no new login, so the browser is never kicked. The browser owns and refreshes the
# session; the scraper is a read-only follower. Default ON. Set KW388_FOLLOW_BROWSER=false to use
# a dedicated scraper account instead (then auto-login/refresh is safe).
KW_FOLLOW_BROWSER = (os.environ.get("KW388_FOLLOW_BROWSER", "true").strip().lower() not in ("0","false","no","off"))
# Chrome's Local Storage leveldb for the KR883 profile. Override if the profile differs.
KW_CHROME_LEVELDB = os.environ.get(
    "KW388_CHROME_LEVELDB",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data",
                 "Profile 1", "Local Storage", "leveldb"))


def _read_locked(path):
    """Read a file even while Chrome holds it open (share-all flags via Win32)."""
    try:
        import win32file, win32con
        h = win32file.CreateFile(
            path, win32con.GENERIC_READ,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None, win32con.OPEN_EXISTING, 0, None)
        try:
            out = []
            while True:
                rc, data = win32file.ReadFile(h, 1 << 20)
                if not data:
                    break
                out.append(data)
            return b"".join(out)
        finally:
            h.Close()
    except Exception:
        try:
            return open(path, "rb").read()
        except Exception:
            return b""


def _classify_jwt(tok):
    """Return (token_type, exp) from a JWT payload, or (None, 0)."""
    try:
        p = tok.split(".")[1]
        p += "=" * ((4 - len(p) % 4) % 4)
        c = json.loads(base64.urlsafe_b64decode(p))
        return c.get("token_type"), c.get("exp", 0)
    except Exception:
        return None, 0


KW_CHROME_EXE = os.environ.get("KW388_CHROME_EXE", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
KW_CHROME_PROFILE = os.environ.get("KW388_CHROME_PROFILE", "Profile 1")
KW_AUTO_OPEN_CHROME = os.environ.get("KW388_AUTO_OPEN_CHROME", "true").strip().lower() not in ("0", "false", "no", "off")


async def ensure_chrome_running():
    """If Chrome (with the debug port) isn't up, launch the LAU profile so the scraper can read
    the KR883 session. Only launches when the port is down - if Chrome is already open, does
    nothing. Opening the profile restores its saved KR883 login automatically (if still valid)."""
    if not (KW_AUTO_OPEN_CHROME and KW_FOLLOW_BROWSER):
        return
    cdp = (os.environ.get("KW388_CDP_URL") or "http://127.0.0.1:9222").rstrip("/")
    async def port_up():
        try:
            async with ClientSession() as s:
                async with s.get(cdp + "/json/version", timeout=ClientTimeout(total=3)) as r:
                    return r.status == 200
        except Exception:
            return False
    if await port_up():
        return  # Chrome already running with the debug port
    if not os.path.exists(KW_CHROME_EXE):
        return
    try:
        import subprocess
        # Chrome 136+ only opens the debug port when --user-data-dir is passed explicitly
        # (even pointing at the real default folder), so the Lau profile + its KR883 login stay
        # intact while the port becomes reachable.
        udd = os.environ.get("KW388_CHROME_USERDATA",
                             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data"))
        subprocess.Popen(
            [KW_CHROME_EXE, "--user-data-dir=" + udd,
             "--profile-directory=" + KW_CHROME_PROFILE,
             "--remote-debugging-port=9222", "--restore-last-session"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    except Exception:
        return
    for _ in range(25):          # wait up to ~25s for Chrome + the port to come up
        await asyncio.sleep(1)
        if await port_up():
            await asyncio.sleep(2)   # let the SPA restore/load its tab
            return


async def browser_login_cdp():
    """Drive the dedicated KR883 Chrome (over its debug port) to sign in with the .env credentials
    + TOTP. Safe: this is the dedicated profile, not the human's everyday Chrome, so it never
    signs anyone out. Returns True if the window ends up logged in."""
    if not (KW_USER and KW_PASS and KW_TOTP_SECRET):
        return False
    cdp = (os.environ.get("KW388_CDP_URL") or "http://127.0.0.1:9222").rstrip("/")
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            b = await p.chromium.connect_over_cdp(cdp)
            try:
                pg = None
                for c in b.contexts:
                    for x in c.pages:
                        if "kr883" in x.url:
                            pg = x
                            break
                if pg is None:
                    pg = await (b.contexts[0] if b.contexts else await b.new_context()).new_page()
                await pg.goto(KW_BASE_URL + "/login", wait_until="domcontentloaded", timeout=45000)
                await pg.wait_for_timeout(1200)
                if "login" not in pg.url:
                    return True
                await pg.fill("input[type=text]", KW_USER)
                await pg.fill("input[type=password]", KW_PASS)
                try:
                    await pg.get_by_role("button", name="Sign in").click(timeout=5000)
                except Exception:
                    await pg.keyboard.press("Enter")
                await pg.wait_for_timeout(3000)
                if "login" in pg.url:  # 2FA step
                    box = pg.locator("input:not([type=password])").first
                    for _ in range(2):  # code can straddle a 30s boundary - retry once
                        try:
                            await box.wait_for(state="visible", timeout=8000)
                            await box.fill("")
                            await box.type(totp(KW_TOTP_SECRET), delay=60)
                            await pg.wait_for_timeout(700)
                            for label in ("Verify", "Submit", "Continue", "Sign in"):
                                btn = pg.get_by_role("button", name=label)
                                if await btn.count() and await btn.first.is_enabled():
                                    await btn.first.click()
                                    break
                            await pg.wait_for_timeout(3000)
                            if "login" not in pg.url:
                                break
                        except Exception:
                            await pg.wait_for_timeout(1500)
                await pg.goto(KW_BASE_URL + "/reports/credit", wait_until="domcontentloaded", timeout=45000)
                await pg.wait_for_timeout(1500)
                return "login" not in pg.url
            finally:
                await b.close()
    except Exception:
        return False


async def read_browser_session_cdp():
    """Read the LIVE KR883 tokens from the running Chrome via its debug port (in-memory, no
    disk-flush lag). Requires Chrome started with --remote-debugging-port=9222. Returns access or
    None."""
    cdp = os.environ.get("KW388_CDP_URL") or "http://127.0.0.1:9222"
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            b = await p.chromium.connect_over_cdp(cdp)
            try:
                for ctx in b.contexts:
                    for pg in ctx.pages:
                        if "kr883" in pg.url:
                            try:
                                ls = await pg.evaluate(
                                    "() => ({a:localStorage.getItem('kw388_access'),"
                                    " r:localStorage.getItem('kw388_refresh'),"
                                    " p:localStorage.getItem('kw388_profile')})")
                            except Exception:
                                continue
                            if ls and ls.get("a"):
                                sess = load_session() or {}
                                lsd = sess.get("localStorage") or {}
                                if ls.get("p"):
                                    lsd["kw388_profile"] = ls["p"]
                                sess["localStorage"] = lsd
                                _save_tokens(ls.get("a"), ls.get("r"))
                                return ls.get("a")
            finally:
                await b.close()
    except Exception:
        pass
    return None


def read_browser_session():
    """Pull the newest KR883 access + refresh tokens out of Chrome's on-disk localStorage and
    write them into the scraper's session file. Returns the access token, or None if none found.
    Picks the freshest of each kind by expiry, so old cached tokens are ignored."""
    import glob, re as _re
    d = KW_CHROME_LEVELDB
    if not os.path.isdir(d):
        return None
    blob = b""
    for f in sorted(glob.glob(os.path.join(d, "*")), key=lambda p: os.path.getmtime(p)):
        if f.endswith((".ldb", ".log")):
            blob += _read_locked(f)
    if b"kw388" not in blob:
        return None
    best = {"access": ("", 0), "refresh": ("", 0)}
    for m in _re.findall(rb"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", blob):
        tok = m.decode("ascii", "ignore")
        tt, exp = _classify_jwt(tok)
        if tt in best and exp > best[tt][1]:
            best[tt] = (tok, exp)
    access, refresh = best["access"][0], best["refresh"][0]
    if not access:
        return None
    _save_tokens(access, refresh)
    return access


async def ensure_api_token(force=False):
    """Return a valid access token, renewing as needed: reuse if fresh, else refresh, else full
    login. Serialised so concurrent callers don't stampede the endpoints."""
    async with _TOKEN_LOCK:
        if KW_FOLLOW_BROWSER:
            # Ride the browser's session. Never log in / refresh ourselves - that would evict the
            # human. Auto-open the LAU Chrome if it's closed, then prefer a LIVE read over the
            # debug port (no flush lag); fall back to disk.
            await ensure_chrome_running()
            acc = await read_browser_session_cdp()
            if acc and (_jwt_exp(acc) - time.time()) > 30:
                return acc
            acc = read_browser_session()
            if acc and (_jwt_exp(acc) - time.time()) > 30:
                return acc
            # Fall back to whatever we last copied, if still valid.
            acc = (load_session() or {}).get("localStorage", {}).get("kw388_access", "")
            if acc and (_jwt_exp(acc) - time.time()) > 30:
                return acc
            # The dedicated KR883 window is open but logged out - sign it in automatically
            # (credentials + TOTP from .env). Safe: it's the dedicated profile, not the everyday
            # Chrome, so nobody gets kicked out.
            if KW_ALLOW_AUTO_LOGIN and await browser_login_cdp():
                acc = await read_browser_session_cdp()
                if acc and (_jwt_exp(acc) - time.time()) > 30:
                    return acc
            raise RuntimeError(
                "The KR883 window isn't open. Open the KR883 desktop icon (it auto-signs-in), "
                "then scrapes and reports work automatically.")
        # Dedicated-account mode: safe to refresh / auto-login.
        sess = load_session() or {}
        acc = (sess.get("localStorage") or {}).get("kw388_access", "")
        if acc and not force and (_jwt_exp(acc) - time.time()) > 120:
            return acc
        if not KW_ALLOW_AUTO_LOGIN:
            raise RuntimeError("No valid KR883 session, and automatic login/refresh is disabled "
                               "(KW388_ALLOW_AUTO_LOGIN=false). Log in to KR883 in your normal Chrome.")
        a = await api_refresh_token()
        if a:
            return a
        return await api_login_fresh()


async def token_keeper():
    """Background task: proactively refresh the token every 25 min so it never expires mid-scrape,
    and recover automatically after the PC has been off. Rotation means this keeps the session
    alive indefinitely with zero manual steps."""
    if not load_session():
        return
    while True:
        try:
            await ensure_api_token(force=True)
        except Exception as e:
            print("token_keeper: could not renew session yet: " + str(e)[:120], file=sys.stderr)
        await asyncio.sleep(25 * 60)


KW_API_MEMBERS = KW_API_BASE + "/api/members/"
def _due_from(created_iso, tenure_days):
    """Approximate due date = the loan's creation date + tenure_days. The exact due_date lives only
    in the master-scoped /api/loans/ endpoint; this is within ~1 day and covers every customer."""
    try:
        base = datetime.datetime.strptime((created_iso or "")[:10], "%Y-%m-%d").date()
        return (base + datetime.timedelta(days=int(tenure_days))).isoformat()
    except Exception:
        return ""


def _member_pick(results, ic):
    """From a members search result list, the newest record for this IC as a compact dict
    (loan rollup: amount, onHand, tenure, created, event), or None."""
    best = None
    for m in results or []:
        if cic(m.get("ic_number") or "") != cic(ic):
            continue
        if best is None or (m.get("created_at") or "") > (best.get("_c", "") or best.get("created_at") or ""):
            best = m
    if not best:
        return None
    created = best.get("created_at") or ""
    return {"amount": best.get("loan_amount"), "onHand": best.get("on_hand_amount"),
            "tenure": best.get("tenure_days"), "created": (created[:10] if created else ""),
            "event": best.get("event_display") or "", "_c": created}


BACKEND_USER = os.environ.get("BACKEND_ADMIN_USERNAME", "")
BACKEND_PASS = os.environ.get("BACKEND_ADMIN_PASSWORD", "")
BACKEND_PIN = os.environ.get("BACKEND_ADMIN_PIN", "")
API_BASE = os.environ.get("API_INTERNAL_URL", "http://api:4000/api/v1")


def _envflag(name, default=False):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# LOCAL_ONLY: run the scraper with no backend at all. Results are written to OUTPUT_DIR as JSON
# instead of POSTed, and the new/known-IC lookups resolve to "nothing is known yet". Defaults to
# ON whenever no backend admin username is configured, so an unconfigured install is local by
# default rather than failing halfway through a run against a backend that isn't there.
LOCAL_ONLY = _envflag("LOCAL_ONLY", default=not BACKEND_USER)

# Automatic KR883 password/TOTP login (and token refresh) is disabled unless explicitly enabled. The
# UI workflow never uses it: it mirrors the session of the normal Chrome. A second login would
# supersede that session and log the worker out. Set KW388_ALLOW_AUTO_LOGIN=true only for a
# dedicated scraper-only KR883 account.
KW_ALLOW_AUTO_LOGIN = _envflag("KW388_ALLOW_AUTO_LOGIN", default=False)

# When true, the scraper NEVER logs in itself: it only uses the copied session. KR883 allows one
# session per account, so a self-login invalidates the human's browser session ("session
# expired") and triggers a re-login fight that also wrecks throughput. In this mode an expired
# session stops the run with a clear "re-import" message instead of logging in. Defaults on when
# a captured session file exists.
KW_SHARED_SESSION = _envflag("KW388_SHARED_SESSION", default=_shared_session_default())
# Direct-API scrape path (default on when a session with an access token exists).

_here = os.path.dirname(os.path.abspath(__file__))
_data = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else os.path.join(_here, "data"))

CANDIDATES_PATH = os.environ.get(
    "CANDIDATES_PATH", os.path.join(_data, "storage", "kw-candidates", "ctm_numbers_ic.xlsx"))
PROFILE_DIR = os.environ.get("PROFILE_DIR", os.path.join(_data, "kw_profile"))
CACHE_DIR = os.environ.get("CACHE_DIR", os.path.join(_data, "kw_cache"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join(_data, "output"))
PORT = int(os.environ.get("PORT", "8765"))

# ---------------- Google Sheets export ----------------
# Service-account JSON key file, and the target spreadsheet. Give the sheet edit access to the
# service account's client_email or every write 403s.
GSHEETS_CREDENTIALS = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", os.path.join(_here, "google-credentials.json"))
GSHEETS_SPREADSHEET_ID = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID", "")
GSHEETS_WORKSHEET = os.environ.get("GOOGLE_SHEETS_WORKSHEET", "KW388 Leads")
# When set, a finished scrape pushes itself to Google Sheets with no further action.
GSHEETS_AUTO = _envflag("GOOGLE_SHEETS_AUTO_EXPORT", default=False)
# strftime pattern for the auto-export's worksheet name. Defaults to the BLASTER sheet's own
# per-day tab convention ("28/08"), so each run lands in its own dated tab instead of
# overwriting yesterday's.
GSHEETS_TAB_PATTERN = os.environ.get("GOOGLE_SHEETS_TAB_PATTERN", "%d/%m")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 50 * 1024 * 1024))
NAME_COL, IC_COL, PHONE_COL, AMT_COL = 1, 2, 3, 5
BATCH = int(os.environ.get("KW_BATCH", 300))
# Concurrent browser tabs. ~57% of a candidate's wall-clock is spent waiting on KW388's search
# response (measured median 1.85s), during which the tab is idle — so throughput scales with tab
# count far better than with per-candidate tuning. The old default of 6 was sized for a 2-vCPU
# VPS. RAM is the real ceiling: budget ~100MB per tab.
CONC = int(os.environ.get("KW_CONC", 12))
# Matches KW_OVERDUE_SKIP_DAYS in apps/api/src/modules/kwLeads/kwLead.service.ts — a customer
# overdue this long isn't a near-term blast/lend candidate, so re-scraping them wastes time.
OVERDUE_SKIP_DAYS = 30
# The direct API is fast enough now that skipping the 30+d overdue crowd no longer saves meaningful
# time, and the user wants every press to cover the whole uploaded list. Set KW388_SKIP_OVERDUE=true
# to bring the time-saving skip back.
SKIP_OVERDUE = _envflag("KW388_SKIP_OVERDUE", default=False)
MAX_PAGES = 5
# How many times one run may re-authenticate after KW388 drops the session before giving up.
MAX_RELOGINS = int(os.environ.get("KW_MAX_RELOGINS", 3))
KNOWN_IC = "960924085285"  # used only to sanity-check a session is actually live

# RoyalPay — the payment gateway KW388 disburses through. A separate site/login entirely; its
# own loan amount is a different figure from KW388's, kept in a separate backend table (RoyalPayLead).
RP_USER = os.environ.get("ROYALPAY_USERNAME", "")
RP_PASS = os.environ.get("ROYALPAY_PASSWORD", "")
RP_TOTP_SECRET = os.environ.get("ROYALPAY_TOTP_SECRET", "")
RP_BASE = "https://merchant.royalpay.biz"
RP_PROFILE_DIR = os.environ.get("RP_PROFILE_DIR", "/data/rp_profile")
RP_KW_ENRICH_PROFILE_DIR = PROFILE_DIR + "_rpenrich"  # separate KW388 session — never collides
                                                        # with the main scan or Check Now's profile

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(os.path.dirname(CANDIDATES_PATH), exist_ok=True)

# ---------------- time (always Malaysia time, regardless of the container/host clock) ----------------
# The container's system clock is UTC (confirmed — TZ=Asia/Kuala_Lumpur alone isn't enough without
# tzdata actually installed). Every "today" concept here — todayCount, the overdue-streak walk,
# the cache file's day boundary, RoyalPay's date-range query — must mean Malaysia's calendar day,
# not UTC's, or there's an 8-hour window (00:00-08:00 MYT) every single day where "today" is
# silently off by one. Compute it explicitly instead of trusting the OS timezone.
MYT_OFFSET = datetime.timedelta(hours=8)

def now_myt():
    return datetime.datetime.utcnow() + MYT_OFFSET

def today_myt():
    return now_myt().date()

# ---------------- TOTP (RFC 6238) ----------------
def totp(secret_b32, digits=6, period=30, algo="sha1", t=None):
    key = base64.b32decode(secret_b32.upper() + "=" * ((8 - len(secret_b32) % 8) % 8))
    counter = int((t if t is not None else time.time()) // period)
    h = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, algo)).digest()
    offset = h[-1] & 0x0F
    code = (struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)

# ---------------- helpers (ported from the local KW388-Daily-Filter/daily.py) ----------------
def raw_digits(v):
    return re.sub(r"\D", "", str(v or "").split(".")[0])

def cic(v):
    d = raw_digits(v)
    if 9 <= len(d) <= 12: d = d.zfill(12)
    return d if len(d) == 12 else ""

def pdt(d, t):
    try: return datetime.datetime.strptime(f"{d} {t}", "%d %b %Y %H:%M")
    except Exception: return None

def money(s):
    m = re.search(r"([\d,]+\.\d{2}|[\d,]+)", str(s))
    return float(m.group(1).replace(",", "")) if m else None

def num(s):
    d = re.sub(r"\D", "", str(s)); return int(d) if d else 0

def statv(st, *keys):
    for k, v in st.items():
        if all(x in k.lower() for x in keys): return v
    return ""

def recent5(loans, txs):
    """The 5 most recently COMPLETED loans, each paired with a repayment amount — the 'Recent 5
    (days/RM)' signal agents use to judge how fast/reliably someone repays.

    Previously this grouped by the per-loan remark code (e.g. "IO") and matched rank-within-code,
    on the assumption the code was a stable per-loan identifier. In practice many customers have
    dozens of loans sharing the SAME generic code, so completed-loan-count and repayment-count
    within a code group drift apart and most pairings failed a same-code/date sanity check,
    surfacing as "?" for nearly everyone. A "Completed" loan is BY DEFINITION fully repaid, so
    the Nth most recent completed loan and the Nth most recent Loan Repayment transaction are
    overwhelmingly the same event — pairing purely by recency rank, with no code matching, is
    simpler and actually matches reality better."""
    completed = []
    for x in loans:
        c = x.get("c") or []
        if len(c) >= 1 and c[0] == "Completed":
            dt = pdt(x["date"], x["time"])
            if dt: completed.append(dt)
    completed.sort(reverse=True)

    repayments = []
    for x in txs:
        c = x.get("c") or []
        if len(c) >= 1 and c[0] == "Loan Repayment":
            dt = pdt(x["date"], x["time"])
            if dt: repayments.append((dt, money(c[1]) if len(c) > 1 else None))
    repayments.sort(key=lambda r: r[0], reverse=True)

    out = []
    for i, bdt in enumerate(completed[:5]):
        if i < len(repayments) and repayments[i][0].date() >= bdt.date():
            rdt, amt = repayments[i]
            out.append({"borrow": bdt, "repay": rdt, "days": (rdt.date() - bdt.date()).days + 1, "amt": amt})
        else:
            out.append({"borrow": bdt, "repay": None, "days": None, "amt": None})
    return out

def median_days(last5):
    ds = sorted(o["days"] for o in last5 if o.get("days"))
    if not ds: return None
    n = len(ds); mid = n // 2
    return ds[mid] if n % 2 else round((ds[mid - 1] + ds[mid]) / 2)

ACTIVE_LOAN_STATUSES = {"Disbursed", "Approved"}  # Approved = money not sent yet, but it's still
                                                    # a loan the customer is about to be carrying

def loans_by_date(loans):
    """How many loans are currently Disbursed OR Approved (money out, or about to be — an
    Approved loan is still debt the customer is taking on even before the transfer clears) on
    each date, across the scraped Loan-details pages — a borrow-velocity signal for STILL-ACTIVE
    loans. Completed (already repaid), Rejected, and not-yet-approved statuses (Under
    review/Pending) are excluded — a fully repaid loan isn't a risk signal. Newest date first."""
    counts = defaultdict(int)
    for x in loans:
        c = x.get("c") or []
        if len(c) < 2 or c[1] not in ACTIVE_LOAN_STATUSES:
            continue
        d = x.get("date")
        if d: counts[d] += 1
    def sort_key(d):
        try: return datetime.datetime.strptime(d, "%d %b %Y")
        except Exception: return datetime.datetime.min
    ordered = sorted(counts.items(), key=lambda kv: sort_key(kv[0]), reverse=True)
    # kwLeadDisbursedByDateEntrySchema caps this at 10 — a very active borrower can have more
    # distinct dates than that, and one oversized row fails validation for its whole upload
    # chunk. Keep the newest 10; that's already what agents care about for borrow-velocity.
    return [{"date": d, "count": c} for d, c in ordered[:10]]

def loans_by_remark(loans):
    """How many currently open (Disbursed/Approved) loans this customer has per remark/channel
    code — the 4th Loan-details column (e.g. "IO", "SK (i)") identifying which app or agent the
    loan came through. Same open-loan definition as loans_by_date (money out or about to be),
    just grouped by code instead of date — which lender/channel they're juggling debt with right
    now, biggest count first."""
    counts = defaultdict(int)
    for x in loans:
        c = x.get("c") or []
        if len(c) < 4 or c[1] not in ACTIVE_LOAN_STATUSES:
            continue
        code = c[3]
        if code: counts[code] += 1
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    # Same cap rationale as loans_by_date — kwLeadDisbursedByRemarkEntrySchema enforces this.
    return [{"code": code, "count": n} for code, n in ordered[:20]]

def overdue_streak_days(txs):
    """How many CONSECUTIVE days (ending today) this customer has had a Late Penalty with no
    Loan Repayment — the chronic-delinquency severity behind the overdue flag. A single day with
    a penalty and no repayment is a mild "missed it today"; ten days straight is a customer who
    has been overdue for a week and a half and should not be blasted a new loan offer. A
    Repayment on any day breaks the streak; a day with neither logged also breaks it (KW388
    charges the penalty daily while a loan stays overdue, so silence means it wasn't overdue
    that day)."""
    penalty_dates, repay_dates = set(), set()
    for x in txs:
        c = x.get("c") or []
        d = x.get("date")
        if not c or not d:
            continue
        if c[0] == "Late Penalty":
            penalty_dates.add(d)
        elif c[0] == "Loan Repayment":
            repay_dates.add(d)
    streak = 0
    d = today_myt()
    while True:
        ds = d.strftime("%d %b %Y")
        if ds in penalty_dates and ds not in repay_dates:
            streak += 1
            d -= datetime.timedelta(days=1)
        else:
            break
    return streak

def kw388_signals(loans, txs, st):
    """The KW388-derived fields shared by every consumer of a scraped credit-report page: the
    main candidate scan, Check Now, and RoyalPay's per-customer enrichment lookup."""
    last5 = recent5(loans, txs)
    overdue_days = overdue_streak_days(txs)
    today_active = sum(
        1 for x in loans
        if x.get("date") == today_str() and len((x.get("c") or [])) >= 2 and x["c"][1] in ACTIVE_LOAN_STATUSES
    )
    return {
        # Only Disbursed/Approved count as "took a loan today" — same filter as loans_by_date.
        # A Rejected or still-Under-review application dated today isn't an actual loan.
        "todayCount": str(today_active),
        "totalCompleted": str(num(statv(st, "completed"))),
        "totalDisbursed": str(num(statv(st, "disbursed"))),
        "recentDetail": [{"days": o["days"], "amt": o["amt"]} for o in last5],
        "disbursedByDate": loans_by_date(loans),
        "disbursedByRemark": loans_by_remark(loans),
        "overdueToday": overdue_days > 0,
        "overdueDays": overdue_days,
        # KR883's own "Overdue days" from the summary card - captured for free on EVERY scrape,
        # including fast mode where the transaction-log streak (overdue_days) isn't computed.
        # This is the reliable signal for the 30-day re-scrape skip.
        "cardOverdueDays": num(statv(st, "overdue", "days")),
    }

CANDIDATES_DIR = os.path.dirname(CANDIDATES_PATH)
CANDIDATES_MANIFEST_PATH = os.path.join(CANDIDATES_DIR, "manifest.json")
STORAGE_ROOT = os.path.dirname(CANDIDATES_DIR)

def candidate_files():
    """Which xlsx files make up the current candidate pool. A multi-file upload writes a
    manifest (newest-uploaded file first, for tie-break priority below); before the first such
    upload, or if the manifest is ever missing, fall back to the single legacy path so an
    already-uploaded list keeps working."""
    if os.path.exists(CANDIDATES_MANIFEST_PATH):
        manifest = read_manifest()
        # Every uploaded list is kept forever; "active" is what decides whether it feeds the
        # current candidate pool. Files predating this flag have no "active" key and default to
        # True, so an existing install keeps behaving exactly as before.
        active = [f for f in manifest.get("files", []) if f.get("active", True)]
        paths = [os.path.join(STORAGE_ROOT, f["key"]) for f in reversed(active)]
        paths = [p for p in paths if os.path.exists(p)]
        if paths:
            return paths
        if manifest.get("files"):
            return []  # lists exist but all are switched off — an empty pool, not a missing one
    return [CANDIDATES_PATH] if os.path.exists(CANDIDATES_PATH) else []

_CTM_CACHE = {"key": None, "val": None}


def load_ctm():
    # Sheet names that are plain numbers (e.g. "307") are treated as dated snapshots, newest
    # first, so a later re-export's phone/amount wins on a shared IC. Any other name (a plain
    # "Sheet1" single-sheet export, most commonly) falls back to (0, 0) — just processed in
    # whatever order the workbook lists them, which is fine when there's nothing to prioritize.
    # Files are processed newest-uploaded first (see candidate_files), same priority rule
    # extended across files as within one file's tabs.
    #
    # Parsing this 2MB+, 10k-row workbook with openpyxl costs ~2.5s of pure CPU and is fully
    # synchronous, so calling it inside an async request handler BLOCKS the event loop for that
    # whole time — which is what made /credit-report take ~20s under concurrency (every request
    # re-parsed the file). The candidate list only changes on a new upload, so cache the parsed
    # result keyed on each file's (path, mtime, size) and rebuild only when that changes.
    try:
        key = tuple((p, os.path.getmtime(p), os.path.getsize(p)) for p in candidate_files())
    except OSError:
        key = None
    if key is not None and _CTM_CACHE["key"] == key:
        return _CTM_CACHE["val"]

    def tabkey(s):
        try: return (int(s[-1]), int(s[:-1]))
        except Exception: return (0, 0)
    phones, amts, seen, cand = {}, {}, set(), []
    for path in candidate_files():
        wb = openpyxl.load_workbook(path, data_only=True)
        sheets = sorted(wb.sheetnames, key=tabkey, reverse=True)
        for sh in sheets:
            ws = wb[sh]
            for i in range(1, ws.max_row + 1):
                ic = cic(ws.cell(i, IC_COL).value); nm = ws.cell(i, NAME_COL).value
                if len(ic) != 12 or not nm: continue
                if ic not in phones:
                    d = re.sub(r"\D", "", str(ws.cell(i, PHONE_COL).value or "").split(".")[0])
                    phones[ic] = ("+" + d) if d.startswith("60") else d
                    try: amts[ic] = float(ws.cell(i, AMT_COL).value)
                    except Exception: amts[ic] = ""
                if ic not in seen:
                    seen.add(ic); cand.append([str(nm).strip(), ic])
    _CTM_CACHE["key"], _CTM_CACHE["val"] = key, (phones, amts, cand)
    return phones, amts, cand

ROW_JS = """table => Array.from(table.querySelectorAll('tbody tr')).map(tr => {
  const tds=Array.from(tr.querySelectorAll('td'));
  const cell=td=>{const p=td.querySelector('.pill,.ident-tag,.muted-dash');return (p?p.innerText:td.innerText).trim();};
  return {date:((tr.querySelector('.tcell-date')||{}).innerText||'').trim(),
          time:((tr.querySelector('.tcell-time')||{}).innerText||'').trim(), c:tds.slice(1).map(cell)};})"""
STATS_JS = """() => Object.fromEntries(
  Array.from(document.querySelectorAll('.sum-stat')).map(e => {
    const l = e.querySelector('.sum-label'), v = e.querySelector('.sum-val');
    return [l ? l.innerText.trim() : '', v ? v.innerText.trim() : ''];
  }).filter(p => p[0]))"""
def sig_of(pr): return tuple((r["date"], r["time"], tuple(r["c"])) for r in pr)
def dash(ic): return f"{ic[:6]}-{ic[6:8]}-{ic[8:12]}" if len(ic) == 12 else ic

ROW_COUNT_JS = "t => t.querySelectorAll('tbody tr').length"


async def _row_count(t):
    try:
        return await t.evaluate(ROW_COUNT_JS)
    except Exception:
        return -1


async def wait_table_settled(t, baseline, timeout_ms=8000, idle_ms=150, grace_ms=900, poll_ms=40):
    """Wait until a table's row count stops changing, instead of sleeping a flat 1.5s.

    Measured on this account: a 267-row Loan details table and a 1161-row Transaction log both
    settled well inside the old fixed sleep, so most of that 1.5s was dead time — but a genuinely
    huge table now gets up to `timeout_ms` rather than being cut off at 1.5s, so this is both
    faster on average AND safer on the outliers.

    `grace_ms` bounds the case where the count never changes (the table already showed everything),
    so a small record doesn't pay the full timeout waiting for a change that will never come."""
    loop = asyncio.get_event_loop()
    start = loop.time()
    last, changed, stable_at = baseline, False, None
    while (loop.time() - start) * 1000 < timeout_ms:
        n = await _row_count(t)
        if n != last:
            last, changed, stable_at = n, True, None
        elif changed:
            if stable_at is None:
                stable_at = loop.time()
            elif (loop.time() - stable_at) * 1000 >= idle_ms:
                return last
        elif (loop.time() - start) * 1000 >= grace_ms:
            return last
        await asyncio.sleep(poll_ms / 1000)
    return last


async def select_all_page_size(page, card):
    """KW388's chevron_right pagination silently self-disables after ~2 pages on
    high-activity ICs, truncating Loan details/Transaction log well before the
    card's own header count (e.g. stops at 18 rows when the header says 88).
    Switching the 'Records per page' selector to 'All' loads the full table in
    one shot and avoids that bug entirely. Returns True on success."""
    dd = card.locator(".q-table__bottom .q-select, .q-table__bottom [role=combobox]").first
    if not await dd.count(): return False
    t = card.locator("table.q-table").first
    baseline = await _row_count(t) if await t.count() else 0
    try: await dd.click(timeout=3000)
    except Exception: return False
    # Wait for the menu to actually render rather than assuming 300ms is enough.
    try:
        await page.wait_for_selector(".q-menu .q-item", timeout=3000)
    except Exception:
        return False
    opts = page.locator(".q-menu .q-item__label, .q-menu .q-item")
    # One round trip for every label, instead of one per option.
    try:
        labels = await opts.evaluate_all("els => els.map(e => (e.innerText || '').trim().toLowerCase())")
    except Exception:
        labels = []
    target = labels.index("all") if "all" in labels else None
    if target is None:
        try: await page.keyboard.press("Escape")
        except Exception: pass
        return False
    try: await opts.nth(target).click(timeout=3000)
    except Exception: return False
    await wait_table_settled(t, baseline)
    return True

async def scrape_table(page, card, maxp):
    t = card.locator("table.q-table").first
    if not await t.count(): return []
    if await select_all_page_size(page, card):
        return await t.evaluate(ROW_JS)
    rows, seen = [], set()
    for _ in range(maxp):
        if not await t.count(): break
        pr = await t.evaluate(ROW_JS); s = sig_of(pr)
        if s in seen: break
        seen.add(s); rows += pr
        nxt = card.locator("button").filter(has_text="chevron_right")
        if not await nxt.count() or await nxt.last.is_disabled(): break
        try: await nxt.last.click(timeout=3000)
        except Exception: break
        for _w in range(20):
            await page.wait_for_timeout(250)
            if sig_of(await t.evaluate(ROW_JS)) != s: break
        else: break
    return rows

async def scrape_table_first_page(page, card):
    """Whatever the Loan details table shows before any pagination/select-all — KW388 renders it
    newest-first, so this is enough to answer "did they take a loan today", without paying for
    the expensive select_all_page_size() + full Transaction log walk. Used only by fast mode's
    cheap today-check; a full scrape still calls scrape_table() for the complete history."""
    t = card.locator("table.q-table").first
    if not await t.count(): return []
    return await t.evaluate(ROW_JS)

async def scrape_customer(page, ic, fast=False):
    await page.goto(f"{KW_BASE_URL}/reports/credit", wait_until="domcontentloaded", timeout=60000)
    if "login" in page.url: return {"session": False}
    box = page.get_by_placeholder("Enter IC number", exact=False)
    await box.wait_for(state="visible", timeout=15000); await box.fill(dash(ic))
    try: await page.get_by_role("button", name="Search").first.click(timeout=5000)
    except Exception: await box.press("Enter")
    # Returns the moment results render. The old 500ms polling loop kept the same 9s ceiling but
    # rounded every hit up to the next 500ms tick; the real median response is ~1.85s.
    try:
        await page.wait_for_selector(".sum-name", timeout=9000)
    except Exception:
        return {"found": False}
    # The stat cards render with (or just after) .sum-name — wait for them specifically rather
    # than sleeping a flat 400ms and hoping.
    try:
        await page.wait_for_selector(".sum-stat", timeout=3000)
    except Exception:
        pass
    # One evaluate for every stat card. The per-locator version cost two CDP round trips per
    # card (~19ms total); this is ~1ms and returns identical values.
    stats = await page.evaluate(STATS_JS)
    lc = page.locator(".ccard").filter(has=page.get_by_text("Loan details", exact=True)).first
    tc = page.locator(".ccard").filter(has=page.get_by_text("Transaction log", exact=True)).first

    if fast:
        # Cheap check: does today's date show up on the Loan details table's default (unpaginated)
        # view? If not, skip the expensive full loan+transaction scrape entirely for this
        # candidate — stats (totalCompleted/totalDisbursed) are already captured above for free,
        # so only recentDetail/disbursedByDate/period/overdueDays get left stale (see
        # all_scraped_rows/to_lead_payload's "partial" row, which the API only applies as a
        # partial update so it never clobbers those fields with blanks).
        first_page = await scrape_table_first_page(page, lc) if await lc.count() else []
        ts = today_str()
        has_loan_today = any(
            x.get("date") == ts and len((x.get("c") or [])) >= 2 and x["c"][1] in ACTIVE_LOAN_STATUSES
            for x in first_page
        )
        if not has_loan_today:
            return {"found": True, "stats": stats, "skipped": True}

    loans = await scrape_table(page, lc, MAX_PAGES) if await lc.count() else []
    txs = await scrape_table(page, tc, MAX_PAGES) if await tc.count() else []
    return {"found": True, "stats": stats, "loans": loans, "txs": txs}

async def scrape_batch(ctx, batch, on_done=None, fast=False):
    q = asyncio.Queue()
    for x in batch: q.put_nowait(x)
    out, expired = [], [False]
    async def worker():
        page = await ctx.new_page()
        while True:
            try: name, ic = q.get_nowait()
            except Exception: break
            rec = None
            try:
                data = await scrape_customer(page, ic, fast=fast)
                if data.get("session") is False: expired[0] = True; break
                rec = {"name": name, "ic": ic, **data}
                out.append(rec)
            except Exception as e:
                rec = {"name": name, "ic": ic, "found": "err", "err": str(e)[:80]}
                out.append(rec)
            if on_done:
                # Hand the finished record over so results can surface live, rather than only
                # when the whole batch flushes to disk.
                await on_done(rec)
        await page.close()
    await asyncio.gather(*[worker() for _ in range(CONC)])
    return out, expired[0]

def today_str():
    return today_myt().strftime("%d %b %Y")

def all_scraped_rows(raw, phones, amts):
    """Everyone actually found on KW388 — not just people with a loan dated today. We already
    visit every candidate to check for today's activity, so keep the full picture instead of
    throwing away everyone who didn't happen to borrow today; `today_n` is just 0 for them.

    A fast-mode `skipped` rec never fetched the paginated Loan details/Transaction log tables (no
    loan today on the cheap first-page check), so recentDetail/disbursedByDate/disbursedByRemark/
    period/overdueDays can't be computed — `partial: True` tells to_lead_payload/runImport to
    leave those columns alone rather than overwrite real history with blanks.
    totalCompleted/totalDisbursed still come from the summary stat cards, which are free
    regardless of mode."""
    ts = today_str()
    rows = []
    for rec in raw:
        if rec.get("found") is not True: continue
        ic = cic(rec.get("ic"))
        if rec.get("skipped"):
            st = rec.get("stats", {}) or {}
            rows.append({"name": rec.get("name"), "ic": ic, "phone": phones.get(ic, ""), "amt": amts.get(ic, ""),
                         "today_n": 0,
                         "comp": num(statv(st, "completed")), "disb": num(statv(st, "disbursed")),
                         "partial": True})
            continue
        loans = rec.get("loans", []) or []; txs = rec.get("txs", []) or []; st = rec.get("stats", {}) or {}
        # Only Disbursed/Approved count as "took a loan today" — same filter as loans_by_date.
        # A Rejected or still-Under-review application dated today isn't an actual loan.
        today_n = sum(
            1 for x in loans
            if x.get("date") == ts and len((x.get("c") or [])) >= 2 and x["c"][1] in ACTIVE_LOAN_STATUSES
        )
        # Signals for the "filtered scrape" quality filter:
        #  - hasRecentRepayment: any Loan Repayment on the transaction log's first page (10 most
        #    recent). Distinguishes a truly delinquent customer from one whose late-penalty rows
        #    are the admin data-entry bug - a customer still being relent-to keeps repaying.
        #  - recentLoanRejected: the most recent loan on Loan details is Rejected.
        first_page_txs = txs[:10]
        has_recent_repay = any((x.get("c") or [""])[0] == "Loan Repayment" for x in first_page_txs)
        recent_rejected = bool(loans and len(loans[0].get("c") or []) >= 2 and loans[0]["c"][1] == "Rejected")
        rows.append({"name": rec.get("name"), "ic": ic, "phone": phones.get(ic, ""), "amt": amts.get(ic, ""),
                     "today_n": today_n,
                     "comp": num(statv(st, "completed")), "disb": num(statv(st, "disbursed")),
                     "last5": recent5(loans, txs), "byDate": loans_by_date(loans),
                     "byRemark": loans_by_remark(loans),
                     "overdueDays": overdue_streak_days(txs),
                     "hasRecentRepayment": has_recent_repay,
                     "recentLoanRejected": recent_rejected,
                     "lastPayment": rec.get("lastPayment"),
                     "lastDisbursed": rec.get("lastDisbursed"),
                     "lastLoanCreated": rec.get("lastLoanCreated"),
                     "member": rec.get("member"),
                     "partial": False})
    return rows

HEAVY_RESOURCE_TYPES = {"image", "font", "media"}

async def open_browser(p, profile_dir):
    """Return (context, close_fn).

    Attached mode (KW_CDP_URL) reuses the user's own Chrome: their login is our login. The
    close_fn must then close only the pages we opened - closing the context would shut down
    their browser mid-browse."""
    if KW_CDP_URL:
        browser = await p.chromium.connect_over_cdp(KW_CDP_URL)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        ctx._kw_attached = True
        ctx._kw_pages = []

        async def close():
            for pg in list(getattr(ctx, "_kw_pages", [])):
                try:
                    await pg.close()
                except Exception:
                    pass
            # Deliberately no browser.close(): that would terminate the user's Chrome.
        return ctx, close

    sess = load_session()
    launch = dict(headless=True, viewport={"width": 1400, "height": 1000}, args=STEALTH_ARGS)
    if sess and sess.get("userAgent"):
        launch["user_agent"] = sess["userAgent"]   # MUST match what cf_clearance was issued to
    if KW_BROWSER_CHANNEL:
        launch["channel"] = KW_BROWSER_CHANNEL
    try:
        ctx = await p.chromium.launch_persistent_context(profile_dir, **launch)
    except Exception:
        # The real-Chrome channel may not be installed where expected; fall back to Chromium.
        launch.pop("channel", None)
        ctx = await p.chromium.launch_persistent_context(profile_dir, **launch)
    ctx._kw_attached = False
    if sess and sess.get("cookies"):
        try:
            await ctx.add_cookies(sess["cookies"])
        except Exception:
            pass
    # KR883 keeps its JWT auth in localStorage, not cookies. Inject the captured tokens at
    # document-start on the KR883 origin so the SPA boots already authenticated - no login.
    ls = (sess or {}).get("localStorage") or {}
    if ls:
        try:
            await ctx.add_init_script(
                "(() => { try { if (location.hostname && location.hostname.indexOf('kr883') !== -1) {"
                " const s = " + json.dumps(ls) + ";"
                " for (const k in s) { try { localStorage.setItem(k, s[k]); } catch(e){} } } } catch(e){} })();")
        except Exception:
            pass
    return ctx, ctx.close


async def acquire_page(ctx):
    """A page we own. Attached to someone's live browser we must never navigate a tab they are
    using, so always open our own and remember it so close() can clean it up."""
    if getattr(ctx, "_kw_attached", False):
        page = await ctx.new_page()
        ctx._kw_pages.append(page)
        await block_heavy_resources(page)   # per-page: never throttle their other tabs
        return page
    return ctx.pages[0] if ctx.pages else await ctx.new_page()


async def block_heavy_resources(ctx):
    """We only ever read text out of a table — images/fonts/video are pure CPU+network overhead
    per page. The VPS is CPU-bound at the current concurrency (2 vCPUs, already ~190% used), so
    cutting this waste speeds up every page without needing more tabs or a bigger box."""
    # Accepts a BrowserContext or a Page - both expose .route(). When attached to the user's
    # Chrome we route per-page only, so their normal browsing keeps its images and fonts.
    if getattr(ctx, "_kw_attached", False):
        return
    await ctx.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in HEAVY_RESOURCE_TYPES
        else route.continue_(),
    )

# ---------------- login (TOTP-automated, no human) ----------------
async def _attempt_login(page):
    """One full pass: reload the login page, submit credentials, enter TOTP. Returns True on
    success. KW388 occasionally leaves the OTP box disabled for several seconds (still rendering
    /validating the previous step) — that's caught per-attempt below so it doesn't blow up the
    whole login; the outer ensure_logged_in loop gives the page a clean reload if both TOTP
    attempts here still find it stuck."""
    await page.goto(f"{KW_BASE_URL}/", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(1500)
    if "login" not in page.url:
        return True

    await page.fill("input[type=text]", KW_USER)
    await page.fill("input[type=password]", KW_PASS)
    await page.click("button[type=submit]")
    await page.wait_for_timeout(2000)

    for attempt in range(2):  # a code can straddle a 30s boundary — retry once with a fresh one
        if "login" not in page.url:
            break
        code_box = page.locator("input[type=text]")
        if not await code_box.count():
            break
        try:
            await code_box.wait_for(state="visible", timeout=15000)
            await code_box.click(timeout=15000)
            await code_box.fill("")
            await code_box.press_sequentially(totp(KW_TOTP_SECRET), delay=60)
            await page.wait_for_timeout(1500)
            btn = page.get_by_role("button", name="Verify")
            if await btn.is_enabled():
                await btn.click(timeout=3000)
        except Exception:
            pass  # OTP box was slow/disabled/unclickable this round — next attempt gets a fresh code
        await page.wait_for_timeout(2000)

    return "login" not in page.url

async def _is_cloudflare_block(page):
    """Cloudflare's block/challenge page instead of the app. Detected by title/body markers so we
    can fail fast with a useful message rather than timing out on every candidate."""
    try:
        title = (await page.title()) or ""
        if "Attention Required" in title or "Just a moment" in title:
            return True
        body = (await page.inner_text("body"))[:400].lower()
        return ("sorry, you have been blocked" in body
                or "cloudflare" in body and "blocked" in body
                or "verify you are human" in body)
    except Exception:
        return False


async def ensure_logged_in(ctx):
    missing = [n for n, v in (("KW388_USERNAME", KW_USER), ("KW388_PASSWORD", KW_PASS),
                              ("KW388_TOTP_SECRET", KW_TOTP_SECRET)) if not v]
    if missing:
        raise RuntimeError(
            "KW388 credentials are not configured — set %s in .env before scraping" % ", ".join(missing))
    page = await acquire_page(ctx)
    try:
        r = await scrape_customer(page, KNOWN_IC)
        if r.get("found") is True:
            return  # session already valid
    except Exception:
        pass

    # If we're staring at a Cloudflare wall, no amount of username/TOTP retrying helps - and a
    # credential login would just burn the account's one session. Stop with the real fix.
    if await _is_cloudflare_block(page):
        raise RuntimeError(
            "Blocked by Cloudflare (KR883 bot protection) - is the VPN connected? If so, the "
            "saved session/cf_clearance may have expired; re-run tools/import_chrome_session.py.")

    # Shared-session mode: never self-login. Doing so would log the human out of KR883 (one
    # session per account). The copied session must already be valid; if it isn't, stop and ask
    # for a refresh rather than stealing the session.
    if KW_SHARED_SESSION:
        raise RuntimeError(
            "The shared KR883 session has expired, and the scraper is set NOT to log in on its "
            "own (so it never logs you out). Log in to KR883 in Chrome, then re-run "
            "tools/import_chrome_session.py to refresh the session, then scrape again.")

    last_err = None
    for outer_attempt in range(3):  # a stuck/glitched login page gets a full clean reload + retry
        try:
            if await _attempt_login(page):
                return
        except Exception as e:
            last_err = e
        await page.wait_for_timeout(2000)

    try:
        await page.screenshot(path=os.path.join(CACHE_DIR, "login_failure.png"))
    except Exception:
        pass
    raise RuntimeError(f"KW388 login failed after 3 attempts (bad password/TOTP secret, or a KW388 UI issue): {last_err}")

# ---------------- run state ----------------
STATE_LOCK = asyncio.Lock()
STATE = {"status": "idle", "stage": None, "lastLine": "", "progress": None, "result": None,
         "error": None, "startedAt": None, "finishedAt": None}

async def set_state(**kw):
    async with STATE_LOCK:
        STATE.update(kw)

async def get_state():
    async with STATE_LOCK:
        return dict(STATE)

def notify_desktop(title, message):
    """Fire a Windows toast. Best-effort and fully detached - a notification failure must never
    affect a scrape. Off by KW388_NOTIFY=false."""
    if not _envflag("KW388_NOTIFY", default=True):
        return
    try:
        import subprocess
        ps1 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "notify.ps1")
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1, title, message[:200]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def now_iso():
    # Naive (no tzinfo) MYT wall-clock time — the frontend's `new Date(...)` parses a
    # timezone-less string as local browser time, and agents' browsers are in Malaysia, so this
    # displays correctly without needing an explicit offset marker.
    return now_myt().isoformat()

IMPORT_CHUNK = 1000  # kwLeadImportRequestSchema caps a single request at 2000 rows — stay well under it

# Rows from the run currently in progress, appended per candidate as each finishes so the UI can
# show data live. Reset at the start of each run; kept afterwards so the table stays populated.
LIVE_ROWS = []

def _write_local_results(rows, filename):
    """LOCAL_ONLY sink: merge rows into a per-day JSON file under OUTPUT_DIR, keyed by IC so a
    re-scrape of the same person overwrites rather than duplicating. Mirrors what the backend's
    import does (imported vs updated) so run totals stay meaningful with no backend attached."""
    path = os.path.join(OUTPUT_DIR, filename)
    existing = {}
    if os.path.exists(path):
        try:
            for r in json.load(open(path, encoding="utf-8")):
                key = r.get("icNumber") or r.get("ic") or r.get("fullName")
                if key:
                    existing[key] = r
        except Exception:
            existing = {}  # corrupt/partial file from a killed run — rebuild rather than abort

    totals = {"imported": 0, "updated": 0, "invalid": 0}
    for r in rows:
        key = r.get("icNumber") or r.get("ic") or r.get("fullName")
        if not key:
            totals["invalid"] += 1
            continue
        totals["updated" if key in existing else "imported"] += 1
        existing[key] = {**existing.get(key, {}), **r, "scrapedAt": now_iso()}

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(existing.values()), f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)  # atomic — a crash mid-write never leaves a truncated results file
    return totals


async def upload_to_backend(rows):
    if LOCAL_ONLY:
        return _write_local_results(rows, f"kw_leads_{today_myt().isoformat()}.json")
    # Always fully read each response body before the `async with` exits — leaving it unread
    # can keep the connection out of aiohttp's keep-alive pool in a bad state, corrupting the
    # NEXT request on this same session (symptom: login succeeds, the very next call 401s).
    timeout = ClientTimeout(total=120)
    async with ClientSession(timeout=timeout) as s:
        async with s.post(f"{API_BASE}/auth/login", json={"username": BACKEND_USER, "password": BACKEND_PASS}) as r:
            body = await r.text()
            if r.status != 200:
                raise RuntimeError(f"Backend login failed ({r.status}): {body[:200]}")
        async with s.post(f"{API_BASE}/portal/pin/unlock", json={"pin": BACKEND_PIN}) as r:
            body = await r.text()
            if r.status != 200:
                raise RuntimeError(f"Backend PIN unlock failed ({r.status}): {body[:200]}")

        totals = {"imported": 0, "updated": 0, "invalid": 0}
        for i in range(0, len(rows), IMPORT_CHUNK):
            chunk = rows[i:i + IMPORT_CHUNK]
            async with s.post(f"{API_BASE}/kw-leads/import", json={"rows": chunk}) as r:
                body = await r.json()
                if r.status != 200:
                    raise RuntimeError(f"Backend import failed ({r.status}) on rows {i}-{i+len(chunk)}: {str(body)[:200]}")
                data = body.get("data", body)
                for k in totals:
                    totals[k] += data.get(k, 0)
        return totals

def to_lead_payload(r):
    if r.get("partial"):
        # Fast-mode skip: only fields that were actually (cheaply) checked go in the payload —
        # the API's runImport leaves everything else (period/recentDetail/disbursedByDate/
        # overdueDays/overdueToday) untouched on the existing row instead of blanking it out.
        return {
            "fullName": r["name"], "icNumber": r["ic"], "phone": r["phone"],
            "loanAmount": str(r["amt"]) if r["amt"] not in (None, "") else "",
            "todayCount": str(r["today_n"]),
            "totalCompleted": str(r["comp"]), "totalDisbursed": str(r["disb"]),
            "partial": True,
        }
    # Member data (loan amount/tenure/due) from the row (persisted on the raw record).
    _m = r.get("member") or {}
    return {
        "fullName": r["name"], "icNumber": r["ic"], "phone": r["phone"],
        "loanAmount": str(r["amt"]) if r["amt"] not in (None, "") else "",
        "period": str(median_days(r["last5"]) or ""),
        "todayCount": str(r["today_n"]),
        "totalCompleted": str(r["comp"]), "totalDisbursed": str(r["disb"]),
        "recentDetail": [{"days": o["days"], "amt": o["amt"]} for o in r["last5"]],
        "disbursedByDate": r["byDate"],
        "disbursedByRemark": r["byRemark"],
        "overdueToday": r["overdueDays"] > 0,
        "overdueDays": r["overdueDays"],
        "hasRecentRepayment": r.get("hasRecentRepayment"),
        "recentLoanRejected": r.get("recentLoanRejected"),
        # Who processed this customer's most recent repayment, and when/how much. Empty when the
        # customer has no repayment on record. Lets the CRM see "who paid which admin" straight
        # from the bulk scrape, no per-customer lookup needed.
        "lastPaymentAdmin": (r.get("lastPayment") or {}).get("admin", ""),
        "lastPaymentDate": (r.get("lastPayment") or {}).get("date", ""),
        "lastPaymentAmount": (r.get("lastPayment") or {}).get("amount", ""),
        # Who disbursed the customer's most recent loan: a named admin login, or a platform/channel
        # code (FG/TN/DM...) when no named admin processed it. lastDisbursedByType says which.
        # The API exposes no disbursed amount (see api_report_to_shape) - loanAmount above is the
        # amount from the uploaded candidate list, the nearest available figure.
        "lastDisbursedBy": (r.get("lastDisbursed") or {}).get("by", ""),
        "lastDisbursedByType": (r.get("lastDisbursed") or {}).get("type", ""),
        "lastDisbursedDate": (r.get("lastDisbursed") or {}).get("date", ""),
        "lastDisbursedRemark": (r.get("lastDisbursed") or {}).get("remark", ""),
        # Creation date of the customer's most recent loan (any status), from the credit-report API.
        "lastLoanCreated": r.get("lastLoanCreated") or "",
        # Amount / tenure / due date from the members list (covers all customers; the credit-report
        # API has none of these). loanAmountActual is the real loan principal; dueDate is computed
        # as the member record's created date + tenure_days (approximate, no master login needed).
        "loanAmountActual": _m.get("amount", ""),
        "onHandAmount": _m.get("onHand", ""),
        "tenureDays": _m.get("tenure", ""),
        "dueDate": _due_from(_m.get("created", ""), _m.get("tenure", "")),
        "loanEvent": _m.get("event", ""),
        "partial": False,
    }

def add_totals(totals, part):
    for k in totals:
        totals[k] += part.get(k, 0)

# ---------------- credit-report providers + the UI-mode pipeline ----------------
from credit_report_provider import (UiCreditReportProvider, ReauthRequired,
                                    MANUAL_REAUTH_REQUIRED)
from session_sync import SessionSync
import audit_log

# Credit Report access audit log (who / which endpoint / when / from where / result), rotated.
AUDIT_LOG = audit_log.setup(os.path.join(_data, "logs", "credit_report_access.log"),
                            max_bytes=int(os.environ.get("CREDIT_AUDIT_MAX_BYTES", 2_000_000)),
                            backups=int(os.environ.get("CREDIT_AUDIT_BACKUPS", 5)))


def audited_lookup(endpoint):
    """Wrap a Credit Report endpoint: assign a lookup_id (passed to the UI provider's stage logs) and
    write one audit line after the request. Logging only - the handler's behaviour is unchanged."""
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(request):
            request["lookup_id"] = audit_log.new_lookup_id()
            role = role_from_token(request.cookies.get(AUTH_COOKIE)) if AUTH_ON else "open"
            t0 = time.perf_counter()
            result, resp = "ERROR", None
            try:
                resp = await fn(request)
                result = request.get("audit_result") or ("OK" if resp.status == 200 else "NOT_MEMBER" if resp.status == 404 else "ERROR")
                return resp
            except BaseException:
                request.setdefault("audit_stage", "unhandled")
                raise
            finally:
                if request.get("audit_ic"):          # only real lookups (an invalid IC never reaches the browser)
                    audit_log.write(AUDIT_LOG, request, endpoint, role, request["audit_ic"], result,
                                    time.perf_counter() - t0, request["lookup_id"],
                                    request.get("audit_stage") if result == "ERROR" else None)
        return wrapper
    return deco


UI_PROVIDER = None                       # the ONE persistent UI provider (created lazily, closed with the queue)
UI_RUN = {"paused": False, "reason": "", "resume": None, "runs": 0, "lastFinished": None,
          "session": {"state": "unchecked", "at": None}}
SESSION_SYNC = None


def get_session_sync():
    global SESSION_SYNC
    if SESSION_SYNC is None:
        SESSION_SYNC = SessionSync(KW_BASE_URL, api_base=KW_API_BASE, cdp_url=(KW_CDP_URL or "http://127.0.0.1:9222"),
                                   chrome_leveldb=os.environ.get("KW388_NORMAL_CHROME_LEVELDB") or None,
                                   log=lambda m: print(m, file=sys.stderr))
    return SESSION_SYNC


def get_ui_provider():
    global UI_PROVIDER
    if UI_PROVIDER is None:
        UI_PROVIDER = UiCreditReportProvider(KW_BASE_URL, UI_PROFILE_DIR, headless=UI_HEADLESS, slow_mo=UI_SLOW_MO,
                                             log=lambda m: print(m, file=sys.stderr), sync=get_session_sync())
    return UI_PROVIDER


async def ui_startup_session_check():
    """Service start: (1) is the dedicated UI session valid? (2) if not, can an existing authorized
    session be synchronized from the normal Chrome? (3) validate. Then release the profile - the
    browser is (re)opened, once, when a queue starts. Never logs in."""
    prov = get_ui_provider()
    async with _UI_START_LOCK:           # same lock searches take before starting the browser: no race with an early lookup
        try:
            await prov.start()
            UI_RUN["session"] = {"state": "valid" if not prov.metrics.session_syncs else "synced-from-chrome", "at": now_iso()}
        except ReauthRequired:
            UI_RUN["session"] = {"state": "none-available", "at": now_iso(),
                                 "detail": "dedicated profile not authenticated and no valid session in the normal Chrome"}
        except Exception as e:
            UI_RUN["session"] = {"state": "check-failed", "at": now_iso(), "detail": str(e)[:120]}
        finally:
            try:
                await prov.close()
            except Exception:
                pass
    print("ui session check: %s" % UI_RUN["session"].get("state"), file=sys.stderr)


def ui_rec_from_result(name, ic, res):
    """Provider result -> the raw record the rest of the pipeline consumes (parsing / schemas unchanged)."""
    if res["status"] == "ok":
        rec = {"name": name, "ic": ic, **api_report_to_shape(res["report"])}
        rec["member"] = _member_pick([res["member"]], ic) or {}
        return rec
    if res["status"] == "not_member":
        return {"name": name, "ic": ic, "found": False}
    return {"name": name, "ic": ic, "found": "err", "err": (res.get("error") or "ui lookup failed")[:80]}


async def run_pipeline_ui(mode="all", limit=None, fast=False, fresh=True):
    """Credit-report scrape through the genuine UI workflow (CREDIT_REPORT_MODE=ui). Same candidate
    selection, cache, upload and output as before; the per-customer fetch is the UI workflow.
    Sequential on one persistent browser, with the conservative human-like pause between customers."""
    cache_file = os.path.join(CACHE_DIR, "raw_" + today_myt().isoformat() + ".json")
    LIVE_ROWS.clear()
    if fresh and os.path.exists(cache_file):
        try:
            os.remove(cache_file)
        except OSError:
            pass
    await set_state(status="running", stage="starting (UI workflow)", lastLine="", progress=None,
                     result=None, error=None, startedAt=now_iso(), finishedAt=None)
    totals = {"imported": 0, "updated": 0, "invalid": 0}
    prov = get_ui_provider()
    prov.metrics.reset()
    UI_RUN.update(paused=False, reason="", resume=asyncio.Event(), runs=UI_RUN["runs"] + 1)
    try:
        phones, amts, cand = load_ctm()
        await set_state(lastLine="candidate pool: %d" % len(cand))
        if not cand:
            raise RuntimeError("Candidate list has 0 usable rows - check the uploaded file's tabs "
                               "are named with dates (e.g. 307, 78), not Sheet1.")
        overdue = set()
        if mode != "all":
            try:
                known, overdue = await fetch_known_ics()
            except Exception as e:
                raise RuntimeError("Could not verify known customers (mode=%s): %s" % (mode, e))
            cand = [c for c in cand if (cic(c[1]) in known) == (mode == "old")]
        elif SKIP_OVERDUE:
            try:
                _k, overdue = await fetch_known_ics()
            except Exception:
                pass
        if overdue and SKIP_OVERDUE:
            cand = [c for c in cand if cic(c[1]) not in overdue]
        if limit:
            cand = cand[:limit]

        raw = json.load(open(cache_file, encoding="utf-8")) if os.path.exists(cache_file) else []
        scraped = {cic(r["ic"]) for r in raw}
        queue = [c for c in cand if cic(c[1]) not in scraped]
        await set_state(lastLine=("resuming: %d cached, %d left" % (len(raw), len(queue))) if raw
                        else "fresh scrape (UI workflow): %d candidates" % len(queue))
        if raw:
            leads = [to_lead_payload(r) for r in all_scraped_rows(raw, phones, amts)]
            if leads:
                add_totals(totals, await upload_to_backend(leads))

        await prov.start()          # validates the dedicated session, syncing from the normal Chrome if needed
        UI_RUN["session"] = {"state": "valid" if not prov.metrics.session_syncs else "synced-from-chrome", "at": now_iso()}
        await set_state(stage="scraping (UI workflow: Member List -> Credit report)",
                         lastLine="scraping 0 / %d..." % len(queue), progress={"current": 0, "total": len(queue)})
        pending, done_count = [], 0

        async def flush():
            nonlocal pending
            if not pending:
                return
            json.dump(raw, open(cache_file, "w", encoding="utf-8"), ensure_ascii=False)
            leads = [to_lead_payload(r) for r in all_scraped_rows(pending, phones, amts)]
            if leads:
                add_totals(totals, await upload_to_backend(leads))
            pending = []

        for name, ic in queue:
            res = None
            while res is None:
                try:
                    res = await prov.fetch(cic(ic))
                except ReauthRequired as e:
                    # STOP/pause the queue. Release the profile so a human can log in; never type creds.
                    UI_RUN.update(paused=True, reason=MANUAL_REAUTH_REQUIRED)
                    await flush()
                    await prov.close()
                    await set_state(stage=MANUAL_REAUTH_REQUIRED,
                                    lastLine="MANUAL_REAUTH_REQUIRED - no valid KR883 session anywhere (dedicated profile and normal Chrome are both "
                                             "logged out). Log in to KR883 in your normal Chrome, then press Resume. %d / %d done." % (done_count, len(queue)))
                    notify_desktop("KW388 scrape paused", "MANUAL_REAUTH_REQUIRED - log in, then press Resume.")
                    while True:
                        UI_RUN["resume"].clear()
                        await UI_RUN["resume"].wait()
                        if await prov.is_authenticated():
                            break
                        await set_state(lastLine="MANUAL_REAUTH_REQUIRED - still not authenticated; log in and press Resume again.")
                    UI_RUN.update(paused=False, reason="")
                    await set_state(stage="scraping (UI workflow: Member List -> Credit report)",
                                    lastLine="resumed - scraping %d / %d..." % (done_count, len(queue)))
            rec = ui_rec_from_result(name, ic, res)
            raw.append(rec)
            pending.append(rec)
            try:
                for row in all_scraped_rows([rec], phones, amts):
                    LIVE_ROWS.append(to_lead_payload(row))
            except Exception:
                pass
            done_count += 1
            await set_state(lastLine="scraping %d / %d... (%s)" % (done_count, len(queue), res["status"]),
                             progress={"current": done_count, "total": len(queue)})
            if len(pending) >= 20:
                await flush()
            if done_count < len(queue) and KW_HUMAN_DELAY_MAX > 0:
                await asyncio.sleep(random.uniform(KW_HUMAN_DELAY_MIN, KW_HUMAN_DELAY_MAX))
        await flush()

        sheets_note = None
        if GSHEETS_AUTO and GSHEETS_SPREADSHEET_ID:
            try:
                results_path = os.path.join(OUTPUT_DIR, "kw_leads_" + today_myt().isoformat() + ".json")
                if os.path.exists(results_path):
                    rows = _sheet_rows_from_results(results_path)
                    tab = now_myt().strftime(GSHEETS_TAB_PATTERN)
                    info = await asyncio.get_event_loop().run_in_executor(None, _push_to_sheets, rows, tab)
                    sheets_note = "exported %d rows to Google Sheets tab '%s'" % (info["rows"], tab)
            except Exception as e:
                sheets_note = "Google Sheets export FAILED (%s) - data is safe on disk" % str(e)[:120]
        m = prov.metrics.as_dict()
        await set_state(status="done", stage=None, progress=None, result=totals,
                         lastLine=(sheets_note or "") or "UI workflow: %d ok, %d not members, %d errors, avg %.1fs/lookup"
                         % (m["ok"], m["notMember"], m["errors"], m["avgTotalMs"] / 1000), finishedAt=now_iso())
        notify_desktop("KW388 scrape complete", "%d imported, %d updated, %d invalid." % (
            totals.get("imported", 0), totals.get("updated", 0), totals.get("invalid", 0)))
        try:
            os.remove(cache_file)
        except FileNotFoundError:
            pass
    except Exception as e:
        await set_state(status="error", error=str(e)[:400], finishedAt=now_iso())
        notify_desktop("KW388 scrape FAILED", str(e)[:180])
    finally:
        UI_RUN["paused"] = False
        UI_RUN["lastFinished"] = now_iso()
        await prov.close()                # queue over -> release the dedicated profile


def pick_runner():
    """The scrape pipeline (button + scheduler): always the UI workflow."""
    return run_pipeline_ui


async def handle_ui_status(request):
    prov = UI_PROVIDER
    return web.json_response({
        "creditReportMode": CREDIT_REPORT_MODE, "headless": UI_HEADLESS, "profile": UI_PROFILE_DIR,
        "paused": UI_RUN["paused"], "reason": UI_RUN["reason"],
        "session": UI_RUN["session"], "syncedCopies": SESSION_SYNC.syncs if SESSION_SYNC else 0,
        "account": prov.account if prov else None,
        "browserAlive": bool(prov and prov.alive), "browserLaunches": prov.launches if prov else 0,
        "metrics": prov.metrics.as_dict() if prov else None,
    })


async def handle_ui_resume(request):
    """After a human has re-authenticated the dedicated profile: continue the paused queue."""
    if not (UI_RUN["paused"] and UI_RUN["resume"]):
        return web.json_response({"status": "not_paused"})
    UI_RUN["resume"].set()
    return web.json_response({"status": "resuming"})


async def handle_ui_reauth(request):
    """Open a VISIBLE window on the dedicated profile so the human can log in (nothing is typed for
    them), then resume the paused queue once the profile is authenticated."""
    prov = get_ui_provider()
    if not UI_RUN["paused"]:
        return web.json_response({"error": "queue is not paused for re-authentication"}, status=409)

    async def _go():
        ok = await prov.open_login_window()
        if ok and UI_RUN["resume"]:
            UI_RUN["resume"].set()
    asyncio.create_task(_go())
    return web.json_response({"status": "login_window_opening"})


# ---------------- on-demand single-candidate check ----------------
async def check_one(ic, lookup_id=None):
    """On-demand single-candidate refresh for /check. Goes through the SAME UI provider as
    /credit-report (Member List -> Actions -> Credit report on the persistent dedicated browser and
    its already-authorized session) - no separate browser, no login, no OTP, no direct API call."""
    res = await ui_lookup(ic, lookup_id)
    if res["status"] == "not_member":
        raise NotAMember("Not found on KW388 (bad/changed IC?)")
    if res["status"] != "ok":
        raise UiLookupError("UI lookup failed: " + (res.get("error") or "unknown")[:160], res.get("failedStage"))
    data = api_report_to_shape(res["report"])
    loans = data.get("loans", []) or []; txs = data.get("txs", []) or []; st = data.get("stats", {}) or {}
    return {**kw388_signals(loans, txs, st), "period": str(median_days(recent5(loans, txs)) or "")}

# ---------------- RoyalPay: Payout List scrape + name match against KW388 + KW388 enrichment ----------------
async def _rp_attempt_login(page):
    """One full pass: reload the login page, submit username/password, then a TOTP code in the
    modal that appears (confirmed live: fields are input[name=Username/Password/VerifyCode],
    buttons are 'Sign In' then 'Submit')."""
    await page.goto(f"{RP_BASE}/Login", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(1000)
    if "Login" not in page.url:
        return True

    await page.fill("input[name=Username]", RP_USER)
    await page.fill("input[name=Password]", RP_PASS)
    await page.click("button[type=submit]:has-text('Sign In')")
    await page.wait_for_timeout(2000)

    code_box = page.locator("input[name=VerifyCode]")
    for attempt in range(2):  # a code can straddle a 30s boundary — retry once with a fresh one
        if "Login" not in page.url:
            break
        if not await code_box.count():
            break
        try:
            await code_box.wait_for(state="visible", timeout=15000)
            await code_box.click(timeout=15000)
            await code_box.fill("")
            await code_box.press_sequentially(totp(RP_TOTP_SECRET), delay=60)
            await page.wait_for_timeout(500)
            submit_btn = page.locator("button[type=submit]:has-text('Submit')")
            if await submit_btn.count():
                await submit_btn.click(timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(2000)

    return "Login" not in page.url

async def rp_ensure_logged_in(ctx):
    page = await acquire_page(ctx)
    try:
        await page.goto(f"{RP_BASE}/Dashboard", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1000)
        if "Login" not in page.url:
            return
    except Exception:
        pass

    last_err = None
    for outer_attempt in range(3):  # a stuck/glitched login page gets a full clean reload + retry
        try:
            if await _rp_attempt_login(page):
                return
        except Exception as e:
            last_err = e
        await page.wait_for_timeout(2000)

    try:
        await page.screenshot(path=os.path.join(CACHE_DIR, "royalpay_login_failure.png"))
    except Exception:
        pass
    raise RuntimeError(f"RoyalPay login failed after 3 attempts: {last_err}")

RP_ROW_JS = """table => Array.from(table.querySelectorAll('tbody tr')).map(tr =>
  Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim()))"""

async def rp_scrape_payouts_page(page):
    return await page.locator("table#Payout").evaluate(RP_ROW_JS)

def rp_sig(rows):
    return tuple(tuple(r) for r in rows)

async def rp_scrape_today_payouts(page):
    """Payout?startdate=...&enddate=... takes the date directly, no UI date-picker needed.
    Pagination is DataTables' numbered pager (#Payout_paginate) — data-dt-idx=N is page N (0 is
    'previous', the last idx is 'next'), so clicking by that attribute is unambiguous regardless
    of how many pages exist."""
    today = today_myt().isoformat()
    await page.goto(f"{RP_BASE}/Payout?startdate={today}&enddate={today}", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(1500)

    all_rows = []
    page_num = 1
    while True:
        rows = await rp_scrape_payouts_page(page)
        sig = rp_sig(rows)
        all_rows += rows

        nxt = page.locator(f'#Payout_paginate a[data-dt-idx="{page_num + 1}"]')
        if not await nxt.count():
            break
        try:
            await nxt.click(timeout=3000)
        except Exception:
            break
        page_num += 1
        for _w in range(20):
            await page.wait_for_timeout(300)
            if rp_sig(await rp_scrape_payouts_page(page)) != sig:
                break
        else:
            break
    return all_rows

def rp_parse_row(cells):
    """Confirmed live column order: 0=#, 1=Payout ID, 2=Reference ID, 3=Created By, 4=Bank From,
    5=Bank To (bank name<br>customer name<br>account number), 6=Bank Time, 7=Reference Number,
    8=Payout Date ("YYYY-MM-DD HH:MM:SS"), 9=Remark, 10=Amount, 11=Fee, ..."""
    if len(cells) < 11:
        return None
    payout_id = cells[1].strip()
    parts = [p.strip() for p in cells[5].split("\n") if p.strip()]
    bank_name = parts[0] if len(parts) > 0 else ""
    full_name = parts[1] if len(parts) > 1 else ""
    account_no = parts[2] if len(parts) > 2 else ""
    payout_date = cells[8].strip()
    amount = cells[10].strip()
    if not payout_id or not full_name or not payout_date:
        return None
    return {
        "payoutId": payout_id, "fullName": full_name, "bankName": bank_name,
        "bankAccountNo": account_no, "amount": amount, "payoutDate": payout_date,
    }

async def _backend_session(s):
    """Log the given aiohttp ClientSession in as the backend admin + unlock the PIN gate — shared by
    every call into the backend's API (upload_to_backend, match_names_on_backend, upload_royalpay_payouts)."""
    async with s.post(f"{API_BASE}/auth/login", json={"username": BACKEND_USER, "password": BACKEND_PASS}) as r:
        body = await r.text()
        if r.status != 200:
            raise RuntimeError(f"Backend login failed ({r.status}): {body[:200]}")
    async with s.post(f"{API_BASE}/portal/pin/unlock", json={"pin": BACKEND_PIN}) as r:
        body = await r.text()
        if r.status != 200:
            raise RuntimeError(f"Backend PIN unlock failed ({r.status}): {body[:200]}")

async def fetch_known_ics():
    """All non-deleted kw_leads ICs currently in the backend (the ground truth for splitting a
    candidate pool into new-vs-already-known, used by /candidates/preview and the mode='new'/
    'old' scrape filter) plus the subset chronically overdue (30+ days — see OVERDUE_SKIP_DAYS)
    that's worth skipping on a re-scrape to save time. Always fetched live (never cached across
    runs), so mode='new' run twice in a row is self-correcting: anyone the first run just
    imported is no longer 'new' on the second. Returns (known_ics, overdue_ics)."""
    if LOCAL_ONLY:
        # No backend, so "known" and "chronically overdue" are read from the most recent local
        # results file instead. Anyone last seen 30+ days overdue (OVERDUE_SKIP_DAYS) is returned
        # in the overdue set, which run_pipeline_ui removes from the candidate pool - so they are not
        # re-scraped. Both sets empty on the very first run (no prior results yet).
        import glob
        def _od(r):
            best = 0
            for k in ("cardOverdueDays", "overdueDays"):
                try:
                    best = max(best, int(float(r.get(k) or 0)))
                except (TypeError, ValueError):
                    pass
            return best
        known, overdue = set(), set()
        files = sorted(glob.glob(os.path.join(OUTPUT_DIR, "kw_leads_*.json")))
        # "known" = ICs from the newest run of any size. "overdue" (the 30-day skip set) is a
        # union across COMPLETE runs only (>=5000 rows) - a partial/broken file would otherwise
        # both miss real overdue customers and, if garbage, skip the wrong ones. Anyone measured
        # 30+ days overdue in any recent complete run is skipped.
        for f in files:
            try:
                rows = json.load(open(f, encoding="utf-8"))
            except Exception:
                continue
            complete = len(rows) >= 5000
            newest = (f == files[-1])
            for r in rows:
                ic = cic(r.get("icNumber") or r.get("ic"))
                if len(ic) != 12:
                    continue
                if newest:
                    known.add(ic)
                if complete and _od(r) >= OVERDUE_SKIP_DAYS:
                    overdue.add(ic)
        return known, overdue
    timeout = ClientTimeout(total=30)
    async with ClientSession(timeout=timeout) as s:
        await _backend_session(s)
        async with s.get(f"{API_BASE}/kw-leads/known-ics") as r:
            body = await r.json()
            if r.status != 200:
                raise RuntimeError(f"Backend known-ICs lookup failed ({r.status}): {str(body)[:200]}")
            data = body.get("data", body)
    return set(data.get("icNumbers", [])), set(data.get("overdueIcNumbers", []))

async def match_names_on_backend(names):
    """Bulk fullName -> {icNumber, phone} lookup against existing KW388 data — RoyalPay's own
    Payout List never shows IC, only name + bank account number."""
    if not names:
        return {}
    if LOCAL_ONLY:
        # Name -> IC resolution lives entirely in the backend's kw_leads table; with no backend
        # there's nothing to match against, so payouts are recorded unmatched (and unenriched).
        return {}
    timeout = ClientTimeout(total=60)
    async with ClientSession(timeout=timeout) as s:
        await _backend_session(s)
        async with s.post(f"{API_BASE}/kw-leads/match-names", json={"names": names}) as r:
            body = await r.json()
            if r.status != 200:
                raise RuntimeError(f"Backend name-match failed ({r.status}): {str(body)[:200]}")
            data = body.get("data", body)
    return {row["fullName"].strip().lower(): row for row in data}

async def enrich_with_kw388(matched_pairs):
    """For customers whose RoyalPay payout matched a KW388 IC, look up their KW388 credit
    signals too — same scrape_customer used everywhere else, just a separate Chrome profile so
    this never collides with the main scan or Check Now."""
    if not matched_pairs:
        return {}
    async with async_playwright() as p:
        ctx, close_ctx = await open_browser(p, RP_KW_ENRICH_PROFILE_DIR)
        await block_heavy_resources(ctx)
        try:
            await ensure_logged_in(ctx)
            raw, _expired = await scrape_batch(ctx, matched_pairs)
        finally:
            await close_ctx()
    out = {}
    for rec in raw:
        if rec.get("found") is not True:
            continue
        loans = rec.get("loans", []) or []; txs = rec.get("txs", []) or []; st = rec.get("stats", {}) or {}
        out[cic(rec.get("ic"))] = kw388_signals(loans, txs, st)
    return out

RP_IMPORT_CHUNK = 1000  # royalPayImportRequestSchema caps a single request at 2000 rows

async def upload_royalpay_payouts(payout_rows):
    if LOCAL_ONLY:
        t = _write_local_results(payout_rows, f"royalpay_payouts_{today_myt().isoformat()}.json")
        # Same shape the backend returns, so rp_run_pipeline's result totals stay consistent.
        return {"newPayouts": t["imported"], "leadsCreated": 0, "leadsUpdated": t["updated"]}
    timeout = ClientTimeout(total=120)
    async with ClientSession(timeout=timeout) as s:
        await _backend_session(s)
        totals = {"newPayouts": 0, "leadsCreated": 0, "leadsUpdated": 0}
        for i in range(0, len(payout_rows), RP_IMPORT_CHUNK):
            chunk = payout_rows[i:i + RP_IMPORT_CHUNK]
            async with s.post(f"{API_BASE}/royalpay-leads/import", json={"payouts": chunk}) as r:
                body = await r.json()
                if r.status != 200:
                    raise RuntimeError(f"RoyalPay import failed ({r.status}) on rows {i}-{i+len(chunk)}: {str(body)[:200]}")
                data = body.get("data", body)
                for k in totals:
                    totals[k] += data.get(k, 0)
        return totals

# ---------------- RoyalPay run state (separate from KW388's STATE) ----------------
RP_STATE_LOCK = asyncio.Lock()
RP_STATE = {"status": "idle", "stage": None, "lastLine": "", "progress": None, "result": None,
            "error": None, "startedAt": None, "finishedAt": None}

async def set_rp_state(**kw):
    async with RP_STATE_LOCK:
        RP_STATE.update(kw)

async def get_rp_state():
    async with RP_STATE_LOCK:
        return dict(RP_STATE)

async def rp_run_pipeline():
    await set_rp_state(status="running", stage="logging in (TOTP)", lastLine="", progress=None,
                        result=None, error=None, startedAt=now_iso(), finishedAt=None)
    try:
        async with async_playwright() as p:
            ctx, close_ctx = await open_browser(p, RP_PROFILE_DIR)
            await block_heavy_resources(ctx)
            await rp_ensure_logged_in(ctx)
            page = await acquire_page(ctx)

            await set_rp_state(stage="scraping Payout List (today)")
            raw_rows = await rp_scrape_today_payouts(page)
            await close_ctx()

        parsed = [row for row in (rp_parse_row(r) for r in raw_rows) if row]
        await set_rp_state(lastLine="scraped %d payout rows" % len(parsed))
        if not parsed:
            await set_rp_state(status="done", stage=None, progress=None,
                                result={"newPayouts": 0, "leadsCreated": 0, "leadsUpdated": 0}, finishedAt=now_iso())
            return

        names = sorted({r["fullName"] for r in parsed})
        await set_rp_state(stage="matching names against KW388 data", lastLine="matching %d names..." % len(names))
        matched = await match_names_on_backend(names)
        for r in parsed:
            m = matched.get(r["fullName"].strip().lower())
            if m:
                r["icNumber"] = m.get("icNumber") or ""
                r["phone"] = m.get("phone") or ""

        matched_pairs = sorted({(r["fullName"], r["icNumber"]) for r in parsed if r.get("icNumber")})
        if matched_pairs:
            await set_rp_state(stage="looking up KW388 credit signals",
                                lastLine="checking %d matched customers..." % len(matched_pairs))
            signals_by_ic = await enrich_with_kw388(matched_pairs)
            for r in parsed:
                sig = signals_by_ic.get(cic(r.get("icNumber") or ""))
                if sig:
                    r.update(sig)

        await set_rp_state(stage="uploading to backend", lastLine="uploading %d payout rows..." % len(parsed))
        result = await upload_royalpay_payouts(parsed)
        await set_rp_state(status="done", stage=None, progress=None, result=result, finishedAt=now_iso())
    except Exception as e:
        await set_rp_state(status="error", error=str(e)[:400], finishedAt=now_iso())

# ---------------- candidate list upload ----------------
def read_manifest():
    """Manifest of uploaded candidate files. Oldest first — candidate_files() reverses it so the
    newest upload wins on a shared IC."""
    if os.path.exists(CANDIDATES_MANIFEST_PATH):
        try:
            m = json.load(open(CANDIDATES_MANIFEST_PATH, encoding="utf-8"))
            if isinstance(m.get("files"), list):
                return m
        except Exception:
            pass  # unreadable manifest is equivalent to no manifest — rebuild from scratch
    return {"files": []}


def write_manifest(m):
    tmp = CANDIDATES_MANIFEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CANDIDATES_MANIFEST_PATH)


def safe_filename(name):
    """Strip any directory component and anything non-portable. Without this an uploaded name
    like ../../evil.xlsx would escape the candidates directory."""
    name = os.path.basename(str(name or "")).replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip() or "upload.xlsx"
    return name[:120]


async def handle_candidates_upload(request):
    """Accept one or more .xlsx candidate lists.

    Uploads are ADDITIVE by default: every list ever uploaded is kept on disk, so old lists never
    need re-uploading. ?replace=true doesn't delete anything either — it deactivates the previous
    lists so only the new upload feeds the pool, and they can be switched back on at any time."""
    replace = (request.query.get("replace", "false").lower() == "true")
    reader = await request.multipart()
    saved, errors = [], []
    os.makedirs(CANDIDATES_DIR, exist_ok=True)

    pending = []
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name not in ("file", "files"):
            continue
        fname = safe_filename(part.filename)
        if not fname.lower().endswith((".xlsx", ".xlsm")):
            errors.append(f"{fname}: not an .xlsx file")
            await part.read()  # drain, or the next part boundary is misread
            continue

        # Write to a temp name first: a file that fails openpyxl validation must never be left
        # where candidate_files() would pick it up.
        tmp_path = os.path.join(CANDIDATES_DIR, f".incoming_{fname}")
        size = 0
        try:
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ValueError(f"file exceeds {MAX_UPLOAD_BYTES // (1024*1024)}MB limit")
                    f.write(chunk)
            wb = openpyxl.load_workbook(tmp_path, data_only=True, read_only=True)
            tabs = list(wb.sheetnames)
            wb.close()
        except Exception as e:
            try: os.remove(tmp_path)
            except OSError: pass
            errors.append(f"{fname}: {str(e)[:160]}")
            continue
        pending.append((fname, tmp_path, size, tabs))

    if not pending:
        return web.json_response({"error": "no valid .xlsx file received", "errors": errors}, status=400)

    # Only now that at least one file is known-good does the active set change. Nothing is
    # deleted — "replace" just switches the previous lists off.
    manifest = read_manifest()
    if replace:
        for old in manifest["files"]:
            old["active"] = False

    for fname, tmp_path, size, tabs in pending:
        dest = os.path.join(CANDIDATES_DIR, fname)
        # Re-uploading the same filename replaces that file's contents but keeps its slot; a
        # genuinely different list should have a different name.
        os.replace(tmp_path, dest)
        key = "kw-candidates/" + fname
        prev = next((f for f in manifest["files"] if f.get("key") == key), None)
        manifest["files"] = [f for f in manifest["files"] if f.get("key") != key]
        manifest["files"].append({
            "key": key, "name": fname, "size": size, "uploadedAt": now_iso(), "tabs": tabs,
            "active": True,
            "firstUploadedAt": (prev or {}).get("firstUploadedAt") or now_iso(),
        })
        saved.append({"name": fname, "size": size, "tabs": tabs})

    write_manifest(manifest)

    # Report the parsed pool straight back so a bad tab-naming scheme is visible immediately
    # rather than only when a scrape later returns zero rows.
    try:
        _p, _a, cand = load_ctm()
        total = len(cand)
    except Exception as e:
        return web.json_response({"saved": saved, "errors": errors, "total": 0,
                                  "warning": f"Uploaded, but the list could not be parsed: {str(e)[:200]}"})
    warning = None
    if total == 0:
        warning = ("Uploaded, but 0 usable rows were found — sheet tabs must be named with plain "
                   "numbers (e.g. \"307\", \"78\"), and column A/B must hold name/IC.")
    return web.json_response({"saved": saved, "errors": errors, "total": total, "warning": warning})


async def handle_candidates_list(request):
    """Every list ever uploaded, newest first, each flagged active/inactive. Also reports how many
    unique candidates each file contributes on its own, so it's obvious what switching one off
    would cost."""
    manifest = read_manifest()
    files = []
    for f in reversed(manifest["files"]):
        p = os.path.join(STORAGE_ROOT, f.get("key", ""))
        exists = os.path.exists(p)
        rows = None
        if exists:
            try:
                wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
                seen = set()
                for sh in wb.sheetnames:
                    ws = wb[sh]
                    for row in ws.iter_rows(min_col=1, max_col=IC_COL, values_only=True):
                        if len(row) >= IC_COL and row[0]:
                            ic = cic(row[IC_COL - 1])
                            if len(ic) == 12:
                                seen.add(ic)
                wb.close()
                rows = len(seen)
            except Exception:
                rows = None  # unreadable file still gets listed, just without a count
        files.append({**f, "active": f.get("active", True), "exists": exists, "candidates": rows})
    legacy = os.path.exists(CANDIDATES_PATH) and not files
    active_total = 0
    try:
        _p, _a, cand = load_ctm()
        active_total = len(cand)
    except Exception:
        pass
    return web.json_response({"files": files, "legacyFile": legacy, "activeTotal": active_total})


async def handle_candidates_toggle(request):
    """Switch a stored list in or out of the active pool without deleting it."""
    name = safe_filename(request.match_info["name"])
    try:
        body = await request.json()
    except Exception:
        body = {}
    manifest = read_manifest()
    key = "kw-candidates/" + name
    target = next((f for f in manifest["files"] if f.get("key") == key), None)
    if target is None:
        return web.json_response({"error": "no such file"}, status=404)
    target["active"] = bool(body["active"]) if "active" in body else not target.get("active", True)
    write_manifest(manifest)
    return web.json_response({"name": name, "active": target["active"]})


async def handle_candidates_delete(request):
    name = safe_filename(request.match_info["name"])
    manifest = read_manifest()
    key = "kw-candidates/" + name
    before = len(manifest["files"])
    manifest["files"] = [f for f in manifest["files"] if f.get("key") != key]
    if len(manifest["files"]) == before:
        return web.json_response({"error": "no such file"}, status=404)
    p = os.path.join(CANDIDATES_DIR, name)
    try: os.remove(p)
    except OSError: pass
    write_manifest(manifest)
    return web.json_response({"deleted": name})


# ---------------- Google Sheets export ----------------
# What gets written to Excel / Google Sheets. Mirrors the existing BLASTER sheet's columns and
# order exactly (A-H), with the remark breakdown added as column I.
EXPORT_COLUMNS = [
    ("fullName", "Customer"), ("icNumber", "IC"), ("phone", "Phone"),
    ("todayCount", "Today"), ("totalCompleted", "Completed"), ("totalDisbursed", "Disbursed"),
    ("recentDetail", "Recent 5 (days/RM)"), ("disbursedByDate", "Loans Disbursed Per Date"),
    ("disbursedByRemark", "Remarks (I/O/IO)"),
]

# The on-page table shows more than the export does — the extra signals are useful for sorting
# and triage on screen without widening the sheet everyone actually works from.
SHEET_COLUMNS = EXPORT_COLUMNS + [
    ("topRemark", "Top Remark"), ("openLoans", "Open Loans"),
    ("overdueToday", "Overdue"), ("overdueDays", "Overdue Days"),
    ("loanAmount", "Loan Amount"), ("period", "Period (days)"),
]

# Columns the UI should sort numerically rather than as text.
NUMERIC_COLUMNS = {"loanAmount", "period", "todayCount", "totalCompleted", "totalDisbursed",
                   "overdueDays", "openLoans"}


def _num(v):
    """Render a number without a trailing .0 — the sheet shows "9d/800", never "9d/800.0"."""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _fmt_cell(v):
    if v is None or v == "":
        return ""
    if isinstance(v, bool):
        return "YES" if v else "NO"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, list):
        # Formats deliberately match the existing BLASTER sheet exactly, so an exported tab can be
        # dropped in beside the manual ones without reformatting.
        parts = []
        for o in v:
            if isinstance(o, dict) and ("days" in o or "amt" in o):
                parts.append(f"{_num(o.get('days','?'))}d/{_num(o.get('amt','?'))}")   # "9d/800"
            elif isinstance(o, dict) and "code" in o:
                parts.append(f"{o['code']}: {o.get('count', 0)}")            # "IO: 3"
            elif isinstance(o, dict) and "date" in o:
                parts.append(f"{o['date']}: {o.get('count', 0)}")            # "28 Aug 2026: 6"
            else:
                parts.append(str(o))
        return ", ".join(parts)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def enrich_row(r):
    """Add the derived fields the table sorts and filters on.

    `topRemark` is the channel with the most currently-open loans — the single most useful thing
    to sort or filter by, since disbursedByRemark itself is a list and can't be sorted directly.
    `openLoans` totals those counts, so "who is juggling the most live debt" is one click."""
    remarks = r.get("disbursedByRemark") or []
    top = max(remarks, key=lambda e: e.get("count", 0), default=None)
    return {
        **r,
        "topRemark": (top or {}).get("code", ""),
        "openLoans": sum(e.get("count", 0) for e in remarks),
        "remarkCodes": [e.get("code", "") for e in remarks],
    }


def latest_results_file():
    files = sorted(f for f in os.listdir(OUTPUT_DIR) if f.startswith("kw_leads_") and f.endswith(".json"))
    return os.path.join(OUTPUT_DIR, files[-1]) if files else None


def sort_for_export(rows):
    """Busiest borrowers first — same ordering the manual BLASTER sheet uses (Today desc), with
    open-loan count and completed count breaking ties."""
    return sorted(rows, key=lambda r: (
        -int(r.get("todayCount") or 0),
        -sum(e.get("count", 0) for e in (r.get("disbursedByRemark") or [])),
        -int(r.get("totalCompleted") or 0),
    ))


def _export_cell(r, k):
    if k == "phone":
        return str(r.get(k) or "").lstrip("+")
    return _fmt_cell(r.get(k))


def _sheet_rows_from_results(path):
    rows = sort_for_export(json.load(open(path, encoding="utf-8")))
    header = [label for _k, label in EXPORT_COLUMNS]
    return [header] + [[_export_cell(r, k) for k, _label in EXPORT_COLUMNS] for r in rows]


def _sheet_rows_from_candidates():
    phones, amts, cand = load_ctm()
    header = ["Name", "IC", "Phone", "Loan Amount"]
    return [header] + [[nm, ic, phones.get(ic, ""), _fmt_cell(amts.get(ic, ""))] for nm, ic in cand]


def _push_to_sheets(rows, worksheet_name):
    """Blocking — gspread is sync, so callers run this in a thread executor."""
    import gspread
    from google.oauth2.service_account import Credentials

    if not GSHEETS_SPREADSHEET_ID:
        raise RuntimeError("GOOGLE_SHEETS_SPREADSHEET_ID is not set in .env")
    if not os.path.exists(GSHEETS_CREDENTIALS):
        raise RuntimeError(f"Service-account key file not found at {GSHEETS_CREDENTIALS} "
                           "(set GOOGLE_SHEETS_CREDENTIALS in .env)")

    creds = Credentials.from_service_account_file(
        GSHEETS_CREDENTIALS,
        scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GSHEETS_SPREADSHEET_ID)
    try:
        ws = sh.worksheet(worksheet_name)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=worksheet_name, rows=max(len(rows) + 10, 100),
                              cols=max(len(rows[0]) if rows else 1, 10))
    ws.update(values=rows, range_name="A1")
    try:
        ws.freeze(rows=1)
    except Exception:
        pass  # cosmetic only — never fail an export over it
    return {"rows": len(rows) - 1, "worksheet": worksheet_name, "spreadsheetUrl": sh.url}


async def handle_export_sheets(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    source = body.get("source", "results")
    worksheet = body.get("worksheet") or GSHEETS_WORKSHEET

    try:
        if source == "candidates":
            rows = _sheet_rows_from_candidates()
            worksheet = body.get("worksheet") or "Candidate List"
        else:
            path = body.get("file")
            path = os.path.join(OUTPUT_DIR, safe_filename(path)) if path else latest_results_file()
            if not path or not os.path.exists(path):
                return web.json_response(
                    {"error": "No scrape results to export yet — run a scrape first, or export "
                              "source 'candidates' to push the uploaded list itself."}, status=404)
            rows = _sheet_rows_from_results(path)
    except Exception as e:
        return web.json_response({"error": f"Could not build export rows: {str(e)[:200]}"}, status=400)

    if len(rows) <= 1:
        return web.json_response({"error": "Nothing to export — 0 data rows."}, status=400)

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _push_to_sheets, rows, worksheet)
    except ImportError:
        return web.json_response(
            {"error": "Google Sheets support not installed — run: pip install gspread google-auth"},
            status=500)
    except Exception as e:
        return web.json_response({"error": f"Google Sheets export failed: {str(e)[:300]}"}, status=502)
    return web.json_response({"ok": True, **result})


def build_results_xlsx(rows, presorted=False):
    """Render result rows to a real .xlsx in memory, using the same column set as the Sheets
    export so the two never drift apart.

    Sorts by default so a bare call can't produce an arbitrarily-ordered sheet. Callers that have
    already applied their own ordering (select_rows, honouring the user's sort choice) pass
    presorted=True, otherwise that choice would be silently overwritten here."""
    import io
    if not presorted:
        rows = sort_for_export(rows)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = today_myt().strftime("%d-%m")   # matches the sheet's per-day tab naming
    ws.append([label for _k, label in EXPORT_COLUMNS])
    for c in ws[1]:
        c.font = openpyxl.styles.Font(bold=True)

    numeric = {"todayCount", "totalCompleted", "totalDisbursed"}
    for r in rows:
        out = []
        for k, _label in EXPORT_COLUMNS:
            v = r.get(k)
            if k == "phone":
                # The working sheet holds bare digits (601...), not E.164 with a leading +.
                out.append(str(v or "").lstrip("+"))
                continue
            if k in numeric:
                try:
                    out.append(int(v))       # keep numbers sortable in Excel, not text
                    continue
                except (TypeError, ValueError):
                    pass
            out.append(_fmt_cell(v))
        ws.append(out)

    # IC and phone are digit strings, not quantities — force text so Excel neither switches to
    # scientific notation nor strips a leading zero.
    for col in (2, 3):
        for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
            for c in row:
                c.number_format = "@"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions   # column dropdowns for sorting/filtering in Excel
    widths = {"fullName": 32, "icNumber": 15, "phone": 15, "recentDetail": 34,
              "disbursedByDate": 60, "disbursedByRemark": 28}
    for i, (k, label) in enumerate(EXPORT_COLUMNS, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = widths.get(k, max(len(label) + 2, 11))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _results_path(name):
    p = os.path.join(OUTPUT_DIR, safe_filename(name))
    if not os.path.exists(p):
        return None
    return p


# Named filters for downloads. Each takes a result row and says whether it belongs in the file.
RESULT_FILTERS = {
    "all": lambda r: True,
    # "Took a loan today" - todayCount counts only Disbursed/Approved loans dated today, so this
    # is genuinely "borrowed today", not "has an application sitting in review".
    "today": lambda r: int(r.get("todayCount") or 0) > 0,
    "overdue": lambda r: bool(r.get("overdueToday")) or int(r.get("overdueDays") or 0) > 0,
    "today_not_overdue": lambda r: (int(r.get("todayCount") or 0) > 0
                                    and not r.get("overdueToday")
                                    and int(r.get("overdueDays") or 0) == 0),
    "clean": lambda r: (not r.get("overdueToday")) and int(r.get("overdueDays") or 0) == 0,
    # "Filtered scrape": only today's borrowers, minus the genuinely bad ones. A customer is
    # dropped only if BOTH (a) not actually repaying - no Loan Repayment on the transaction log's
    # first page (which also filters out the admin late-penalty data-entry bug, since real active
    # customers keep repaying) AND (b) weak history - most recent loan Rejected, or fewer than 5
    # completed loans. hasRecentRepayment is None on older result files (field didn't exist) - we
    # treat unknown as repaying, so we never wrongly drop someone we can't assess.
    "filtered": lambda r: (
        int(r.get("todayCount") or 0) > 0
        and not (
            r.get("hasRecentRepayment") is False
            and (bool(r.get("recentLoanRejected")) or int(r.get("totalCompleted") or 0) < 5)
        )
    ),
}

FILTER_SUFFIX = {
    "all": "", "today": "_loan-today", "overdue": "_overdue",
    "today_not_overdue": "_loan-today-clean", "clean": "_no-overdue",
    "filtered": "_filtered",
}


def _sort_value(r, key):
    if key in NUMERIC_COLUMNS:
        try:
            return -float(r.get(key) or 0)      # numeric columns default high-to-low
        except (TypeError, ValueError):
            return 0.0
    if key == "openLoans":
        return -sum(e.get("count", 0) for e in (r.get("disbursedByRemark") or []))
    return str(r.get(key) or "").lower()


def select_rows(rows, filt="all", remark="", sort_key=None, descending=None):
    """Apply a named filter, an optional remark-code filter, and a sort."""
    fn = RESULT_FILTERS.get(filt, RESULT_FILTERS["all"])
    out = [r for r in rows if fn(r)]
    if remark:
        out = [r for r in out
               if any(e.get("code") == remark for e in (r.get("disbursedByRemark") or []))]
    if not sort_key:
        return sort_for_export(out)
    out = sorted(out, key=lambda r: _sort_value(r, sort_key))
    # _sort_value already negates numerics, so "descending" is its natural order; reverse only
    # when the caller explicitly asked for the opposite.
    if descending is False:
        out.reverse()
    return out


async def handle_results_download(request):
    """Download a results file as .xlsx (default) or the raw .json.

    Query params:
      format=xlsx|json
      filter=all|today|overdue|today_not_overdue|clean
      remark=<code>      only customers with open loans under that remark (e.g. IO)
      sort=<column key>  e.g. todayCount, overdueDays, totalCompleted
      dir=desc|asc
    """
    name = request.match_info["name"]
    fmt = request.query.get("format", "xlsx").lower()
    filt = request.query.get("filter", "all").lower()
    remark = request.query.get("remark", "")
    sort_key = request.query.get("sort") or None
    dir_param = request.query.get("dir", "").lower()
    descending = None if dir_param not in ("asc", "desc") else (dir_param == "desc")

    if filt not in RESULT_FILTERS:
        return web.json_response(
            {"error": f"unknown filter '{filt}'", "valid": sorted(RESULT_FILTERS)}, status=400)

    path = _results_path(name)
    if not path:
        return web.json_response({"error": "no such results file"}, status=404)
    rows = select_rows(json.load(open(path, encoding="utf-8")),
                       filt, remark, sort_key, descending)
    if not rows:
        return web.json_response(
            {"error": f"no rows match filter '{filt}'" + (f" + remark '{remark}'" if remark else "")},
            status=404)

    stem = os.path.splitext(os.path.basename(path))[0] + FILTER_SUFFIX.get(filt, "")
    if remark:
        stem += "_" + re.sub(r"[^A-Za-z0-9]+", "-", remark).strip("-")
    if fmt == "json":
        return web.Response(
            body=json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8"),
            content_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{stem}.json"'})
    data = await asyncio.get_event_loop().run_in_executor(
        None, functools.partial(build_results_xlsx, rows, presorted=True))
    return web.Response(
        body=data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{stem}.xlsx"'})


async def handle_results_preview(request):
    """First N rows, for the on-page table."""
    name = request.match_info["name"]
    path = _results_path(name)
    if not path:
        return web.json_response({"error": "no such results file"}, status=404)
    try:
        limit = max(1, min(500, int(request.query.get("limit", 50))))
    except Exception:
        limit = 50
    rows = sort_for_export(json.load(open(path, encoding="utf-8")))
    return web.json_response(_table_payload(rows, limit))


def _table_payload(rows, limit):
    """Shared shape for the results table: formatted display strings plus raw numeric values so
    the client can sort numerically without re-parsing "12" out of a string."""
    enriched = [enrich_row(r) for r in rows]
    codes = sorted({c for r in enriched for c in r["remarkCodes"] if c})
    out = []
    for r in enriched[:limit]:
        cells = {k: _fmt_cell(r.get(k)) for k, _l in SHEET_COLUMNS}
        for k in NUMERIC_COLUMNS:
            try:
                cells["_" + k] = float(r.get(k) or 0)
            except (TypeError, ValueError):
                cells["_" + k] = 0.0
        cells["_codes"] = r["remarkCodes"]
        out.append(cells)
    return {
        "total": len(rows),
        "shown": len(out),
        "columns": [{"key": k, "label": l, "numeric": k in NUMERIC_COLUMNS} for k, l in SHEET_COLUMNS],
        "remarkCodes": codes,
        "rows": out,
    }


async def handle_results_live(request):
    """Rows produced by the run that's in progress (or the most recent one), so data is visible
    while a multi-hour scrape is still going rather than only at the end."""
    try:
        limit = max(1, min(2000, int(request.query.get("limit", 200))))
    except Exception:
        limit = 200
    state = await get_state()
    rows = sort_for_export(list(LIVE_ROWS))
    return web.json_response({
        "status": state["status"], "stage": state["stage"], "progress": state["progress"],
        **_table_payload(rows, limit),
    })


async def handle_results_rebuild(request):
    """Regenerate a results file from the raw scrape cache.

    The cache holds every scraped record, so results can always be rebuilt from it — useful if a
    run was interrupted before its upload step, or if a results file was lost. Phone number and
    loan amount come from the candidate list rather than KW388, so those columns are only filled
    if a matching list is currently loaded."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    day = safe_filename(body.get("date") or today_myt().isoformat())
    cache_file = os.path.join(CACHE_DIR, f"raw_{day}.json")
    if not os.path.exists(cache_file):
        available = sorted(f[4:-5] for f in os.listdir(CACHE_DIR)
                           if f.startswith("raw_") and f.endswith(".json"))
        return web.json_response(
            {"error": f"no scrape cache for {day}", "available": available}, status=404)
    try:
        raw = json.load(open(cache_file, encoding="utf-8"))
    except Exception as e:
        return web.json_response({"error": f"cache unreadable: {str(e)[:200]}"}, status=500)

    try:
        phones, amts, _cand = load_ctm()
    except Exception:
        phones, amts = {}, {}

    rows = [to_lead_payload(r) for r in all_scraped_rows(raw, phones, amts)]
    out_name = f"kw_leads_{day}.json"
    totals = _write_local_results(rows, out_name)
    enriched = sum(1 for r in rows if r.get("phone"))
    return web.json_response({
        "ok": True, "file": out_name, "cached": len(raw), "rows": len(rows),
        "withPhone": enriched, **totals,
        "warning": None if enriched else
                   "Phone and loan amount are blank — no candidate list is loaded, so those "
                   "columns could not be filled. Re-upload the list and rebuild to add them.",
    })


async def handle_results_list(request):
    files = []
    for f in sorted(os.listdir(OUTPUT_DIR), reverse=True):
        if f.endswith(".json"):
            p = os.path.join(OUTPUT_DIR, f)
            try:
                n = len(json.load(open(p, encoding="utf-8")))
            except Exception:
                n = 0
            files.append({"name": f, "rows": n, "size": os.path.getsize(p),
                          "modified": datetime.datetime.fromtimestamp(os.path.getmtime(p)).isoformat()})

    # Also surface the raw scrape caches. A run that was interrupted before its upload step leaves
    # data here and nothing in OUTPUT_DIR, which previously looked like "the scrape produced
    # nothing" — these can always be turned into results via /results/rebuild.
    caches = []
    for f in sorted(os.listdir(CACHE_DIR), reverse=True):
        if f.startswith("raw_") and f.endswith(".json"):
            p = os.path.join(CACHE_DIR, f)
            day = f[4:-5]
            try:
                n = len(json.load(open(p, encoding="utf-8")))
            except Exception:
                n = 0
            caches.append({"date": day, "records": n, "size": os.path.getsize(p),
                           "hasResults": any(x["name"] == f"kw_leads_{day}.json" for x in files)})
    return web.json_response({"files": files, "caches": caches})


async def handle_index(request):
    path = os.path.join(_here, "web", "index.html")
    if not os.path.exists(path):
        return web.Response(text="UI not installed (web/index.html missing)", status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-store, must-revalidate"})


async def handle_report_page(request):
    """Standalone, worker-facing Credit Report lookup page (separate from the control panel)."""
    path = os.path.join(_here, "web", "report.html")
    if not os.path.exists(path):
        return web.Response(text="report.html missing", status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-store, must-revalidate"})


# ---------------- HTTP API (internal Docker network only) ----------------
async def handle_status(request):
    return web.json_response(await get_state())

def _service_account_email():
    """The address the Google Sheet must be shared with. Surfacing it in /health and on the page
    removes the single most common setup failure: creating the key but never sharing the sheet."""
    try:
        with open(GSHEETS_CREDENTIALS, encoding="utf-8") as f:
            return json.load(f).get("client_email", "")
    except Exception:
        return ""


async def handle_health(request):
    """Config/plumbing snapshot — deliberately separate from /status, whose shape mirrors
    KwScrapeStatusDto in packages/shared and must not grow fields. Reports no secret values,
    only whether each one is set."""
    files = candidate_files()
    return web.json_response({
        "ok": True,
        "mode": "local-only (results written to disk, no backend)" if LOCAL_ONLY else "backend",
        "localOnly": LOCAL_ONLY,
        "apiBase": None if LOCAL_ONLY else API_BASE,
        "credentials": {
            "kw388": all([KW_USER, KW_PASS, KW_TOTP_SECRET]),
            "royalpay": all([RP_USER, RP_PASS, RP_TOTP_SECRET]),
            "backendAdmin": all([BACKEND_USER, BACKEND_PASS, BACKEND_PIN]),
        },
        "sheets": {
            "configured": bool(GSHEETS_SPREADSHEET_ID) and os.path.exists(GSHEETS_CREDENTIALS),
            "hasSpreadsheetId": bool(GSHEETS_SPREADSHEET_ID),
            "hasCredentialsFile": os.path.exists(GSHEETS_CREDENTIALS),
            "credentialsPath": GSHEETS_CREDENTIALS,
            "serviceAccountEmail": _service_account_email(),
            "autoExport": GSHEETS_AUTO,
            "tabPattern": GSHEETS_TAB_PATTERN,
            "nextTabName": now_myt().strftime(GSHEETS_TAB_PATTERN),
        },
        "kw388BaseUrl": KW_BASE_URL,
        "sharedSession": KW_SHARED_SESSION,
        "creditReportMode": CREDIT_REPORT_MODE,
        "schedule": ["%02d:%02d" % t for t in SCHEDULE],
        "sessionCaptured": bool(load_session()),
        "paths": {"candidates": CANDIDATES_PATH, "cache": CACHE_DIR,
                  "profile": PROFILE_DIR, "output": OUTPUT_DIR},
        "candidateFiles": files,
        "candidateFilesFound": len(files),
    })

async def handle_scrape(request):
    state = await get_state()
    if state["status"] == "running":
        return web.json_response({"status": "already_running", **state})
    try:
        body = await request.json()
    except Exception:
        body = {}  # no body (e.g. the in-app cron scheduler's bare POST) keeps today's behavior
    mode = body.get("mode") or "all"
    if mode not in ("all", "new", "old"):
        return web.json_response({"error": "mode must be 'all', 'new', or 'old'"}, status=400)
    limit = body.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except Exception:
            return web.json_response({"error": "limit must be an integer"}, status=400)
        if limit <= 0:
            return web.json_response({"error": "limit must be a positive integer"}, status=400)
    fast = bool(body.get("fast"))
    # Default true: "scrape" means get current data. Pass fresh=false only to continue an
    # interrupted run without re-fetching what it already got.
    fresh = body.get("fresh", True) is not False
    # Prefer the direct-API pipeline when available (much faster); else browser.
    runner = pick_runner()
    asyncio.create_task(runner(mode=mode, limit=limit, fast=fast, fresh=fresh))
    return web.json_response({"status": "started"})

async def handle_candidates_preview(request):
    try:
        _phones, _amts, cand = load_ctm()
    except Exception as e:
        return web.json_response({"error": f"Could not read candidate list: {str(e)[:200]}"}, status=502)
    total = len(cand)
    if total == 0:
        stored = read_manifest().get("files", [])
        if stored and not any(f.get("active", True) for f in stored):
            warning = (f"All {len(stored)} saved list(s) are switched off — tick at least one to "
                       "include it in the next scrape.")
        elif not stored and not os.path.exists(CANDIDATES_PATH):
            warning = "No candidate list uploaded yet."
        else:
            warning = ("Candidate list has 0 usable rows — check the uploaded file's tabs are "
                       "named with dates (e.g. \"307\", \"78\"), not \"Sheet1\"/other names")
        return web.json_response({
            "total": 0, "new": 0, "old": 0, "oldOverdueSkip": 0, "warning": warning,
        })
    try:
        known, overdue = await fetch_known_ics()
    except Exception as e:
        return web.json_response({"error": f"Could not check known customers against the backend: {str(e)[:200]}"}, status=502)
    old = sum(1 for _nm, ic in cand if cic(ic) in known)
    old_overdue_skip = sum(1 for _nm, ic in cand if cic(ic) in overdue)
    return web.json_response({
        "total": total, "new": total - old, "old": old, "oldOverdueSkip": old_overdue_skip, "warning": None,
    })

_UI_START_LOCK = asyncio.Lock()
UI_IDLE_CLOSE_S = int(os.environ.get("KW388_UI_IDLE_CLOSE_S", 600))


def _mem_view(m):
    """Member List row (compact) -> the `member` object the report page shows."""
    if not m:
        return {}
    return {"loanAmountActual": m.get("amount"), "onHandAmount": m.get("onHand"), "tenureDays": m.get("tenure"),
            "loanCreated": m.get("created"), "dueDate": _due_from(m.get("created"), m.get("tenure")),
            "loanEvent": m.get("event") or ""}


class NotAMember(RuntimeError):
    """The IC is not in KR883's Member List (same message/behaviour as before)."""


class UiLookupError(RuntimeError):
    def __init__(self, msg, stage=None):
        super().__init__(msg)
        self.stage = stage


async def ui_lookup(ic, lookup_id=None):
    """THE single credit-report entry point: the persistent UI provider (Member List -> Actions ->
    Credit report). Starts the browser on first use, then reuses it; concurrent callers queue on the
    provider's lock. Raises ReauthRequired when no valid session exists anywhere."""
    prov = get_ui_provider()
    async with _UI_START_LOCK:
        if not prov.alive:
            await prov.start()          # validates the session; syncs from the normal Chrome if needed
    return await prov.fetch(ic, lookup_id=lookup_id)


@audited_lookup("/credit-report")
async def handle_credit_report(request):
    """Full structured credit report for one IC (the report page's Search). ALWAYS the genuine UI
    workflow on the persistent dedicated browser: Member List -> IC -> Apply -> Actions -> Credit
    report -> popup -> extract. Lookups are serialized by the provider (one page); the browser starts
    on first use and closes after UI_IDLE_CLOSE_S idle (ui_idle_closer)."""
    ic = cic(request.query.get("ic"))
    if not ic:
        return web.json_response({"error": "invalid or missing ic"}, status=400)
    request["audit_ic"] = ic
    try:
        res = await ui_lookup(ic, request.get("lookup_id"))
    except ReauthRequired:
        request["audit_stage"] = "session"
        return web.json_response({"error": "MANUAL_REAUTH_REQUIRED: no valid KR883 session (log in to KR883 in your "
                                           "normal Chrome, keep a KR883 tab open, then search again)."}, status=502)
    except Exception as e:
        request["audit_stage"] = "browser_start"
        return web.json_response({"error": "UI lookup failed: " + str(e)[:160]}, status=502)
    if res["status"] == "not_member":
        return web.json_response({"found": False, "ic": ic, "source": "ui"}, status=404)
    if res["status"] != "ok":
        request["audit_stage"] = res.get("failedStage")
        return web.json_response({"error": "UI lookup failed: " + (res.get("error") or "unknown")[:160]}, status=502)
    d = res["report"]
    ui_mem = _mem_view(_member_pick(res.get("members") or [res["member"]], ic))

    if request.query.get("recent") in ("1", "true", "yes"):
        # Lightweight path for the CRM's "did this person pay today, and to which admin?" check.
        # Skips load_ctm (the ~2.5s candidate-list parse) and all history processing — just scans
        # the newest transactions. Same single backend call; there's no pagination to avoid (KW388
        # returns the whole log in one response), so this is about cutting our own work, not the
        # backend's. Returns in roughly backend latency (~1-2s).
        tl = d.get("transaction_log") or []
        today = today_myt().isoformat()
        last_pay, paid_today = None, []
        for t in tl:  # newest-first
            if (t.get("transaction_type") or "") != "Loan Repayment":
                continue
            dtp = _iso_to_myt(t.get("date"))
            iso = dtp.strftime("%Y-%m-%d") if dtp else (t.get("date") or "")
            entry = {"admin": t.get("admin") or "", "adminId": t.get("admin_identifier") or "",
                     "date": iso, "amount": t.get("credit") or t.get("debit") or 0}
            if iso == today:
                paid_today.append(entry)
                if last_pay is None:
                    last_pay = entry
            else:
                if last_pay is None:
                    last_pay = entry
                break  # reached an older repayment; today's (if any) are already collected
        # Who disbursed — newest loan overall, plus everything disbursed today. Each carries the
        # named admin or, when none, the platform/channel code. No amount: the API exposes none.
        last_disb, disbursed_today = None, []
        for l in (d.get("loan_details") or []):  # newest-first
            if l.get("loan_status") != "Disbursed":
                continue
            dtl = _iso_to_myt(l.get("created_on"))
            iso = dtl.strftime("%Y-%m-%d") if dtl else ""
            admin = l.get("admin") or ""
            entry = {"by": admin or (l.get("platform") or ""),
                     "type": "admin" if admin else ("platform" if l.get("platform") else ""),
                     "admin": admin, "platform": l.get("platform") or "",
                     "date": iso, "remark": l.get("remark") or ""}
            if last_disb is None:
                last_disb = entry
            if iso == today:
                disbursed_today.append(entry)
            elif last_disb is not None:
                break
        mem = ui_mem
        return web.json_response({
            "found": True, "ic": dash(ic),
            "name": (d.get("customer_details") or {}).get("name", ""),
            "paidToday": bool(paid_today),
            "paymentsToday": paid_today,
            "lastPayment": last_pay,
            "disbursedToday": bool(disbursed_today),
            "disbursementsToday": disbursed_today,
            "lastDisbursed": last_disb,
            "loanAmount": mem.get("loanAmountActual", ""),
            "tenureDays": mem.get("tenureDays", ""),
            "dueDate": mem.get("dueDate", ""),
            "loanEvent": mem.get("loanEvent", ""),
        })

    return await credit_report_payload(ic, d, mem=ui_mem,
                                       extra={"source": "ui", "uiMs": res["timings"].get("totalMs")})


async def credit_report_payload(ic, d, mem, extra=None):
    """The /credit-report JSON for one IC from a credit-report response `d` (as the popup loaded it)."""
    # Name / phone / loan amount from the uploaded list (the report has the name too).
    name, phone, amt = (d.get("customer_details") or {}).get("name", ""), "", ""
    try:
        phones, amts, cand = load_ctm()
        phone = phones.get(ic, "")
        amt = amts.get(ic, "")
        name = name or next((nm for nm, c in cand if cic(c) == ic), "")
    except Exception:
        pass

    shape = api_report_to_shape(d)
    sig = kw388_signals(shape["loans"], shape["txs"], shape["stats"])
    sig["period"] = str(median_days(recent5(shape["loans"], shape["txs"])) or "")

    return web.json_response({
        "found": True,
        "ic": dash(ic),
        "name": name,
        "phone": phone,
        "loanAmount": str(amt) if amt not in (None, "") else "",
        "customer": d.get("customer_details") or {},
        "loans": d.get("loan_details") or [],
        "transactions": d.get("transaction_log") or [],
        "blacklist": d.get("blacklist") or [],
        "member": mem,
        "signals": {
            "todayCount": sig["todayCount"], "totalCompleted": sig["totalCompleted"],
            "totalDisbursed": sig["totalDisbursed"], "overdueDays": sig["overdueDays"],
            "cardOverdueDays": sig["cardOverdueDays"], "period": sig["period"],
            "recentDetail": sig["recentDetail"], "disbursedByRemark": sig["disbursedByRemark"],
        },
        **(extra or {}),
    })


async def ui_idle_closer():
    """Release the dedicated profile when search has been idle (and no scrape is running), so tools
    like sync_session.py can use it. The next search re-opens it."""
    while True:
        await asyncio.sleep(60)
        prov = UI_PROVIDER
        if not (prov and prov.alive) or UI_RUN["paused"]:
            continue
        if (await get_state())["status"] == "running":
            continue
        if time.time() - (prov.last_used or 0) > UI_IDLE_CLOSE_S and not prov._lock.locked():
            print("ui browser idle - closing (reopens on next search)", file=sys.stderr)
            try:
                await prov.close()
            except Exception:
                pass


@audited_lookup("/check")
async def handle_check(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "expected JSON body with an ic field"}, status=400)
    ic = cic(body.get("ic"))
    if not ic:
        return web.json_response({"error": "invalid or missing ic"}, status=400)
    # Concurrent checks/searches queue on the UI provider (one page) instead of being refused.
    request["audit_ic"] = ic
    try:
        row = await check_one(ic, request.get("lookup_id"))
    except ReauthRequired:
        request["audit_stage"] = "session"
        return web.json_response({"error": "MANUAL_REAUTH_REQUIRED: no valid KR883 session (log in to KR883 in your "
                                           "normal Chrome, keep a KR883 tab open, then try again)."}, status=502)
    except NotAMember as e:
        request["audit_result"] = "NOT_MEMBER"
        return web.json_response({"error": str(e)[:300]}, status=502)
    except Exception as e:
        request["audit_stage"] = getattr(e, "stage", None) or "browser_start"
        return web.json_response({"error": str(e)[:300]}, status=502)

    # check_one returns only the KW388-derived signals. Name/phone/loan amount live in the
    # uploaded candidate list, so fill them in when this IC is on it - a lookup for someone not
    # on any list still works, just without those three fields.
    name, phone, amt = "", "", ""
    try:
        phones, amts, cand = load_ctm()
        phone = phones.get(ic, "")
        amt = amts.get(ic, "")
        name = next((nm for nm, c in cand if cic(c) == ic), "")
    except Exception:
        pass
    full = {"fullName": name, "icNumber": ic, "phone": phone,
            "loanAmount": str(amt) if amt not in (None, "") else "", **row}
    full["onCandidateList"] = bool(name)
    return web.json_response({
        **full,
        "display": {k: _fmt_cell(full.get(k)) for k, _l in SHEET_COLUMNS},
        "columns": [{"key": k, "label": l} for k, l in SHEET_COLUMNS],
        **{k: v for k, v in enrich_row(full).items() if k in ("topRemark", "openLoans")},
    })

async def handle_rp_status(request):
    return web.json_response(await get_rp_state())

async def handle_rp_scrape(request):
    state = await get_rp_state()
    if state["status"] == "running":
        return web.json_response({"status": "already_running", **state})
    asyncio.create_task(rp_run_pipeline())
    return web.json_response({"status": "started"})

# Two-tier HTTP Basic Auth (protects everything when exposed off-localhost via ngrok):
#   KW388_WORKER_AUTH="user:pass"  -> may open ONLY the credit-report page + its API.
#   KW388_ADMIN_AUTH="user:pass"   -> full access (control panel, scrape, downloads, export).
# Back-compat: KW388_WEB_AUTH, if set, is the worker credential. If no auth vars are set at all,
# the service runs open (local-only use).
WORKER_AUTH = (os.environ.get("KW388_WORKER_AUTH") or os.environ.get("KW388_WEB_AUTH", "")).strip()
ADMIN_AUTH = os.environ.get("KW388_ADMIN_AUTH", "").strip()
AUTH_ON = bool(WORKER_AUTH or ADMIN_AUTH)

# Paths a worker (report-only) may reach. Everything else is admin-only.
WORKER_PATHS = ("/report", "/credit-report")


# ---- cookie-based login (a real login page, not the browser popup) ----
# A signed cookie carries the role. Better than HTTP Basic Auth here because Basic Auth caches one
# credential per site and gives no way to switch from the worker login to the admin login - which
# is exactly the "stuck on 403" problem. The cookie is signed so it can't be forged, and lives
# long (30 days) so there's no auto-logout.
def _auth_secret():
    p = os.path.join(_data, ".auth_secret")
    try:
        if os.path.exists(p):
            return open(p, "rb").read()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        sec = base64.b64encode(os.urandom(32))
        with open(p, "wb") as f:
            f.write(sec)
        return sec
    except Exception:
        return b"kw388-fallback-secret-change-me"


AUTH_SECRET = _auth_secret()
AUTH_COOKIE = "kwauth"
AUTH_MAXAGE = 60 * 60 * 24 * 30  # 30 days


def make_auth_token(role):
    sig = hmac.new(AUTH_SECRET, role.encode(), hashlib.sha256).hexdigest()[:32]
    return role + "." + sig


def role_from_token(tok):
    if not tok or "." not in tok:
        return None
    role, _, sig = tok.partition(".")
    if role not in ("admin", "worker"):
        return None
    good = hmac.new(AUTH_SECRET, role.encode(), hashlib.sha256).hexdigest()[:32]
    return role if hmac.compare_digest(sig, good) else None


def _match_role(creds):
    """creds is 'user:pass'. Return the role it unlocks, or None."""
    if ADMIN_AUTH and creds == ADMIN_AUTH:
        return "admin"
    if WORKER_AUTH and creds == WORKER_AUTH:
        return "worker"
    return None


@web.middleware
async def auth_middleware(request, handler):
    if not AUTH_ON:
        return await handler(request)
    path = request.path
    if path in ("/login", "/logout"):
        return await handler(request)

    role = role_from_token(request.cookies.get(AUTH_COOKIE))
    worker_area = any(path == p or path.startswith(p + "/") for p in WORKER_PATHS)

    if role == "admin":
        return await handler(request)
    if role == "worker" and worker_area:
        return await handler(request)

    # Not authorized. Page requests -> redirect to the login form; API/other -> JSON 401/403.
    is_page = request.method == "GET" and (path == "/" or worker_area)
    if role == "worker" and not worker_area:
        # Logged-in worker reaching an admin area: send them to login to upgrade to admin.
        if is_page or path == "/":
            raise web.HTTPFound("/login?next=" + urllib.parse.quote(path))
        return web.json_response({"error": "Administrator access required."}, status=403)
    if is_page:
        raise web.HTTPFound("/login?next=" + urllib.parse.quote(path))
    return web.json_response({"error": "Login required."}, status=401)


LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in — KW388</title>
<style>
:root{--bg:#0f1115;--panel:#171a20;--border:#2a2f38;--text:#e8eaed;--muted:#9aa3af;--accent:#6d5efc;--err:#ff9a92;}
@media (prefers-color-scheme: light){:root{--bg:#eef1f6;--panel:#fff;--border:#e3e7ee;--text:#16181d;--muted:#6b7280;--accent:#4f46e5;--err:#b42318;}}
*{box-sizing:border-box}body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--bg);color:var(--text);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.card{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:34px 32px;width:340px;
box-shadow:0 12px 40px rgba(0,0,0,.18)}
h1{font-size:20px;margin:0 0 4px;letter-spacing:-.02em}.sub{color:var(--muted);font-size:13.5px;margin:0 0 22px}
label{display:block;font-size:12.5px;color:var(--muted);margin:14px 0 5px;text-transform:uppercase;letter-spacing:.04em}
input{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:10px;
padding:11px 13px;font-size:15px}
input:focus{outline:2px solid var(--accent);border-color:transparent}
button{width:100%;margin-top:22px;background:var(--accent);color:#fff;border:0;border-radius:10px;
padding:12px;font-size:15px;font-weight:700;cursor:pointer}button:hover{filter:brightness(1.08)}
.err{background:color-mix(in srgb,var(--err) 16%,transparent);color:var(--err);border-radius:10px;
padding:10px 12px;font-size:13.5px;margin-bottom:14px;__HIDE__}
.brand span{color:var(--accent)}
</style></head><body>
<form class="card" method="POST" action="/login">
<h1>KR<span>883</span> · Sign in</h1><p class="sub">Enter your username and password.</p>
<div class="err">Wrong username or password.</div>
<input type="hidden" name="next" value="__NEXT__">
<label>Username</label><input name="username" autofocus autocomplete="username">
<label>Password</label><input name="password" type="password" autocomplete="current-password">
<button type="submit">Sign in</button>
</form></body></html>"""


async def handle_login(request):
    nxt = request.query.get("next", "/")
    if request.method == "POST":
        data = await request.post()
        nxt = data.get("next") or "/"
        creds = (data.get("username") or "") + ":" + (data.get("password") or "")
        role = _match_role(creds)
        if role:
            # A worker may only land in the worker area.
            dest = nxt if (role == "admin" or any(nxt.startswith(p) for p in WORKER_PATHS)) else "/report"
            resp = web.HTTPFound(dest if dest.startswith("/") else "/")
            resp.set_cookie(AUTH_COOKIE, make_auth_token(role), max_age=AUTH_MAXAGE,
                            httponly=True, samesite="Lax", path="/")
            return resp
        html = LOGIN_HTML.replace("__HIDE__", "").replace("__NEXT__", _html_escape(nxt))
        return web.Response(text=html, content_type="text/html", status=401)
    html = LOGIN_HTML.replace("__HIDE__", "display:none").replace("__NEXT__", _html_escape(nxt))
    return web.Response(text=html, content_type="text/html")


async def handle_logout(request):
    resp = web.HTTPFound("/login")
    resp.del_cookie(AUTH_COOKIE, path="/")
    return resp


def _html_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


# Auto-scrape schedule, in Malaysia time (24h HH:MM, comma-separated). The service fires a full
# fast+fresh scrape at each of these times whenever it's running (it auto-starts at login), so no
# one has to press Scrape. A time is missed only if the PC is off/asleep then.
# Auto-scrape is OFF by default (empty). It was disabled after KR883 flagged bot activity — do not
# re-enable without the user explicitly asking. Set KW388_SCHEDULE=12:30,13:00,... to turn it back on.
SCHEDULE_RAW = os.environ.get("KW388_SCHEDULE", "")


def _parse_schedule(s):
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hh, mm = part.split(":")
            hh, mm = int(hh), int(mm)
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                out.append((hh, mm))
        except Exception:
            pass
    return sorted(set(out))


SCHEDULE = _parse_schedule(SCHEDULE_RAW)


async def scheduler_loop():
    """Fire a scrape at each scheduled Malaysia-time slot. Never double-fires a slot, and skips a
    slot if a scrape is already running."""
    fired = set()
    while True:
        try:
            now = now_myt()
            today = now.date().isoformat()
            for hh, mm in SCHEDULE:
                if now.hour == hh and now.minute == mm:
                    key = (today, hh, mm)
                    if key not in fired:
                        fired.add(key)
                        st = await get_state()
                        if st.get("status") != "running":
                            runner = pick_runner()
                            await set_state(lastLine="auto-scrape triggered (%02d:%02d MYT)" % (hh, mm))
                            asyncio.create_task(runner(mode="all", fast=True, fresh=True))
            fired = {k for k in fired if k[0] == today}  # keep only today's markers
        except Exception:
            pass
        await asyncio.sleep(20)


NGROK_DOMAIN_CFG = os.environ.get("NGROK_DOMAIN", "").strip()


async def ngrok_watchdog():
    """Keep the ngrok tunnel alive. If ngrok's local API (127.0.0.1:4040) stops answering, the
    agent has died - relaunch it detached, pinned to the reserved domain. Makes the public link
    self-heal within one check interval regardless of why ngrok stopped (crash, session teardown,
    a transient network/DNS blip that made it give up)."""
    if not NGROK_DOMAIN_CFG:
        return
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    exe = os.path.join(here, "tools", "bin", "ngrok.exe")
    await asyncio.sleep(30)  # give the boot-time launch a chance first
    while True:
        try:
            alive = False
            try:
                async with ClientSession() as s:
                    async with s.get("http://127.0.0.1:4040/api/tunnels",
                                     timeout=ClientTimeout(total=4)) as r:
                        alive = r.status == 200
            except Exception:
                alive = False
            if not alive and os.path.exists(exe):
                try:
                    subprocess.Popen(
                        [exe, "http", str(PORT), "--url=" + NGROK_DOMAIN_CFG,
                         "--log", os.path.join(_data, "ngrok.log")],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        | getattr(subprocess, "DETACHED_PROCESS", 0))
                    print("ngrok watchdog: tunnel was down, relaunched", file=sys.stderr)
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(120)


async def _on_startup(app):
    if CREDIT_REPORT_MODE == "ui":
        app["_ui_session"] = asyncio.create_task(ui_startup_session_check())
        app["_ui_idle"] = asyncio.create_task(ui_idle_closer())
    if SCHEDULE:
        app["_sched"] = asyncio.create_task(scheduler_loop())
        print("auto-scrape schedule (MYT): " + ", ".join("%02d:%02d" % t for t in SCHEDULE), file=sys.stderr)
    if NGROK_DOMAIN_CFG:
        app["_ngrok"] = asyncio.create_task(ngrok_watchdog())
        print("ngrok watchdog active for " + NGROK_DOMAIN_CFG, file=sys.stderr)
    # The periodic keeper rotates the account's tokens, which under KR883's one-session rule
    # evicts the human's browser. Off by default so sharing one account doesn't constantly kick
    # you out; the scraper still authenticates on-demand at scrape time. Set KW388_TOKEN_KEEPER=
    # true only when the scraper has its OWN dedicated KR883 account.
    if load_session() and _envflag("KW388_TOKEN_KEEPER", default=False):
        app["_token"] = asyncio.create_task(token_keeper())
        print("token keeper active (auto-refresh + auto-login)", file=sys.stderr)


# ---------------- Members page: scrape /api/members/ into a local snapshot ----------------
# Pages KR883 /api/members/ (100 per page), keeps EVERY field (not just the loan rollup) and saves it as
# data/output/members.json so the /members-list page can search/filter/download it. Paced like the
# per-IC scrape (KW_HUMAN_DELAY_MIN..MAX between page requests) so it doesn't hammer KR883.
MEMBERS_FILE = os.path.join(OUTPUT_DIR, "members.json")
MEMBERS_STATE = {"status": "idle", "page": 0, "pages": 0, "rows": 0, "count": 0, "error": "",
                 "startedAt": "", "finishedAt": ""}
_MEMBERS_TASK = None
_MEMBERS_CACHE = {"mtime": None, "rows": [], "scrapedAt": ""}


def _member_row(m):
    """Flatten one /api/members/ record into the columns the page shows."""
    def names(items):
        return ", ".join(str(a.get("username") or a.get("admin") or "") if isinstance(a, dict) else str(a)
                         for a in (items or []))
    return {
        "id": m.get("id"),
        "name": m.get("full_name") or "",
        "ic": cic(m.get("ic_number") or ""),
        "phone": m.get("phone_number") or "",
        "loanAmount": m.get("loan_amount") or "",
        "onHand": m.get("on_hand_amount") or "",
        "tenureDays": m.get("tenure_days"),
        "customerType": m.get("customer_type") or "",
        "event": m.get("event_display") or m.get("event") or "",
        "status": m.get("distribution_status") or "",
        "distributedAt": (m.get("distributed_at") or "")[:19],
        "createdAt": (m.get("created_at") or "")[:19],
        "groups": ", ".join(str(g) for g in (m.get("groups") or [])),
        "assignedTo": names(m.get("assignments")),
        "viewers": names(m.get("team_viewers")),
        "callNote": m.get("call_log_note") or "",
        "callAt": m.get("call_log_at") if isinstance(m.get("call_log_at"), str) else "",
        "callBy": m.get("call_log_by") if isinstance(m.get("call_log_by"), str) else "",
        "callCount": m.get("call_log_count") or 0,
    }


async def members_scrape_run(max_pages=0, start_page=1, from_date="", to_date="", replace=False,
                             delay_min=None, delay_max=None):
    """Scrape KR883 /api/members/ (100 per page). start_page + max_pages pick a slice of pages,
    from_date/to_date are KR883's own assigned-date filter. Rows merge into the saved snapshot by
    member id (so partial scrapes accumulate) unless replace=True."""
    st = MEMBERS_STATE
    dmin = KW_HUMAN_DELAY_MIN if delay_min is None else delay_min
    dmax = KW_HUMAN_DELAY_MAX if delay_max is None else delay_max
    st.update(status="running", page=0, pages=0, rows=0, count=0, error="", startedAt=datetime.datetime.now().isoformat(timespec="seconds"),
              finishedAt="", first=start_page, last=0)
    fresh = {}
    try:
        async with ClientSession(timeout=ClientTimeout(total=45)) as http:
            page, relogged, done = start_page, False, 0
            while True:
                headers, ok = _api_headers()
                if not ok:
                    raise RuntimeError("No API session. Import your KR883 session (tools/import_chrome_session.py).")
                params = {"page": page, "page_size": 100}
                if from_date: params["from_date"] = from_date
                if to_date: params["to_date"] = to_date
                async with http.get(KW_API_MEMBERS, params=params, headers=headers) as r:
                    if r.status == 401 and not relogged:
                        relogged = True
                        await ensure_api_token(force=True)
                        continue
                    if r.status == 404 and page > start_page:
                        break  # ran past the last page
                    if r.status != 200:
                        raise RuntimeError(f"KR883 members API returned {r.status} on page {page}")
                    d = await r.json()
                relogged = False
                for m in d.get("results") or []:
                    fresh[m.get("id")] = _member_row(m)
                total_pages = ((d.get("count") or 0) + 99) // 100
                last = min(total_pages, start_page + max_pages - 1) if max_pages else total_pages
                done += 1
                st.update(count=d.get("count") or 0, last=last, page=page, rows=len(fresh),
                          pages=max(0, last - start_page + 1), done=done)
                if not d.get("next") or page >= last:
                    break
                page += 1
                if dmax > 0:
                    await asyncio.sleep(random.uniform(dmin, dmax))
        merged = {} if replace else {r["id"]: r for r in members_load()[0]}
        merged.update(fresh)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        tmp = MEMBERS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"scrapedAt": datetime.datetime.now().isoformat(timespec="seconds"), "rows": list(merged.values())}, f, ensure_ascii=False)
        os.replace(tmp, MEMBERS_FILE)
        st["status"] = "done"
    except asyncio.CancelledError:
        st["status"] = "stopped"
        raise
    except Exception as e:
        st["status"], st["error"] = "error", str(e)[:200]
    finally:
        st["finishedAt"] = datetime.datetime.now().isoformat(timespec="seconds")


def members_load():
    try:
        mt = os.path.getmtime(MEMBERS_FILE)
    except OSError:
        return [], ""
    if _MEMBERS_CACHE["mtime"] != mt:
        with open(MEMBERS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        _MEMBERS_CACHE.update(mtime=mt, rows=d.get("rows") or [], scrapedAt=d.get("scrapedAt") or "")
    return _MEMBERS_CACHE["rows"], _MEMBERS_CACHE["scrapedAt"]


async def handle_members_page(request):
    path = os.path.join(_here, "web", "members.html")
    if not os.path.exists(path):
        return web.Response(text="members.html missing", status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-store, must-revalidate"})


def _int_q(q, key, default=0):
    try:
        return max(0, int(q.get(key) or default))
    except ValueError:
        return default


def _float_q(q, key):
    """Optional non-negative float query param (None when absent/invalid), capped at 60s."""
    try:
        return min(60.0, max(0.0, float(q.get(key))))
    except (TypeError, ValueError):
        return None


async def handle_members_scrape(request):
    global _MEMBERS_TASK
    if MEMBERS_STATE["status"] == "running":
        return web.json_response({"status": "already_running", **MEMBERS_STATE})
    q = request.query
    dmin, dmax = _float_q(q, "delay_min"), _float_q(q, "delay_max")
    if dmin is not None and dmax is not None and dmin > dmax:
        dmin, dmax = dmax, dmin
    _MEMBERS_TASK = asyncio.create_task(members_scrape_run(
        max_pages=_int_q(q, "pages"), start_page=max(1, _int_q(q, "start", 1)),
        from_date=q.get("from_date") or "", to_date=q.get("to_date") or "",
        replace=q.get("replace") in ("1", "true", "yes"),
        delay_min=dmin, delay_max=dmax))
    return web.json_response({"status": "started"})


async def handle_members_available(request):
    """Live count from KR883 (optionally for a date range): how many members / pages exist."""
    q = request.query
    headers, ok = _api_headers()
    if not ok:
        return web.json_response({"error": "No API session. Import your KR883 session."}, status=502)
    params = {"page": 1, "page_size": 100}
    if q.get("from_date"): params["from_date"] = q["from_date"]
    if q.get("to_date"): params["to_date"] = q["to_date"]
    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as http:
            async with http.get(KW_API_MEMBERS, params=params, headers=headers) as r:
                if r.status == 401:
                    await ensure_api_token(force=True)
                    headers, _ = _api_headers()
                    async with http.get(KW_API_MEMBERS, params=params, headers=headers) as r2:
                        status, d = r2.status, (await r2.json() if r2.status == 200 else None)
                else:
                    status, d = r.status, (await r.json() if r.status == 200 else None)
    except Exception as e:
        return web.json_response({"error": str(e)[:200]}, status=502)
    if status != 200 or d is None:
        return web.json_response({"error": f"KR883 members API returned {status}"}, status=502)
    count = d.get("count") or 0
    return web.json_response({"count": count, "pages": (count + 99) // 100, "pageSize": 100})


async def handle_members_clear(request):
    if MEMBERS_STATE["status"] == "running":
        return web.json_response({"error": "A scrape is running - stop it first."}, status=409)
    try:
        os.remove(MEMBERS_FILE)
    except OSError:
        pass
    _MEMBERS_CACHE.update(mtime=None, rows=[], scrapedAt="")
    return web.json_response({"status": "cleared"})


async def handle_members_stop(request):
    if _MEMBERS_TASK and not _MEMBERS_TASK.done():
        _MEMBERS_TASK.cancel()
    return web.json_response({"status": "stopping"})


async def handle_members_status(request):
    rows, at = members_load()
    return web.json_response({**MEMBERS_STATE, "saved": len(rows), "savedAt": at})


def _myt_date(iso):
    """'2026-09-25T02:00:02' (UTC, as stored) -> '2026-09-25' in Malaysia time."""
    try:
        return (datetime.datetime.fromisoformat(iso[:19]) + _MYT).date().isoformat()
    except Exception:
        return ""


def _members_filtered(q):
    rows, at = members_load()
    text = (q.get("q") or "").strip().lower()
    name_q, ic_q = (q.get("name") or "").strip().lower(), re.sub(r"\D", "", q.get("ic") or "")
    phone_q = re.sub(r"\D", "", q.get("phone") or "")
    ev, ty, stt, grp = q.get("event") or "", q.get("type") or "", q.get("status") or "", q.get("group") or ""
    adm, d_from, d_to = q.get("admin") or "", q.get("from_date") or "", q.get("to_date") or ""
    out = []
    for r in rows:
        if ev and r["event"] != ev: continue
        if ty and r["customerType"] != ty: continue
        if stt and r["status"] != stt: continue
        if grp and grp not in r["groups"].split(", "): continue
        if adm and adm not in r["viewers"].split(", "): continue
        if name_q and name_q not in r["name"].lower(): continue
        if ic_q and ic_q not in r["ic"]: continue
        if phone_q and phone_q not in re.sub(r"\D", "", r["phone"]): continue
        if d_from or d_to:
            day = _myt_date(r["distributedAt"])
            if not day or (d_from and day < d_from) or (d_to and day > d_to): continue
        if text:
            digits = re.sub(r"\D", "", text)
            hay = (r["name"] + " " + r["groups"] + " " + r["viewers"] + " " + r["callNote"]).lower()
            num = len(digits) >= 3 and (digits in r["ic"] or digits in re.sub(r"\D", "", r["phone"]))
            if text not in hay and not num:
                continue
        out.append(r)
    return out, rows, at


async def handle_members_data(request):
    q = request.query
    out, rows, at = _members_filtered(q)
    sort = q.get("sort") or "createdAt"
    if rows and sort in rows[0]:
        out.sort(key=lambda r: str(r.get(sort) if r.get(sort) is not None else ""), reverse=(q.get("dir", "desc") == "desc"))
    try:
        size = max(1, min(int(q.get("size", 50)), 500)); page = max(1, int(q.get("page", 1)))
    except ValueError:
        size, page = 50, 1
    facets = {
        "events": sorted({r["event"] for r in rows if r["event"]}),
        "types": sorted({r["customerType"] for r in rows if r["customerType"]}),
        "statuses": sorted({r["status"] for r in rows if r["status"]}),
        "groups": sorted({g for r in rows for g in r["groups"].split(", ") if g}),
        "admins": sorted({v for r in rows for v in r["viewers"].split(", ") if v}),
    }
    return web.json_response({"total": len(rows), "matched": len(out), "page": page, "size": size,
                              "savedAt": at, "rows": out[(page - 1) * size: page * size], "facets": facets})


async def handle_members_download(request):
    import io, csv
    q = request.query
    out, _rows, _at = _members_filtered(q)
    fmt = (q.get("format") or "xlsx").lower()
    cols = [("name", "Name"), ("ic", "IC"), ("phone", "Phone"), ("loanAmount", "Loan amount"), ("onHand", "On-hand"),
            ("tenureDays", "Tenure (d)"), ("customerType", "Type"), ("event", "Event"), ("status", "Status"),
            ("distributedAt", "Distributed at (UTC)"), ("createdAt", "Created at (UTC)"), ("groups", "Groups"),
            ("viewers", "Team viewers"), ("assignedTo", "Assigned to"), ("callNote", "Call note"),
            ("callAt", "Call at"), ("callBy", "Call by"), ("callCount", "Call count")]
    stem = f"kr883_members_{today_myt().isoformat()}"
    if fmt == "json":
        body = json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8")
        return web.Response(body=body, headers={"Content-Disposition": f'attachment; filename="{stem}.json"',
                                                "Content-Type": "application/json; charset=utf-8"})
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([label for _k, label in cols])
        for r in out:
            w.writerow([r.get(k, "") for k, _label in cols])
        return web.Response(body=("\ufeff" + buf.getvalue()).encode("utf-8"), headers={
            "Content-Disposition": f'attachment; filename="{stem}.csv"', "Content-Type": "text/csv; charset=utf-8"})
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Members"
    ws.append([label for _k, label in cols])
    for r in out:
        ws.append([r.get(k, "") for k, _label in cols])
    buf = io.BytesIO(); wb.save(buf)
    return web.Response(body=buf.getvalue(), headers={
        "Content-Disposition": f'attachment; filename="{stem}.xlsx"',
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})


def make_app():
    app = web.Application(middlewares=[auth_middleware])
    app.on_startup.append(_on_startup)
    app.router.add_get("/", handle_index)
    app.router.add_route("*", "/login", handle_login)
    app.router.add_get("/logout", handle_logout)
    app.router.add_get("/report", handle_report_page)
    app.router.add_get("/members-list", handle_members_page)
    app.router.add_post("/members/scrape", handle_members_scrape)
    app.router.add_post("/members/stop", handle_members_stop)
    app.router.add_get("/members/available", handle_members_available)
    app.router.add_post("/members/clear", handle_members_clear)
    app.router.add_get("/members/status", handle_members_status)
    app.router.add_get("/members/data", handle_members_data)
    app.router.add_get("/members/download", handle_members_download)
    app.router.add_get("/status", handle_status)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/candidates/upload", handle_candidates_upload)
    app.router.add_get("/candidates/files", handle_candidates_list)
    app.router.add_delete("/candidates/files/{name}", handle_candidates_delete)
    app.router.add_post("/candidates/files/{name}/toggle", handle_candidates_toggle)
    app.router.add_get("/results", handle_results_list)
    app.router.add_get("/results/live", handle_results_live)
    app.router.add_get("/results/{name}/preview", handle_results_preview)
    app.router.add_get("/results/{name}/download", handle_results_download)
    app.router.add_post("/results/rebuild", handle_results_rebuild)
    app.router.add_post("/export/sheets", handle_export_sheets)
    app.router.add_post("/scrape", handle_scrape)
    app.router.add_get("/scrape/ui-status", handle_ui_status)
    app.router.add_post("/scrape/resume", handle_ui_resume)
    app.router.add_post("/scrape/reauth", handle_ui_reauth)
    app.router.add_get("/candidates/preview", handle_candidates_preview)
    app.router.add_post("/check", handle_check)
    app.router.add_get("/credit-report", handle_credit_report)
    app.router.add_get("/royalpay/status", handle_rp_status)
    app.router.add_post("/royalpay/scrape", handle_rp_scrape)
    return app

def _already_running():
    """True if something is already serving our port. Autostart plus a manual launch would
    otherwise leave a second process that fails to bind and dies with a stack trace in the log -
    noise that looks like a real fault."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def main():
    if _already_running():
        print(f"credit report tool already running on port {PORT} - nothing to do.", file=sys.stderr)
        return 1
    bind = os.environ.get("KW388_BIND", "0.0.0.0")      # 127.0.0.1 = this computer only
    print(f"[{now_iso()}] credit report tool service listening on {bind}:{PORT}", file=sys.stderr)
    if LOCAL_ONLY:
        print(f"  mode: LOCAL_ONLY — results written to {OUTPUT_DIR}, nothing posted to a backend",
              file=sys.stderr)
    else:
        print(f"  mode: backend — posting results to {API_BASE}", file=sys.stderr)
    print(f"  candidates: {CANDIDATES_PATH} ({len(candidate_files())} file(s) found)", file=sys.stderr)
    web.run_app(make_app(), host=bind, port=PORT, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
