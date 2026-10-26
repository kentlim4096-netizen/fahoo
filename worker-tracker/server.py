"""Worker tracker server - receives captured KR883 responses from the browser extension and keeps the
Excel file up to date. Listens on 127.0.0.1 only.

    POST /activity        (header X-Activity-Key) one captured response from the extension
    GET  /tracker.xlsx    download the workbook
    GET  /                status
"""
import asyncio, datetime, hmac, io, json, os, re, subprocess

from aiohttp import web

import aiohttp

import crm
import settings
import store
import tracker

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
XLSX = os.path.join(HERE, "data", "worker_tracker.xlsx")
DB = os.path.join(HERE, "data", "tracker.db")
SETTINGS = os.path.join(HERE, "data", "settings.json")
store.init(DB)
crm.init(DB)
KNOWN = set(store.workers(DB))   # KR883 accounts already seen on this computer
KEY = CONFIG["key"]


def log(msg):
    print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def popup(title, message):
    """Windows message box on this PC (detached; it stays up until dismissed)."""
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                          os.path.join(HERE, "notify.ps1"), title, message[:400]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def check_new_account(ev, cfg):
    w = ev.get("worker") or ""
    if not w or w in KNOWN:
        return
    first_ever = not KNOWN
    KNOWN.add(w)
    if first_ever:
        return                                                  # the very first account is just the baseline
    crm.enqueue(DB, "worker.new_account", ev.get("at"), _source(ev), {"note": "First time this KR883 account is seen on any tracked computer"})
    if not cfg["notify"]["newAccount"]:
        return
    store.add_notification(DB, ev.get("at"), "new_account", w, {"name": w}, True, "First time this KR883 account is seen on this computer")
    if cfg["notify"]["popup"]:
        popup("Worker Tracker - new KR883 account", f"A KR883 account not seen before is now active on this computer:\n\n{w}")


def _source(ev):
    return {"worker": ev.get("worker") or "", "computer": ev.get("computer") or ""}


def _member_id(ev):
    req = ev.get("request") if isinstance(ev.get("request"), dict) else {}
    for k in ("member", "member_id", "member_uuid", "customer", "customer_id"):
        if req.get(k) not in (None, ""):
            return str(req[k])
    m = re.search(r"/members?/([^/]+)/", ev.get("path") or "")
    return m.group(1) if m else None


def _txt(v):
    return None if v in (None, "") else str(v)


def push_action_to_crm(ev, cls, info):
    """Queue the LG-CRM event for one save: loan.opened / loan.disbursed / loan.repayment, or activity.action."""
    ok = (ev.get("status") or 0) < 400
    result = {"ok": ok, "http_status": ev.get("status")}
    request_info = {"method": ev.get("method"), "path": ev.get("path")}
    if cls in ("opened", "disbursed", "repayment"):
        crm.enqueue(DB, "loan." + cls, ev.get("at"), _source(ev), {
            "loan": {"kr883_loan_id": crm.loan_id_of(ev.get("path"), ev.get("request"), ev.get("response")),
                     "customer": {"name": info.get("name"), "ic": info.get("ic"), "kr883_member_id": _member_id(ev)},
                     "amount": _txt(info.get("amount") if cls != "repayment" else store.amount_of(ev.get("request"))),
                     "on_hand_amount": _txt(info.get("onHand")), "tenure_days": _txt(info.get("tenure"))},
            "result": result, "request": request_info, "raw_request": ev.get("request")})
    elif cls != "ignore":
        crm.enqueue(DB, "activity.action", ev.get("at"), _source(ev), {
            "customer": {"ic": info.get("ic")} if info.get("ic") else None, "result": result, "request": request_info})


def process_action(ev, cfg):
    """Store a save/update action and, if it is a loan being opened / disbursed and that alert is on, raise it."""
    rid = store.add(DB, ev)
    cls = store.classify(ev.get("method"), ev.get("path"), ev.get("request"), cfg["rules"])
    info = store.loan_info(DB, store.find_ic(ev.get("request"), ev.get("path"), ev.get("response")), ev.get("request"), ev.get("response"))
    if rid:
        push_action_to_crm(ev, cls, info)
    if rid and cls in ("opened", "disbursed") and cfg["notify"].get(cls):
        ok = (ev.get("status") or 0) < 400
        store.add_notification(DB, ev.get("at"), cls, ev.get("worker") or "", info, ok, f"{ev.get('method')} {ev.get('path')} -> {ev.get('status')}")
        if cfg["notify"]["popup"]:
            what = "OPENING a loan" if cls == "opened" else "DISBURSING a loan"
            popup(f"Worker Tracker - loan {cls}",
                  f"{ev.get('worker') or 'A worker'} is {what}\n\nCustomer:  {info['name'] or '(unknown)'}  {info['ic']}\n"
                  f"Loan amount:  {info['amount'] or '-'}\nOn-hand amount:  {info['onHand'] or '-'}\n"
                  f"Tenure:  {(str(info['tenure']) + ' days') if info['tenure'] else '-'}\nResult:  {'saved' if ok else 'FAILED'}")
    return rid


