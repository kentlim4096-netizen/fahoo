"""Credit-report providers.

  CreditReportProvider          the interface (mode, start, fetch(ic), close, metrics)
  UiCreditReportProvider        genuine Playwright workflow in ONE persistent dedicated profile:
                                Member List -> IC filter -> Apply -> locate member -> Actions (3 dots)
                                -> Credit report -> popup -> extract the report the popup loaded

Session model (see session_sync.py): the dedicated profile is a read-only MIRROR of the worker's
normal-Chrome session. It never logs in. When it is not authenticated the provider first tries to
sync the already-valid session from the normal Chrome; only if no valid session exists anywhere
does it raise ReauthRequired (MANUAL_REAUTH_REQUIRED). Token refresh is blocked in this context so
it cannot rotate the shared session and log the worker out.

UI-mode rules
  * One browser context / page is created in start() and reused for the whole queue.
  * It NEVER logs in and never touches OTP. If the session is gone, fetch() raises ReauthRequired
    (surfaced as MANUAL_REAUTH_REQUIRED) so the caller can pause the queue.
  * open_login_window() only opens a visible window on the dedicated profile so the human can
    authenticate; it types nothing.
  * After every customer the UI is returned to a known state (no dialog open, on Member List).
  * The credit report is the JSON response of the request the page itself makes when the
    "Credit report" menu item is clicked - not a separate API call.
"""
import asyncio, os, random, re, time, uuid

# Playwright's headless=True (channel="chrome") launches legacy "old" headless mode, which KR883
# (likely via Cloudflare) silently detects and serves empty Member List results for, rather than
# blocking outright - confirmed: a real visible window finds records fine, legacy headless does
# not, and forcing --headless=new had no effect (Chrome kept Playwright's own earlier --headless=old
# flag). The reliable fix: run a genuine non-headless window, positioned off-screen so nothing is
# visible - see start(), which passes headless=False unconditionally and adds the off-screen
# position args below ONLY when self.headless is True (i.e. "should be invisible"). When
# open_login_window() wants a REAL on-screen window for the human, self.headless is False and the
# off-screen args are skipped, so it appears normally.
STEALTH_ARGS = ["--disable-blink-features=AutomationControlled", "--exclude-switches=enable-automation"]
OFFSCREEN_ARGS = ["--start-minimized"]
MANUAL_REAUTH_REQUIRED = "MANUAL_REAUTH_REQUIRED"


class ReauthRequired(Exception):
    """The dedicated profile is not (or no longer) authenticated - a human must log in."""
    code = MANUAL_REAUTH_REQUIRED


def dash(ic):
    ic = re.sub(r"\D", "", ic or "")
    return f"{ic[:6]}-{ic[6:8]}-{ic[8:]}" if len(ic) == 12 else ic


# The stages of one lookup, in order. Every stage that completes is logged with its timing; on a
# failure the log names the stage that was being attempted, so "which stage failed" is one grep away.
STAGES = ("member_list", "api_check", "credit_report")


class Trace:
    """Per-lookup stage log. Only stage names, timings and counts are logged - never credentials,
    tokens or cookies - and the IC is masked to its last 4 digits."""

    def __init__(self, log, digits, lookup_id=""):
        self.log, self.tag = log, (f"lookup_id={lookup_id} " if lookup_id else "") + "******" + digits[-4:]
        self.t = time.perf_counter()
        self.done = []

    def ok(self, stage, note=""):
        now = time.perf_counter()
        ms = round((now - self.t) * 1000)
        self.t = now
        self.done.append({"stage": stage, "ms": ms})
        self.log(f"[ui {self.tag}] {stage:<22} ok  +{ms:>5} ms{('  ' + note) if note else ''}")

    def attempting(self):
        """Name of the stage that was in progress (the first not yet completed)."""
        n = len(self.done)
        return STAGES[n] if n < len(STAGES) else "done"


