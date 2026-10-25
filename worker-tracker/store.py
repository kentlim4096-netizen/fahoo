"""Worker activity store (SQLite) + the dashboard numbers.

Every thing a worker does in KR883 on this computer becomes one row in `events`:
  kind = 'heartbeat'  the worker used the KR883 tab in that 30 s window (this is what "active time" counts)
         'page'       a KR883 screen was opened
         'search'     a credit report was pulled for a customer
         'lookup'     a Member List search for a customer
         'action'     a save / update sent to KR883 (POST / PUT / PATCH / DELETE)

Each action is classified (at read time, so a changed rule applies to old data too) as
  opened | disbursed | repayment | other | ignore
by the rules the user set on the dashboard first, then by built-in guesses.

All times are stored in UTC and shown in Malaysia time (UTC+8).
"""
import datetime, json, os, re, sqlite3, threading

HB_SECONDS = 30                                   # one heartbeat = 30 s of active use
_MYT = datetime.timedelta(hours=8)
_LOCK = threading.Lock()
_REPAY = re.compile(r"repay|payment|collect|settle|receipt", re.I)
_DISB = re.compile(r"disburs", re.I)
_OPEN = re.compile(r"(open|apply|applic|new)[-_ ]?loan|loan[-_/ ]?(open|apply|create|application)", re.I)
_IC12 = re.compile(r"(?<!\d)(\d{6}-?\d{2}-?\d{4})(?!\d)")
_ID = re.compile(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d+)(?=/|$)", re.I)
KINDS = ("opened", "disbursed", "repayment", "other", "ignore")


def _conn(path):
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _LOCK, _conn(path) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL, worker TEXT,
            path TEXT, method TEXT, ic TEXT, name TEXT, status INTEGER, data TEXT)""")
        try:
            c.execute("ALTER TABLE events ADD COLUMN computer TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass                                   # column already there
        c.execute("CREATE INDEX IF NOT EXISTS ix_ts ON events(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_kind_ts ON events(kind, ts)")
        c.execute("""CREATE TABLE IF NOT EXISTS members (
            id TEXT PRIMARY KEY, ic TEXT, name TEXT, loan_amount TEXT, on_hand TEXT, tenure TEXT, event TEXT, seen TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_member_ic ON members(ic)")
        c.execute("""CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, type TEXT NOT NULL, worker TEXT, name TEXT, ic TEXT,
            amount TEXT, on_hand TEXT, tenure TEXT, ok INTEGER, detail TEXT, acked INTEGER DEFAULT 0)""")


def _utc(iso):
    try:
        d = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if d.tzinfo:
            d = d.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return d.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _digits(v):
    return re.sub(r"\D", "", str(v or ""))


def _dash(ic):
    d = _digits(ic)
    return f"{d[:6]}-{d[6:8]}-{d[8:]}" if len(d) == 12 else d


def _myt(ts):
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S") + _MYT


def find_ic(*vals):
    """First 12-digit IC found in any of the values (dicts/strings), as bare digits."""
    for v in vals:
        s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) if v is not None else ""
        m = _IC12.search(s)
        if m:
            return _digits(m.group(1))
    return ""


# --------------------------------------------------------------------------- classification
def norm_path(path):
    """/api/loans/123/disburse/ -> /api/loans/{id}/disburse/ (so one rule covers every customer)."""
    return _ID.sub("/{id}", path or "")


def rule_key(method, path):
    return f"{str(method).upper()} {norm_path(path)}"


def classify(method, path, request, rules=None):
    """-> 'opened' | 'disbursed' | 'repayment' | 'other' | 'ignore'. User rules win over the built-in guesses."""
    method = str(method or "").upper()
    if method == "GET":
        return "other"
    key = rule_key(method, path)
    for r in rules or []:
        if r.get("key") == key and r.get("kind") in KINDS:
            return r["kind"]
    blob = (path or "") + " " + (json.dumps(request, ensure_ascii=False) if request else "")
    if _DISB.search(blob):
        return "disbursed"
    if _REPAY.search(blob):
        return "repayment"
    if _OPEN.search(blob) or (method == "POST" and re.search(r"/api/loans/?$", path or "")):
        return "opened"
    return "other"