async def activity(request):
    if not hmac.compare_digest(request.headers.get("X-Activity-Key", ""), KEY):
        return web.json_response({"error": "bad key"}, status=401)
    try:
        ev = await request.json()
    except Exception:
        return web.json_response({"error": "expected JSON"}, status=400)
    if ev.get("ping"):
        log(f"extension loaded on {ev.get('page')}")
        return web.json_response({"ok": True})
    loop = asyncio.get_running_loop()
    cfg = settings.load(SETTINGS)
    await loop.run_in_executor(None, check_new_account, ev, cfg)
    if ev.get("type") in ("heartbeat", "page", "action"):      # worker activity (not a customer search)
        if ev["type"] == "action":
            log(f"action {ev.get('method')} {ev.get('path')} -> {ev.get('status')}")
            ok = await loop.run_in_executor(None, process_action, ev, cfg)
        else:
            ok = await loop.run_in_executor(None, store.add, DB, ev)
        return web.json_response({"ok": True, "recorded": bool(ok)})
    log(f"event {ev.get('status')} {str(ev.get('url'))[:110]}")
    ok = await loop.run_in_executor(None, tracker.record, XLSX, ev)
    kind, q = tracker.classify(ev.get("url"))
    if kind and ev.get("status") == 200:
        b = ev.get("body") if isinstance(ev.get("body"), dict) else {}
        if kind == "Member search":                             # remember each customer's loan amount / on-hand / tenure
            await loop.run_in_executor(None, store.upsert_members, DB, b.get("results") or [], ev.get("at"))
        if ok and kind == "Credit report":
            cd = b.get("customer_details") or {}
            await loop.run_in_executor(None, store.add_lookup, DB, "search", ev, q.get("ic_number") or cd.get("ic_number"), cd.get("name"))
            crm.enqueue(DB, "activity.search", ev.get("at"), _source(ev), {
                "customer": {"name": cd.get("name"), "ic": tracker.dash(q.get("ic_number") or cd.get("ic_number"))},
                "blacklist_count": cd.get("blacklist_count"), "overdue_days": cd.get("active_overdue_days"),
                "loans_total": len(b.get("loan_details") or [])})
        elif ok and q.get("ic"):
            rows = b.get("results") or []
            await loop.run_in_executor(None, store.add_lookup, DB, "lookup", ev, q.get("ic"), (rows[0].get("full_name") if len(rows) == 1 else ""))
    return web.json_response({"ok": True, "recorded": bool(ok)})


async def crm_api(request):
    """LG-CRM connection: GET = status; POST = save settings / send a test event / retry failed events."""
    loop = asyncio.get_running_loop()
    if request.method == "POST":
        if not _csrf_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await request.json()
        act = body.pop("action", "")
        try:
            if act == "test":
                if not crm.load_config()["url"]:
                    return web.json_response({"error": "Enter the LG-CRM address first."}, status=400)
                crm.enqueue(DB, "test.ping", datetime.datetime.now(datetime.timezone.utc).isoformat(), {"worker": "", "computer": ""},
                            {"note": "Test event from the worker tracker dashboard"})
            elif act == "retry":
                crm.retry_failed(DB)
            else:
                crm.save_config(body)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"config": crm.public_config(), "status": await loop.run_in_executor(None, crm.status, DB)})


async def _background(app):
    app["crm_sender"] = asyncio.create_task(crm.sender_loop(DB, lambda: aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))))

    async def summary_loop():
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(600)
            try:
                cfg = settings.load(SETTINGS)
                await loop.run_in_executor(None, crm.summaries, DB, store, cfg["rules"], cfg)
            except Exception as e:
                log(f"summary push failed: {e}")
    app["crm_summary"] = asyncio.create_task(summary_loop())