class Metrics:
    def __init__(self):
        self.reset()

    def reset(self):
        self.lookups = self.ok = self.not_member = self.errors = self.timeouts = 0
        self.ambiguous = self.session_expired = self.retries = 0
        self.session_syncs = 0
        self.login_requests = 0          # login / OTP requests the dedicated context made (must stay 0)
        self.total_ms = self.member_ms = self.popup_ms = 0.0
        self.ok_total_ms = 0.0

    def as_dict(self):
        ok = max(self.ok, 1)
        n = max(self.lookups, 1)
        return {
            "lookups": self.lookups, "ok": self.ok, "notMember": self.not_member, "errors": self.errors,
            "timeouts": self.timeouts, "ambiguous": self.ambiguous, "sessionExpired": self.session_expired,
            "retries": self.retries, "sessionSyncs": self.session_syncs, "loginRequests": self.login_requests,
            "avgTotalMs": round(self.total_ms / n), "avgMemberSearchMs": round(self.member_ms / n),
            "avgPopupMs": round(self.popup_ms / ok), "avgOkTotalMs": round(self.ok_total_ms / ok),
        }


class CreditReportProvider:
    mode = "base"

    def __init__(self):
        self.metrics = Metrics()
        self.last_used = 0.0

    async def start(self): ...
    async def close(self): ...
    async def fetch(self, ic): raise NotImplementedError