def _first(d, *keys):
    if isinstance(d, dict):
        for k in keys:
            if d.get(k) not in (None, ""):
                return d[k]
    return None


def amount_of(request):
    return _first(request, "amount", "repayment_amount", "paid_amount", "payment_amount", "credit", "total")


def loan_info(path, ic, request, response, member_id=None):
    """Customer + loan figures for an action: what the worker submitted, filled in from the Member List row
    this computer last saw for that customer (loan amount / on-hand amount / tenure)."""
    req = request if isinstance(request, dict) else {}
    resp = response if isinstance(response, dict) else {}
    ic = _digits(ic) or find_ic(req, resp)
    mid = member_id or _first(req, "member", "member_id", "member_uuid", "customer", "customer_id") or _first(resp, "member", "member_id")
    row = None
    with _LOCK, _conn(path) as c:
        if mid not in (None, ""):
            row = c.execute("SELECT * FROM members WHERE id = ?", (str(mid),)).fetchone()
        if not row and ic:
            row = c.execute("SELECT * FROM members WHERE ic = ? ORDER BY seen DESC", (ic,)).fetchone()
    row = dict(row) if row else {}
    return {
        "name": _first(req, "full_name", "name") or _first(resp, "full_name", "name") or row.get("name") or "",
        "ic": _dash(ic or row.get("ic")),
        "amount": _first(req, "loan_amount", "amount", "principal") or _first(resp, "loan_amount") or row.get("loan_amount") or "",
        "onHand": _first(req, "on_hand_amount", "on_hand", "onHand") or _first(resp, "on_hand_amount") or row.get("on_hand") or "",
        "tenure": _first(req, "tenure_days", "tenure") or _first(resp, "tenure_days") or row.get("tenure") or "",
    }


def upsert_members(path, rows, at=None):
    """Remember the customers a Member List response showed (name, loan amount, on-hand amount, tenure)."""
    seen = _utc(at)
    data = []
    for m in rows or []:
        if not isinstance(m, dict) or m.get("id") in (None, ""):
            continue
        data.append((str(m["id"]), _digits(m.get("ic_number")), m.get("full_name") or m.get("name") or "",
                     str(m.get("loan_amount") or ""), str(m.get("on_hand_amount") or ""), str(m.get("tenure_days") or ""),
                     m.get("event_display") or m.get("event") or "", seen))
    if data:
        with _LOCK, _conn(path) as c:
            c.executemany("INSERT OR REPLACE INTO members VALUES (?,?,?,?,?,?,?,?)", data)
    return len(data)


# --------------------------------------------------------------------------- writing events
def add(path, ev):
    """Store one event dict from the extension. Returns the new row id (or 0). Never raises."""
    try:
        t = ev.get("type")
        if t not in ("heartbeat", "page", "action"):
            return 0
        row = {"ts": _utc(ev.get("at")), "kind": t, "worker": ev.get("worker") or "", "path": ev.get("path") or "",
               "method": ev.get("method") or "", "ic": "", "name": "", "status": ev.get("status"), "data": None,
               "computer": ev.get("computer") or ""}
        if t == "action":
            req = ev.get("request")
            row["ic"] = find_ic(req, ev.get("path"), ev.get("response"))
            row["data"] = json.dumps({"request": req, "response": ev.get("response")}, ensure_ascii=False)[:8000]
        elif t == "page":
            row["data"] = json.dumps({"title": ev.get("title")}, ensure_ascii=False)
        return _insert(path, row)
    except Exception:
        return 0


def add_lookup(path, kind, ev, ic, name):
    """Record a customer search taken from a captured API response ('search' = credit report, 'lookup' = member list)."""
    try:
        return _insert(path, {"ts": _utc(ev.get("at")), "kind": kind, "worker": ev.get("worker") or "",
                              "path": "", "method": "GET", "ic": _digits(ic), "name": name or "",
                              "status": ev.get("status"), "data": None, "computer": ev.get("computer") or ""})
    except Exception:
        return 0