async def download(request):
    if not os.path.exists(XLSX):
        return web.json_response({"error": "nothing recorded yet"}, status=404)
    with open(XLSX, "rb") as f:
        data = f.read()
    return web.Response(body=data, headers={
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Content-Disposition": 'attachment; filename="worker_tracker.xlsx"'})


async def status(request):
    return web.json_response({"service": "worker-tracker", "xlsx": XLSX, "exists": os.path.exists(XLSX)})


@web.middleware
async def guard(request, handler):
    """/activity (the extension's POST, key-protected) answers CORS / Private-Network-Access preflights.
    Everything else - the viewer and the customer data - is for this computer only: no CORS headers, and
    the Host must be localhost (blocks DNS-rebinding), so other websites can never read it."""
    if request.path == "/activity":
        resp = web.Response(status=204) if request.method == "OPTIONS" else await handler(request)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Activity-Key"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Private-Network"] = "true"
        return resp
    if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
        return web.json_response({"error": "forbidden"}, status=403)
    return await handler(request)


_MYT = datetime.timedelta(hours=8)
_ACTIVE = {"Disbursed", "Approved"}


def _dt(iso):
    """Same rule as the scraper: timezone-aware ISO -> Malaysia local time; a naive one is kept as is."""
    try:
        d = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return d.astimezone(datetime.timezone.utc).replace(tzinfo=None) + _MYT if d.tzinfo else d
    except Exception:
        return None


def _signals(d):
    """The summary cards of the scraper's credit-report page (todayCount / totalCompleted / totalDisbursed /
    period), computed from the stored report the way scraper_service.py does it."""
    cd, loans, txs = d.get("customer_details") or {}, d.get("loan_details") or [], d.get("transaction_log") or []
    today = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + _MYT).date()
    today_n = sum(1 for l in loans if l.get("loan_status") in _ACTIVE
                  and (_dt(l.get("created_on")) or datetime.datetime.min).date() == today)
    done = sorted((x for x in (_dt(l.get("created_on")) for l in loans if l.get("loan_status") == "Completed") if x),
                  reverse=True)
    pays = sorted(((_dt(t.get("date")), t.get("credit") or t.get("debit") or 0) for t in txs
                   if t.get("transaction_type") == "Loan Repayment" and _dt(t.get("date"))),
                  key=lambda r: r[0], reverse=True)
    recent = []
    for i, b in enumerate(done[:5]):
        if i < len(pays) and pays[i][0].date() >= b.date():
            recent.append({"days": (pays[i][0].date() - b.date()).days + 1, "amt": pays[i][1]})
        else:
            recent.append({"days": None, "amt": None})
    ds = sorted(o["days"] for o in recent if o["days"])
    period = "" if not ds else (ds[len(ds) // 2] if len(ds) % 2 else round((ds[len(ds) // 2 - 1] + ds[len(ds) // 2]) / 2))
    return {"todayCount": str(today_n), "totalCompleted": str(cd.get("closed") or 0),
            "totalDisbursed": str(sum(1 for l in loans if l.get("loan_status") in _ACTIVE)),
            "cardOverdueDays": cd.get("active_overdue_days") or 0, "period": str(period), "recentDetail": recent}


def _shape(c):
    """Stored row -> the JSON the scraper's /credit-report returns, so the same page can render it."""
    d = c["report"] or {}
    cd = dict(d.get("customer_details") or {})
    loans = d.get("loan_details") or []
    if not cd.get("admins"):
        cd["admins"] = sorted({l["admin"] for l in loans if l.get("admin")})
    return {"found": bool(d), "ic": c["ic"], "name": cd.get("name") or c["name"], "phone": c["phone"], "customer": cd,
            "loans": loans, "transactions": d.get("transaction_log") or [], "blacklist": d.get("blacklist") or [],
            "member": {}, "signals": _signals(d) if d else {}, "memberRecord": c["member"], "incomplete": c.get("incomplete", False),
            "tracker": {"first": c["first"], "last": c["last"], "times": c["times"], "updated": c["updated"]}}


def _read_customers():
    """Customers sheet -> list of dicts for the viewer. Reads a copy of the file so Excel's lock never blocks."""
    import openpyxl
    if not os.path.exists(XLSX):
        return []
    with open(XLSX, "rb") as f:
        raw = f.read()
    ws = openpyxl.load_workbook(io.BytesIO(raw), read_only=True)["Customers"]
    out = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or not r[0]:
            continue
        def j(v):
            try:
                return json.loads(v) if v else None
            except Exception:
                return None
        report = j(r[17])
        full = os.path.join(os.path.dirname(XLSX), "reports", re.sub(r"\D", "", str(r[0])) + ".json")
        if os.path.exists(full):                               # the uncut copy wins over the (maybe cut) cell
            try:
                with open(full, encoding="utf-8") as f:
                    report = json.load(f)
            except Exception:
                pass
        out.append({"ic": r[0], "name": r[1] or "", "phone": r[2] or "", "first": r[3] or "", "last": r[4] or "",
                    "times": r[5] or 0, "updated": r[16] or "", "report": report, "member": j(r[18]), "incomplete": report is None})
    out.sort(key=lambda c: str(c["last"]), reverse=True)
    return out


async def customers(request):
    rows = await asyncio.get_running_loop().run_in_executor(None, _read_customers)
    return web.json_response([_shape(c) for c in rows])


def _today():
    return (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + store._MYT).strftime("%Y-%m-%d")


async def dashboard_api(request):
    date = request.query.get("date") or _today()
    worker = request.query.get("worker", "")
    computer = request.query.get("computer", "")
    loop = asyncio.get_running_loop()
    cfg = settings.load(SETTINGS)
    try:
        end = datetime.datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return web.json_response({"error": "bad date"}, status=400)
    r = cfg["rules"]
    return web.json_response({
        "workers": await loop.run_in_executor(None, store.workers, DB),
        "computers": await loop.run_in_executor(None, store.computers, DB),
        "day": await loop.run_in_executor(None, store.day, DB, date, worker, r, cfg, computer),
        "history": await loop.run_in_executor(None, store.history, DB, 14, worker, end, r, computer),
        "leaderboard": await loop.run_in_executor(None, store.leaderboard, DB, date, r, cfg, computer),
        "actionKeys": await loop.run_in_executor(None, store.action_keys, DB, r),
        "settings": cfg})


def _csrf_ok(request):
    return request.headers.get("X-Tracker") == "1"           # forces a CORS preflight, so other websites cannot call it


async def settings_api(request):
    if request.method == "POST":
        if not _csrf_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "expected JSON"}, status=400)
        if isinstance(body.get("rule"), dict):
            k, kind = str(body["rule"].get("key", "")), str(body["rule"].get("kind", ""))
            if kind and kind not in store.KINDS:
                return web.json_response({"error": "bad kind"}, status=400)
            return web.json_response(settings.set_rule(SETTINGS, k, kind))
        body.pop("rules", None)
        return web.json_response(settings.save(SETTINGS, body))
    return web.json_response(settings.load(SETTINGS))