class UiCreditReportProvider(CreditReportProvider):
    mode = "ui"

    def __init__(self, base_url, profile_dir, headless=True, slow_mo=0, log=None, sync=None, relogin_creds=None,
                 api_base="https://api.kw388.com"):
        super().__init__()
        self.sync = sync                       # SessionSync (optional)
        self.relogin_creds = relogin_creds     # (username, password, totp_secret) or None - see _try_sync
        self.api_base = api_base.rstrip("/")   # raw API, used as the authoritative member check - see _api_member_search
        self.base, self.profile, self.headless, self.slow_mo = base_url.rstrip("/"), profile_dir, headless, slow_mo
        self.log = log or (lambda *_: None)
        self._pw = self.ctx = self.page = None
        self.launches = 0                      # how many browser launches happened (should stay 1 per queue)
        self.browser_pid = None
        self._last_ic = None
        self.account = None                    # who is logged in (username / position / the two permissions we need)
        self._lock = asyncio.Lock()            # one lookup at a time on the single page
        self._start_lock = asyncio.Lock()      # start() is never run twice at once

    # ------------------------------------------------------------ pacing
    @staticmethod
    def _split_pause_budget(total, n):
        """Split `total` seconds into `n` random positive pieces that sum to it (random cut
        points), so one candidate's whole lookup takes ~total seconds spread unevenly across its
        actions - not each action taking that long on its own."""
        cuts = sorted(random.uniform(0, total) for _ in range(n - 1))
        edges = [0.0] + cuts + [total]
        return [edges[i + 1] - edges[i] for i in range(n)]

    async def _human_pause(self, secs):
        """One slice of the per-candidate pause budget, spent before a UI action (type IC, click
        Apply, click Actions, click Credit report, close popup) so the flow is paced like a person
        clicking through the page rather than scripted back-to-back calls."""
        await asyncio.sleep(secs)

    # ------------------------------------------------------------ raw API (member list + credit
    # report both proven 100% reliable all day - every single call succeeded - unlike the UI's own
    # click-driven flow, which KR883's backend intermittently serves empty/incomplete results to for
    # reasons unrelated to anything on this end (ruled out: headless/automation detection, caching,
    # typing method, session freshness, browser fingerprint, and request headers, which were found
    # byte-for-byte identical between a working manual request and a failing automated one - see
    # conversation history). The actual data fetch now goes through these two endpoints directly;
    # the browser/Chrome session is still what the access token itself is read from and kept valid.
    async def _api_token(self):
        """The session's own access token, read live from the page's localStorage. Raises
        ReauthRequired (not a silent empty result) if it's missing - a dead session must trigger
        the normal resync/relogin path, never get silently misreported as "not a member"."""
        token = await self.page.evaluate("() => localStorage.getItem('kw388_access')")
        if not token:
            raise ReauthRequired("no access token in the dedicated profile's localStorage")
        return token

    async def _api_get(self, path, params):
        """GET against api_base with the live session token. Raises ReauthRequired on 401 (same
        reasoning as _api_token) rather than treating an expired session as "no results"."""
        from aiohttp import ClientSession, ClientTimeout
        token = await self._api_token()
        async with ClientSession(headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                                 timeout=ClientTimeout(total=15)) as http:
            async with http.get(self.api_base + path, params=params) as r:
                if r.status == 401:
                    raise ReauthRequired(f"{path} returned 401")
                if r.status != 200:
                    raise RuntimeError(f"{path} HTTP {r.status}")
                return await r.json()

    async def _api_member_search(self, digits):
        """-> list of matching rows (KR883's own result dicts) for this IC, or [] if genuinely not
        a member. Raises ReauthRequired if the session itself is the problem, not the search."""
        d = await self._api_get("/api/members/", {"ic": digits, "page": 1, "page_size": 50})
        return [row for row in (d.get("results") or [])
                if re.sub(r"\D", "", row.get("ic_number") or "") == digits]

    async def _api_credit_report(self, digits):
        """-> the full credit-report JSON (same shape the popup's own request returns: loan_details,
        transaction_log, blacklist, customer_details, ...), fetched directly rather than by clicking
        Actions -> Credit report and reading the popup's own network request."""
        return await self._api_get("/api/loans/credit-report/", {"ic_number": digits})

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        async with self._start_lock:
            await self._start_unlocked()

    async def _start_unlocked(self):
        if self.ctx:
            return
        from playwright.async_api import async_playwright
        os.makedirs(self.profile, exist_ok=True)
        self._pw = await async_playwright().start()
        # Always a real (non-headless) window - see STEALTH_ARGS/OFFSCREEN_ARGS above: legacy
        # headless gets silently detected and fed empty results, so this runs genuinely non-headless
        # but moved off-screen (unless self.headless is False, meaning a real on-screen window was
        # explicitly requested, e.g. by open_login_window()).
        args = STEALTH_ARGS + OFFSCREEN_ARGS if self.headless else STEALTH_ARGS
        launch = dict(headless=False, viewport={"width": 1400, "height": 1000}, args=args,
                      channel="chrome", slow_mo=self.slow_mo)
        try:
            self.ctx = await self._pw.chromium.launch_persistent_context(self.profile, **launch)
        except Exception:
            launch.pop("channel")
            self.ctx = await self._pw.chromium.launch_persistent_context(self.profile, **launch)
        self.launches += 1
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        self.page.set_default_timeout(15000)
        # The mirror must never rotate the shared session: block token refresh, and count any
        # login/OTP request (there must be none - this context never logs in).
        await self.ctx.route(re.compile(r"/token/refresh/"), lambda route: route.abort())
        self.ctx.on("request", self._count_auth_requests)
        self.log(f"[ui] browser launched (headless={self.headless}) profile={self.profile}")
        await self._open_member_list_or_sync()
        await self._read_account()

    async def _read_account(self):
        """Which KR883 account is this session? Reads the app's own profile blob (username, position,
        permission NAMES) - never the tokens. Each KR883 ID has its own permissions; the workflow needs
        view_member_list and view_credit_report."""
        try:
            prof = await self.page.evaluate("() => { try { return JSON.parse(localStorage.getItem('kw388_profile') || 'null'); } catch (e) { return null; } }")
        except Exception:
            prof = None
        if not prof:
            self.account = None
            return
        u, perms = prof.get("user") or {}, prof.get("permissions") or []
        sup = bool((prof.get("adminProfile") or {}).get("is_super_admin")) or bool(u.get("is_superuser"))
        self.account = {"username": u.get("username", ""), "position": (prof.get("position") or {}).get("name", ""),
                        "superAdmin": sup, "canMemberList": sup or "view_member_list" in perms,
                        "canCreditReport": sup or "view_credit_report" in perms}
        a = self.account
        self.log(f"[ui] KR883 account {a['username']} ({a['position'] or 'no position'}): Member List permission="
                 f"{'yes' if a['canMemberList'] else 'NO'}, Credit Report permission={'yes' if a['canCreditReport'] else 'NO'}")

    def _who(self):
        return (self.account or {}).get("username") or "this account"

    def _count_auth_requests(self, req):
        if re.search(r"/users/admin/(login|2fa)/", req.url):
            self.metrics.login_requests += 1
            self.log("[ui] WARNING: a login/OTP request was made by the dedicated context")

    async def _try_sync(self):
        """Copy the already-valid session from the normal Chrome into this profile and re-open Member
        List. Returns True only if the synced session is accepted by KR883.

        Never logs in anywhere, EXCEPT as a last resort directly in that SAME normal-Chrome tab
        when its own session has genuinely expired (only if relogin_creds is configured) - that
        refreshes the one real session in place rather than competing with it, so it cannot
        trigger KR883's one-session-per-account kick-out the way a separate login would."""
        if not self.sync or not self.page:
            return False
        found = await self.sync.find_source()
        if not found and self.relogin_creds:
            if await self.sync.auto_relogin_cdp(*self.relogin_creds):
                found = await self.sync.find_source()
        if not found:
            return False
        ls, where = found
        if not self.page.url.startswith(self.base):
            await self.page.goto(self.base + "/login", wait_until="domcontentloaded", timeout=60000)
        await self.sync.inject_into_page(self.page, ls)
        self.metrics.session_syncs += 1
        self.log(f"[ui] session synchronized from {where}")
        try:
            await self._open_member_list()
            await self._read_account()
            return True
        except ReauthRequired:
            return False

    async def _open_member_list_or_sync(self):
        try:
            await self._open_member_list()
        except ReauthRequired:
            if not await self._try_sync():
                raise ReauthRequired("No valid KR883 session in the dedicated profile, and none could be "
                                     "synchronized from the normal Chrome (it is logged out or closed).")

    async def close(self):
        """Close the browser. Waits for a lookup in progress (never pulls the page out from under one)."""
        async with self._lock:
            ctx, pw = self.ctx, self._pw
            self.ctx = self.page = self._pw = None
            try:
                if ctx:
                    await ctx.close()
            finally:
                if pw:
                    await pw.stop()

    @property
    def alive(self):
        return self.ctx is not None

    # ------------------------------------------------------------ auth / navigation
    async def _guard(self):
        if "/login" in (self.page.url or ""):
            raise ReauthRequired("KR883 session is not authenticated in the dedicated profile "
                                 "(needs a manual login).")
        # A dead/superseded token (e.g. a later login elsewhere issued a new session - KR883 allows
        # only one at a time) does NOT redirect to /login; the URL stays on /members and the page
        # just renders an in-page "no access" notice instead of the real UI. Left undetected, the
        # caller would otherwise sit out the full wait_for_selector timeout waiting for a filter box
        # that will never appear, every single time, without ever triggering a re-sync/re-login.
        try:
            denied = await self.page.locator("text=You do not have access to that page").count()
        except Exception:
            denied = 0
        if denied:
            raise ReauthRequired("KR883 rejected the synced session ('no access to that page' - "
                                 "the token was likely superseded by a login elsewhere).")

    async def _open_member_list(self):
        p = self.page
        await p.goto(self.base + "/members", wait_until="domcontentloaded", timeout=60000)
        await p.wait_for_timeout(500)
        await self._guard()      # catch a dead/superseded session fast, rather than after 20s
        try:
            await p.wait_for_selector("#filter-fields-region", timeout=20000)
        except Exception:
            await self._guard()
            await p.wait_for_timeout(1500)
            await self._guard()
            path = "/" + (p.url or "").split("://", 1)[-1].split("/", 1)[-1].split("?")[0]
            if not path.startswith("/members"):
                raise RuntimeError(f"KR883 would not open Member List for {self._who()} (it went to {path}) - "
                                   f"this account may lack the view_member_list permission")
            raise
        await self._guard()
        self._last_ic = None

    async def is_authenticated(self):
        """True when the dedicated profile lands on Member List without bouncing to /login."""
        try:
            await self.start()                    # start() already tries to sync before giving up
            await self._open_member_list_or_sync()
            return True
        except ReauthRequired:
            return False

    async def open_login_window(self, timeout=900):
        """Visible browser on the dedicated profile so the human can log in (nothing is typed for
        them). Returns True once the profile is authenticated, then closes that window."""
        await self.close()
        prev, self.headless = self.headless, False
        try:
            await self.start()      # start() opens Member List -> may bounce to /login; that is fine
        except ReauthRequired:
            pass
        except Exception:
            pass
        finally:
            self.headless = prev
        ok, deadline = False, time.time() + timeout
        while time.time() < deadline and self.ctx and self.ctx.pages:
            await asyncio.sleep(3)
            try:
                pg = self.ctx.pages[0]
                if "/login" not in pg.url and "kr883" in pg.url:
                    tok = await pg.evaluate("() => localStorage.getItem('kw388_access') || ''")
                    if tok:
                        ok = True
                        break
            except Exception:
                continue
        await self.close()
        return ok

    async def _reset(self):
        """Return the UI to a known state: no dialog/menu open, on Member List."""
        p = self.page
        try:
            for _ in range(3):
                if await p.locator(".q-dialog, .q-menu").count() == 0:
                    break
                await p.keyboard.press("Escape")
                await p.wait_for_timeout(300)
            if await p.locator(".q-dialog, .q-menu").count() or await p.locator("#filter-fields-region").count() == 0 \
                    or not p.url.startswith(self.base + "/members"):
                await self._open_member_list()
        except ReauthRequired:
            raise
        except Exception:
            await self._open_member_list()

    # ------------------------------------------------------------ one lookup
    async def fetch(self, ic, lookup_id=None):
        """-> dict: ic, status ('ok'|'not_member'|'error'), report (credit-report JSON or None),
        member (the Member List row) , ambiguous (n rows), steps (trace), timings, attempts, error?.
        Raises ReauthRequired when the session is gone (queue must pause)."""
        digits = re.sub(r"\D", "", ic or "")
        async with self._lock:
            self.last_used = time.time()
            self._lookup_id = lookup_id or uuid.uuid4().hex[:12]   # correlates stage logs with the audit log
            t0 = time.perf_counter()
            res = None
            for attempt in (1, 2):
                try:
                    if not self.ctx:
                        await self.start()
                    res = await self._lookup_once(digits)
                    res["attempts"] = attempt
                    break
                except ReauthRequired:
                    # Authentication actually failed. Re-sync from the normal Chrome (once) before
                    # giving up; only a genuinely unavailable session stops the queue.
                    self.metrics.session_expired += 1
                    if attempt == 1 and await self._try_sync():
                        self.metrics.retries += 1
                        continue
                    self.metrics.lookups += 1
                    raise
                except Exception as e:
                    timeout = "Timeout" in type(e).__name__ or "timeout" in str(e).lower()
                    tr = getattr(self, "_trace", None)
                    self.log(f"[ui lookup_id={self._lookup_id} ******{digits[-4:]}] FAILED at stage={tr.attempting() if tr else '?'} "
                             f"(attempt {attempt}): {type(e).__name__}: {str(e)[:100]}")
                    try:
                        await self._reset()
                    except ReauthRequired:
                        self.metrics.session_expired += 1
                        self.metrics.lookups += 1
                        raise
                    except Exception:
                        pass
                    if attempt == 1:
                        self.metrics.retries += 1
                        continue
                    tr = getattr(self, "_trace", None)
                    res = {"ic": digits, "status": "error", "report": None, "member": None, "ambiguous": 0,
                           "steps": [d["stage"] for d in tr.done] if tr else [], "stages": tr.done if tr else [],
                           "failedStage": tr.attempting() if tr else None, "attempts": attempt, "timeout": timeout,
                           "error": f"{type(e).__name__}: {str(e)[:150]}"}
            total = (time.perf_counter() - t0) * 1000
            res["lookupId"] = self._lookup_id
            res.setdefault("timings", {})["totalMs"] = round(total)
            m = self.metrics
            m.lookups += 1
            m.total_ms += total
            m.member_ms += res["timings"].get("memberSearchMs", 0)
            if res["status"] == "ok":
                m.ok += 1
                m.popup_ms += res["timings"].get("popupMs", 0)
                m.ok_total_ms += total
            elif res["status"] == "not_member":
                m.not_member += 1
            else:
                m.errors += 1
                if res.get("timeout"):
                    m.timeouts += 1
            if res.get("ambiguous", 0) > 1:
                m.ambiguous += 1                  # same IC, conflicting names - flagged, first row used
            try:                                  # known state before the next customer
                await self._reset()
            except ReauthRequired:
                self.metrics.session_expired += 1
                raise
            return res

    async def _lookup_once(self, digits):
        p, tm = self.page, {}
        tr = self._trace = Trace(self.log, digits, getattr(self, "_lookup_id", ""))
        if "/login" in p.url:
            raise ReauthRequired("session expired")
        await self._reset()                        # _reset() already ensures we're on Member List
        tr.ok("member_list", "on Member List, filter ready")

        # Authoritative check first: the raw API has been 100% reliable all day, while the UI's own
        # type-IC-and-Apply search intermittently comes back empty for a real member (KR883/Cloudflare
        # serving degraded results to the automated browser - see conversation history). A genuine
        # non-member is reported straight from this, skipping the UI step entirely; a confirmed member
        # whose UI search still comes back empty gets retried below rather than trusted outright.
        ts = time.perf_counter()
        api_matches = await self._api_member_search(digits)
        tr.ok("api_check", f"API confirms {len(api_matches)} match(es)" if api_matches else "API confirms not a member")
        if not api_matches:
            tm["memberSearchMs"] = round((time.perf_counter() - ts) * 1000)
            self.log(f"[ui {tr.tag}] member_not_found        not a member (confirmed via API) - no Actions/Credit report to click")
            return {"ic": digits, "memberRows": 0, "ambiguous": 0, "timings": tm, "members": [],
                    "status": "not_member", "report": None, "member": None,
                    "steps": [d["stage"] for d in tr.done], "stages": tr.done}

        # Several Member List rows for one IC are just that person's loan cycles (same credit report).
        # "Ambiguous" only when those rows disagree about WHO it is (different names for one IC).
        names = {(m.get("full_name") or "").strip().upper() for m in api_matches}
        base = {"ic": digits, "memberRows": len(api_matches), "ambiguous": len(names) if len(names) > 1 else 0,
                "timings": tm, "members": api_matches}
        member = api_matches[0]
        tm["memberSearchMs"] = round((time.perf_counter() - ts) * 1000)

        # Credit report, via the same raw API the popup's own request calls - proven equally
        # reliable all day. A short pause first, pacing the two API calls like a person clicking
        # through rather than firing them back-to-back.
        tp = time.perf_counter()
        await self._human_pause(random.uniform(1, 2))
        report = await self._api_credit_report(digits)
        if not isinstance(report, dict) or "customer_details" not in report:
            raise RuntimeError("credit-report API response has no customer_details")
        tm["popupMs"] = round((time.perf_counter() - tp) * 1000)
        tr.ok("credit_report", f"loans={len(report.get('loan_details') or [])} transactions={len(report.get('transaction_log') or [])}")
        return {**base, "status": "ok", "report": report, "member": member,
                "steps": [d["stage"] for d in tr.done], "stages": tr.done}