def _insert(path, r):
    with _LOCK, _conn(path) as c:
        cur = c.execute("INSERT INTO events(ts,kind,worker,path,method,ic,name,status,data,computer) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (r["ts"], r["kind"], r["worker"], r["path"], r["method"], r["ic"], r["name"], r["status"], r["data"], r.get("computer", "")))
        return cur.lastrowid


def worker_known(path, worker):
    """(is this worker already in the data, is there any worker at all)."""
    with _LOCK, _conn(path) as c:
        known = c.execute("SELECT 1 FROM events WHERE worker = ? LIMIT 1", (worker,)).fetchone() is not None
        any_ = c.execute("SELECT 1 FROM events WHERE worker != '' LIMIT 1").fetchone() is not None
    return known, any_


# --------------------------------------------------------------------------- notifications
def add_notification(path, ev_ts, typ, worker, info, ok, detail=""):
    with _LOCK, _conn(path) as c:
        cur = c.execute("INSERT INTO notifications(ts,type,worker,name,ic,amount,on_hand,tenure,ok,detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (_utc(ev_ts), typ, worker, info.get("name", ""), info.get("ic", ""), str(info.get("amount", "")),
                         str(info.get("onHand", "")), str(info.get("tenure", "")), 1 if ok else 0, detail))
        return cur.lastrowid


def notifications(path, after=0, limit=100, unacked_only=False):
    q, args = "SELECT * FROM notifications WHERE id > ?", [after]
    if unacked_only:
        q += " AND acked = 0"
    with _LOCK, _conn(path) as c:
        rows = [dict(r) for r in c.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit])]
    for r in rows:
        r["time"] = _myt(r["ts"]).strftime("%Y-%m-%d %H:%M:%S")
    return rows


def ack_notifications(path, upto):
    with _LOCK, _conn(path) as c:
        c.execute("UPDATE notifications SET acked = 1 WHERE id <= ?", (upto,))


# --------------------------------------------------------------------------- dashboard
def _section(path):
    p = (path or "/").strip("/").split("/")
    return "/" + "/".join(p[:2]) if p and p[0] else "/ (home)"


def computers(path):
    with _LOCK, _conn(path) as c:
        return [r[0] for r in c.execute("SELECT DISTINCT computer FROM events WHERE computer IS NOT NULL AND computer != '' ORDER BY computer")]


def workers(path):
    with _LOCK, _conn(path) as c:
        return [r[0] for r in c.execute("SELECT DISTINCT worker FROM events WHERE worker != '' ORDER BY worker")]


def _bounds(date):
    d0 = datetime.datetime.strptime(date, "%Y-%m-%d")
    return ((d0 - _MYT).strftime("%Y-%m-%dT%H:%M:%S"),
            (d0 + datetime.timedelta(days=1) - _MYT).strftime("%Y-%m-%dT%H:%M:%S"))


def _rows(path, lo, hi, worker="", computer=""):
    q, args = "SELECT * FROM events WHERE ts >= ? AND ts < ?", [lo, hi]
    if worker:
        q, args = q + " AND worker = ?", args + [worker]
    if computer:
        q, args = q + " AND computer = ?", args + [computer]
    with _LOCK, _conn(path) as c:
        return [dict(r) for r in c.execute(q + " ORDER BY ts", args)]