async def notifications_api(request):
    if request.method == "POST":
        if not _csrf_ok(request):
            return web.json_response({"error": "forbidden"}, status=403)
        body = await request.json()
        store.ack_notifications(DB, int(body.get("upto") or 0))
        return web.json_response({"ok": True})
    after = int(request.query.get("after") or 0)
    return web.json_response({"items": store.notifications(DB, after=after, limit=100)})


async def summary_xlsx(request):
    """One row per worker for a day - for handing to LG-CRM / the boss."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    date = request.query.get("date") or _today()
    cfg = settings.load(SETTINGS)
    board = store.leaderboard(DB, date, cfg["rules"], cfg)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Daily summary"
    ws.append(["Date", "Worker", "First active", "Last active", "Active time (min)", "Customers searched", "Credit reports",
               "Loans opened", "Loans disbursed", "Repayments keyed", "Repayment target"])
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
    for b in board:
        ws.append([date, b["worker"], b["firstActive"], b["lastActive"], b["activeMinutes"], b["customers"], b["searches"],
                   b["opened"], b["disbursed"], b["repayments"], b["target"] or ""])
    for i, w in enumerate((12, 22, 12, 12, 16, 18, 14, 13, 15, 16, 16), 1):
        ws.column_dimensions[chr(64 + i)].width = w
    day_ = store.day(DB, date, "", cfg["rules"], cfg)
    ws2 = wb.create_sheet("Loans opened & disbursed")
    ws2.append(["Time", "Worker", "Action", "Customer", "IC", "Loan amount", "On-hand amount", "Tenure (days)", "Result"])
    for c in ws2[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E78")
    for it in sorted(day_["openedList"] + day_["disbursedList"], key=lambda x: x["time"]):
        ws2.append([it["time"], it["worker"], it["kind"], it["name"], it["ic"], it["amount"], it["onHand"], it["tenure"],
                    "saved" if it["ok"] else "failed"])
    buf = io.BytesIO()
    wb.save(buf)
    return web.Response(body=buf.getvalue(), headers={
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Content-Disposition": 'attachment; filename="worker_summary_' + date + '.xlsx"'})


async def dashboard(request):
    return web.FileResponse(os.path.join(HERE, "dashboard.html"))


async def view(request):
    return web.FileResponse(os.path.join(HERE, "viewer.html"))


def main():
    app = web.Application(client_max_size=64 * 1024 * 1024, middlewares=[guard])
    app.on_startup.append(_background)
    app.add_routes([web.post("/activity", activity), web.get("/tracker.xlsx", download), web.get("/api/customers", customers), web.get("/view", view), web.get("/dashboard", dashboard), web.get("/api/dashboard", dashboard_api), web.route("*", "/api/settings", settings_api),
                    web.route("*", "/api/notifications", notifications_api), web.route("*", "/api/crm", crm_api), web.get("/api/summary.xlsx", summary_xlsx), web.get("/", status)])
    web.run_app(app, host="127.0.0.1", port=CONFIG.get("port", 8780), print=lambda *_: None)


if __name__ == "__main__":
    main()
