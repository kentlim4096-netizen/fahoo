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
import asyncio, os, re, time, uuid

STEALTH_ARGS = ["--disable-blink-features=AutomationControlled", "--exclude-switches=enable-automation"]
MANUAL_REAUTH_REQUIRED = "MANUAL_REAUTH_REQUIRED"


class ReauthRequired(Exception):
    """The dedicated profile is not (or no longer) authenticated - a human must log in."""
    code = MANUAL_REAUTH_REQUIRED


def dash(ic):
    ic = re.sub(r"\D", "", ic or "")
    return f"{ic[:6]}-{ic[6:8]}-{ic[8:]}" if len(ic) == 12 else ic


# The nine stages of one lookup, in order. Every stage that completes is logged with its timing; on a
# failure the log names the stage that was being attempted, so "which stage failed" is one grep away.
STAGES = ("member_list", "search", "apply", "member_found", "actions_clicked",
          "credit_report_clicked", "popup_loaded", "extracted", "popup_closed")


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

    def __init__(self, base_url, profile_dir, headless=True, slow_mo=0, log=None, sync=None):
        super().__init__()
        self.sync = sync                       # SessionSync (optional)
        self.base, self.profile, self.headless, self.slow_mo = base_url.rstrip("/"), profile_dir, headless, slow_mo
        self.log = log or (lambda *_: None)
        self._pw = self.ctx = self.page = None
        self.launches = 0                      # how many browser launches happened (should stay 1 per queue)
        self.browser_pid = None
        self._last_ic = None
        self._lock = asyncio.Lock()            # one lookup at a time on the single page

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        if self.ctx:
            return
        from playwright.async_api import async_playwright
        os.makedirs(self.profile, exist_ok=True)
        self._pw = await async_playwright().start()
        launch = dict(headless=self.headless, viewport={"width": 1400, "height": 1000}, args=STEALTH_ARGS,
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

    def _count_auth_requests(self, req):
        if re.search(r"/users/admin/(login|2fa)/", req.url):
            self.metrics.login_requests += 1
            self.log("[ui] WARNING: a login/OTP request was made by the dedicated context")

    async def _try_sync(self):
        """Copy the already-valid session from the normal Chrome into this profile and re-open Member
        List. Returns True only if the synced session is accepted by KR883. Never logs in."""
        if not self.sync or not self.page:
            return False
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
    def _guard(self):
        if "/login" in (self.page.url or ""):
            raise ReauthRequired("KR883 session is not authenticated in the dedicated profile "
                                 "(needs a manual login).")

    async def _open_member_list(self):
        p = self.page
        await p.goto(self.base + "/members", wait_until="domcontentloaded", timeout=60000)
        try:
            await p.wait_for_selector("#filter-fields-region", timeout=20000)
        except Exception:
            self._guard()
            await p.wait_for_timeout(1500)
            self._guard()
            raise
        self._guard()
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
        await self._reset()
        if self._last_ic == digits:               # same filter value would not re-query - start clean
            await self._open_member_list()
        tr.ok("member_list", "on Member List, filter ready")

        # 1. IC filter -> Apply -> Member List response
        ts = time.perf_counter()
        box = p.locator("#filter-fields-region label.filter-field", has_text=re.compile(r"^\s*IC\s*$")).locator("input").first
        await box.fill("")
        await box.fill(dash(digits))
        tr.ok("search", "IC typed in the Member List IC filter")
        async with p.expect_response(lambda r: "/api/members/" in r.url and f"ic={digits}" in r.url, timeout=25000) as mi:
            await p.locator("#filter-fields-region .filter-actions button", has_text="Apply").click()
        mresp = await mi.value
        if mresp.status == 401:
            raise ReauthRequired("Member List returned 401")
        if mresp.status != 200:
            raise RuntimeError(f"Member List HTTP {mresp.status}")
        results = (await mresp.json()).get("results") or []
        self._last_ic = digits
        matches = [i for i, r in enumerate(results) if re.sub(r"\D", "", r.get("ic_number") or "") == digits]
        tr.ok("apply", f"Apply clicked; Member List returned {len(results)} row(s), {len(matches)} match")
        # Several Member List rows for one IC are just that person's loan cycles (same credit report).
        # "Ambiguous" only when those rows disagree about WHO it is (different names for one IC).
        names = {(results[i].get("full_name") or "").strip().upper() for i in matches}
        base = {"ic": digits, "memberRows": len(matches), "ambiguous": len(names) if len(names) > 1 else 0,
                "timings": tm, "members": [results[i] for i in matches]}
        if not matches:
            await p.wait_for_timeout(300)
            tm["memberSearchMs"] = round((time.perf_counter() - ts) * 1000)
            self.log(f"[ui {tr.tag}] member_not_found        not a member - no Actions/Credit report to click")
            return {**base, "status": "not_member", "report": None, "member": None,
                    "steps": [d["stage"] for d in tr.done], "stages": tr.done}
        member = results[matches[0]]

        # 2. locate the row, open its Actions (3-dot) menu
        row = p.locator("table.q-table tbody tr", has_text=dash(digits)).first
        await row.wait_for(timeout=10000)
        if await p.locator("table.q-table tbody tr", has_text=dash(digits)).count() < 1:
            raise RuntimeError("member row not visible")
        tm["memberSearchMs"] = round((time.perf_counter() - ts) * 1000)
        tr.ok("member_found", "member row visible in the table")

        tp = time.perf_counter()
        await row.locator("button.row-action").click()
        item = p.locator(".q-menu .act-menu button.act-item", has_text="Credit report").first
        await item.wait_for(timeout=8000)
        tr.ok("actions_clicked", "row Actions (3-dot) clicked, menu shows 'Credit report'")

        # 3. Credit report -> the request the PAGE makes -> popup
        async with p.expect_response(lambda r: "/api/loans/credit-report/" in r.url and digits in r.url, timeout=25000) as ci:
            await item.click()
        cr = await ci.value
        if cr.status == 401:
            raise ReauthRequired("credit-report returned 401")
        # proof it was the UI, not a side call: request came from the page's own frame as XHR/fetch
        if cr.request.frame != p.main_frame or cr.request.resource_type not in ("xhr", "fetch"):
            raise RuntimeError("credit-report request did not originate from the page UI")
        if cr.status != 200:
            raise RuntimeError(f"credit-report HTTP {cr.status}")
        tr.ok("credit_report_clicked", "'Credit report' clicked; the page requested the report (HTTP 200, from the page's own frame)")

        dlg = p.locator(".q-dialog.fullscreen").first
        await dlg.wait_for(timeout=12000)
        head = (await dlg.inner_text())[:200]
        if dash(digits) not in head and digits not in head:
            raise RuntimeError("popup opened but does not show this IC (unexpected dialog state)")
        tm["popupMs"] = round((time.perf_counter() - tp) * 1000)
        tr.ok("popup_loaded", "Credit Report popup visible and shows this IC")

        report = await cr.json()
        if not isinstance(report, dict) or "customer_details" not in report:
            raise RuntimeError("report loaded by the popup has no customer_details")
        tr.ok("extracted", f"loans={len(report.get('loan_details') or [])} transactions={len(report.get('transaction_log') or [])}")

        await p.keyboard.press("Escape")
        await p.locator(".q-dialog.fullscreen").first.wait_for(state="detached", timeout=8000)
        tr.ok("popup_closed")
        return {**base, "status": "ok", "report": report, "member": member,
                "steps": [d["stage"] for d in tr.done], "stages": tr.done}