def _stats(dbpath, rows, rules):
    """Numbers + lists for a set of events (one worker's day, or everyone's)."""
    hours = [{"hour": h, "activeMin": 0.0, "searches": 0, "actions": 0} for h in range(24)]
    sections, searched, timeline, opened, disbursed, repay = {}, {}, [], [], [], []
    beats, action_ics, first, last, hb = [], set(), None, None, 0
    for r in rows:
        t = _myt(r["ts"])
        if r["kind"] in ("heartbeat", "search", "action"):
            first = first or t
            last = t
        if r["kind"] == "heartbeat":
            hb += 1
            beats.append(t)
            hours[t.hour]["activeMin"] += HB_SECONDS / 60
            s = _section(r["path"])
            sections[s] = sections.get(s, 0) + HB_SECONDS
        elif r["kind"] == "search":
            hours[t.hour]["searches"] += 1
            e = searched.setdefault(r["ic"], {"ic": _dash(r["ic"]), "name": r["name"], "times": 0, "last": ""})
            e["times"] += 1
            e["last"] = t.strftime("%H:%M:%S")
            e["name"] = e["name"] or r["name"]
            timeline.append({"time": t.strftime("%H:%M:%S"), "kind": "search", "worker": r["worker"],
                             "text": f"Credit report: {r['name'] or ''} ({_dash(r['ic'])})"})
        elif r["kind"] == "action":
            hours[t.hour]["actions"] += 1
            data = json.loads(r["data"] or "{}")
            cls = classify(r["method"], r["path"], data.get("request"), rules)
            if cls == "ignore":
                continue
            if r["ic"]:
                action_ics.add(r["ic"])
            info = loan_info(dbpath, r["ic"], data.get("request"), data.get("response")) if cls in ("opened", "disbursed", "repayment") else {}
            amount = info.get("amount") if cls != "repayment" else amount_of(data.get("request"))
            item = {"time": t.strftime("%H:%M:%S"), "kind": cls, "worker": r["worker"], "ok": (r["status"] or 0) < 400,
                    "key": rule_key(r["method"], r["path"]), "text": f"{r['method']} {r['path']} -> {r['status']}",
                    "name": info.get("name", ""), "ic": info.get("ic") or _dash(r["ic"]), "amount": amount if amount is not None else "",
                    "onHand": info.get("onHand", ""), "tenure": info.get("tenure", "")}
            timeline.append(item)
            if cls == "opened":
                opened.append(item)
            elif cls == "disbursed":
                disbursed.append(item)
            elif cls == "repayment":
                repay.append(item)
    actions = [e for e in timeline if e["kind"] not in ("search",)]
    return {
        "first": first, "last": last, "beats": beats, "hours": hours,
        "firstActive": first.strftime("%H:%M") if first else "", "lastActive": last.strftime("%H:%M") if last else "",
        "activeMinutes": round(hb * HB_SECONDS / 60, 1),
        "searches": sum(e["times"] for e in searched.values()), "customers": len(searched),
        "actions": len(actions), "repayments": len(repay), "opened": len(opened), "disbursed": len(disbursed),
        "logs": len(rows), "sections": sections, "searched": searched, "timeline": timeline,
        "openedList": opened[::-1], "disbursedList": disbursed[::-1], "repaymentList": repay[::-1],
        "actionList": actions[::-1], "actionIcs": action_ics,
    }


def _idle_gaps(beats, minutes):
    gaps = []
    for a, b in zip(beats, beats[1:]):
        m = (b - a).total_seconds() / 60
        if m >= minutes:
            gaps.append({"from": a.strftime("%H:%M"), "to": b.strftime("%H:%M"), "minutes": round(m)})
    return gaps[::-1]


def day(path, date, worker="", rules=None, settings=None, computer=""):
    """Everything for one Malaysia-time day (YYYY-MM-DD) and, optionally, one worker."""
    settings = settings or {}
    lo, hi = _bounds(date)
    s = _stats(path, _rows(path, lo, hi, worker, computer), rules)
    sched = settings.get("schedule") or {}
    flags = []
    if s["first"] and sched.get("start") and s["first"].strftime("%H:%M") > sched["start"]:
        flags.append(f"Started {s['first'].strftime('%H:%M')} (schedule {sched['start']})")
    today = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + _MYT)
    if s["last"] and sched.get("end") and s["last"].strftime("%H:%M") < sched["end"] \
            and (date < today.strftime("%Y-%m-%d") or today.strftime("%H:%M") > sched["end"]):
        flags.append(f"Last active {s['last'].strftime('%H:%M')} (schedule ends {sched['end']})")
    idle = int(settings.get("idleMinutes") or 15)
    return {
        "date": date, "worker": worker, **{k: s[k] for k in (
            "firstActive", "lastActive", "activeMinutes", "searches", "customers", "actions", "repayments",
            "opened", "disbursed", "logs", "openedList", "disbursedList", "repaymentList", "actionList")},
        "hours": [dict(h, activeMin=round(h["activeMin"], 1)) for h in s["hours"]],
        "sections": sorted(({"section": k, "minutes": round(v / 60, 1)} for k, v in s["sections"].items()), key=lambda x: -x["minutes"]),
        "customersSearched": sorted(s["searched"].values(), key=lambda x: x["last"], reverse=True),
        "repeatLookups": sorted((e for e in s["searched"].values() if e["times"] >= 3), key=lambda x: -x["times"]),
        "noAction": [e for ic, e in s["searched"].items() if ic not in s["actionIcs"]],
        "idleGaps": _idle_gaps(s["beats"], idle), "idleMinutes": idle, "flags": flags,
        "timeline": s["timeline"][::-1][:300],
    }


def leaderboard(path, date, rules=None, settings=None, computer=""):
    """One line per worker for the day, most active first."""
    lo, hi = _bounds(date)
    rows = _rows(path, lo, hi, "", computer)
    out = []
    target = int((settings or {}).get("targets", {}).get("repayments") or 0)
    for w in sorted({r["worker"] for r in rows if r["worker"]}):
        s = _stats(path, [r for r in rows if r["worker"] == w], rules)
        out.append({"worker": w, "activeMinutes": s["activeMinutes"], "customers": s["customers"], "searches": s["searches"],
                    "opened": s["opened"], "disbursed": s["disbursed"], "repayments": s["repayments"], "target": target,
                    "firstActive": s["firstActive"], "lastActive": s["lastActive"]})
    return sorted(out, key=lambda x: -x["activeMinutes"])


def history(path, days=14, worker="", end=None, rules=None, computer=""):
    """Per-day totals for the last `days` days (Malaysia time), newest first."""
    end = end or (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + _MYT).date()
    start = end - datetime.timedelta(days=days - 1)
    lo = (datetime.datetime.combine(start, datetime.time()) - _MYT).strftime("%Y-%m-%dT%H:%M:%S")
    q, args = "SELECT ts, kind, ic, method, path, data FROM events WHERE ts >= ?", [lo]
    if worker:
        q, args = q + " AND worker = ?", args + [worker]
    if computer:
        q, args = q + " AND computer = ?", args + [computer]
    out = {}
    for i in range(days):
        dd = (start + datetime.timedelta(days=i)).isoformat()
        out[dd] = {"date": dd, "activeMinutes": 0.0, "searches": 0, "customers": set(), "actions": 0, "repayments": 0,
                   "opened": 0, "disbursed": 0, "logs": 0}
    with _LOCK, _conn(path) as c:
        for r in c.execute(q, args):
            o = out.get(_myt(r["ts"]).date().isoformat())
            if not o:
                continue
            o["logs"] += 1
            if r["kind"] == "heartbeat":
                o["activeMinutes"] += HB_SECONDS / 60
            elif r["kind"] == "search":
                o["searches"] += 1
                o["customers"].add(r["ic"])
            elif r["kind"] == "action":
                cls = classify(r["method"], r["path"], json.loads(r["data"] or "{}").get("request"), rules)
                if cls == "ignore":
                    continue
                o["actions"] += 1
                if cls in ("repayment", "opened", "disbursed"):
                    o[{"repayment": "repayments"}.get(cls, cls)] += 1
    rows = []
    for o in out.values():
        o["customers"] = len(o["customers"])
        o["activeMinutes"] = round(o["activeMinutes"], 1)
        rows.append(o)
    return sorted(rows, key=lambda x: x["date"], reverse=True)


def action_keys(path, rules=None, limit=40):
    """Distinct save/update requests seen recently (with how they are classified now) - for the rule editor."""
    seen = {}
    with _LOCK, _conn(path) as c:
        for r in c.execute("SELECT method, path, data, ts FROM events WHERE kind = 'action' ORDER BY id DESC LIMIT 2000"):
            k = rule_key(r["method"], r["path"])
            if k not in seen:
                seen[k] = {"key": k, "count": 0, "last": _myt(r["ts"]).strftime("%Y-%m-%d %H:%M"),
                           "kind": classify(r["method"], r["path"], json.loads(r["data"] or "{}").get("request"), rules)}
            seen[k]["count"] += 1
    return sorted(seen.values(), key=lambda x: -x["count"])[:limit]
