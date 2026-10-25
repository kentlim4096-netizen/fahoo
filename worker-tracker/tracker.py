"""Worker tracker: one Excel row per customer searched in the KR883 website on this computer.

A customer searched again is UPDATED in place (latest credit-report data, last-searched time, search
count) - not duplicated. Every individual search is also appended to a "History" sheet with its
timestamp. Holds FULL ICs and customer data: for hand-over to LG-CRM only. Never raises.

If the workbook is open in Excel (which locks it) events are parked in <file>.pending.jsonl and
applied on the next event once it is free.
"""
import datetime, json, os, re, socket, threading, urllib.parse

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

CUSTOMERS = [
    ("IC Number", 16), ("Name", 30), ("Phone", 15), ("First searched (MYT)", 20), ("Last searched (MYT)", 20),
    ("Times searched", 10), ("Computer", 18), ("Blacklist count", 14), ("Overdue days", 13),
    ("Total loans", 11), ("Completed loans", 15), ("Disbursed loans", 15), ("Latest loan status", 18),
    ("Latest loan date", 20), ("Last repayment date", 20), ("Transactions", 12),
    ("Credit report updated (MYT)", 22), ("Credit report (JSON)", 60), ("Member record (JSON)", 60),
]
HISTORY = [("No.", 6), ("Timestamp (MYT)", 20), ("Computer", 18), ("Activity", 16), ("IC Number", 16),
           ("Name", 30), ("Search", 30)]
COL = {name: i + 1 for i, (name, _w) in enumerate(CUSTOMERS)}
_MYT = datetime.timezone(datetime.timedelta(hours=8))
_HOST = socket.gethostname()
_LOCK = threading.Lock()
_RECENT = {}
_CELL_MAX = 32000                            # Excel's cell limit is 32767 characters


def dash(ic):
    d = re.sub(r"\D", "", str(ic or ""))
    return f"{d[:6]}-{d[6:8]}-{d[8:]}" if len(d) == 12 else d


def myt(v):
    try:
        dt = datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_MYT)
        return dt.astimezone(_MYT).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(v or "")


def classify(url):
    """-> ('Credit report'|'Member search'|None, query dict)."""
    u = urllib.parse.urlparse(url or "")
    q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
    if "/api/loans/credit-report" in u.path:
        return "Credit report", q
    if re.search(r"/api/members/?$", u.path):
        return "Member search", q
    return None, q


def _json(body):
    raw = json.dumps(body, ensure_ascii=False)
    return raw if len(raw) <= _CELL_MAX else raw[:_CELL_MAX] + "...(truncated)"


def _style_header(ws, cols):
    ws.append([c for c, _w in cols])
    for i, (_c, w) in enumerate(cols, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
        cell = ws.cell(row=1, column=i)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"


def _open(path):
    if os.path.exists(path):
        return openpyxl.load_workbook(path)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Customers"
    _style_header(ws, CUSTOMERS)
    _style_header(wb.create_sheet("History"), HISTORY)
    return wb


def _find_row(ws, ic):
    for r in range(2, ws.max_row + 1):
        if re.sub(r"\D", "", str(ws.cell(row=r, column=1).value or "")) == ic:
            return r
    return None


def _apply(wb, ev, reports_dir=None):
    """Apply one captured API response to the workbook. Returns True if anything was recorded."""
    kind, q = classify(ev.get("url"))
    if not kind or ev.get("status") != 200:
        return False
    body = ev.get("body")
    b = body if isinstance(body, dict) else {}
    at = myt(ev.get("at"))
    ws, hist = wb["Customers"], wb["History"]

    ic, name, phone, search, member_json = "", "", "", "", None
    if kind == "Member search":
        rows = b.get("results") if isinstance(b.get("results"), list) else (body if isinstance(body, list) else [])
        search = ", ".join(f"{k}={v}" for k, v in q.items() if k not in ("page", "page_size") and v) or "(all members)"
        wanted = re.sub(r"\D", "", q.get("ic") or q.get("ic_number") or "")
        match = [r for r in rows if wanted and re.sub(r"\D", "", r.get("ic_number") or "") == wanted]
        if len(match) == 1 or (wanted and rows and len(rows) == 1):
            m = match[0] if match else rows[0]
            ic = re.sub(r"\D", "", m.get("ic_number") or wanted)
            name = m.get("name") or m.get("full_name") or ""
            phone = m.get("phone") or m.get("phone_number") or ""
            member_json = _json(m)
        elif wanted:
            ic = wanted                                        # searched but KR883 returned nothing usable
    else:
        ic = re.sub(r"\D", "", q.get("ic_number") or "")
        cust = b.get("customer_details") or {}
        name = cust.get("name") or ""
        ic = ic or re.sub(r"\D", "", cust.get("ic_number") or "")

    host = ev.get("computer") or _HOST
    hist.append([hist.max_row, at, host, kind, dash(ic), name, search or f"ic_number={dash(ic)}"])
    if not ic:                                                 # a browse/filter without one clear customer
        return True

    r = _find_row(ws, ic)
    if r is None:
        ws.append([""] * len(CUSTOMERS))
        r = ws.max_row
        ws.cell(row=r, column=COL["First searched (MYT)"]).value = at
        ws.cell(row=r, column=COL["Times searched"]).value = 0
    ws.cell(row=r, column=COL["IC Number"]).value = dash(ic)
    ws.cell(row=r, column=COL["IC Number"]).number_format = "@"
    ws.cell(row=r, column=COL["Last searched (MYT)"]).value = at
    ws.cell(row=r, column=COL["Computer"]).value = host
    if kind == "Credit report":                                # one credit-report pull = one search
        ws.cell(row=r, column=COL["Times searched"]).value = (ws.cell(row=r, column=COL["Times searched"]).value or 0) + 1
    if name:
        ws.cell(row=r, column=COL["Name"]).value = name
    if phone:
        ws.cell(row=r, column=COL["Phone"]).value = phone
        ws.cell(row=r, column=COL["Phone"]).number_format = "@"
    if member_json:
        ws.cell(row=r, column=COL["Member record (JSON)"]).value = member_json

    if kind == "Credit report":                                # newest report REPLACES the stored one
        cust = b.get("customer_details") or {}
        loans, txs = b.get("loan_details") or [], b.get("transaction_log") or []
        pay = next((t for t in txs if (t.get("transaction_type") or "") == "Loan Repayment"), None)
        last = loans[0] if loans else {}
        vals = {
            "Blacklist count": cust.get("blacklist_count", ""), "Overdue days": cust.get("active_overdue_days", ""),
            "Total loans": len(loans), "Completed loans": sum(1 for l in loans if l.get("loan_status") == "Completed"),
            "Disbursed loans": sum(1 for l in loans if l.get("loan_status") == "Disbursed"),
            "Latest loan status": last.get("loan_status", ""),
            "Latest loan date": myt(last.get("created_on")) if last.get("created_on") else "",
            "Last repayment date": myt(pay.get("date")) if pay else "", "Transactions": len(txs),
            "Credit report updated (MYT)": at, "Credit report (JSON)": _json(body),
        }
        if reports_dir:                                        # the FULL report, uncut, one file per customer
            os.makedirs(reports_dir, exist_ok=True)
            tmp = os.path.join(reports_dir, ic + ".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False)
            os.replace(tmp, os.path.join(reports_dir, ic + ".json"))
            if len(json.dumps(body, ensure_ascii=False)) > _CELL_MAX:
                vals["Credit report (JSON)"] = "Too large for an Excel cell - full report in data/reports/" + ic + ".json"
        for k, v in vals.items():
            ws.cell(row=r, column=COL[k]).value = v
    return True


def record(path, ev):
    """Record one captured KR883 API response. Returns True if it was applied. Never raises."""
    try:
        kind, q = classify(ev.get("url"))
        if not kind or ev.get("status") != 200:
            return False
        key = (kind, json.dumps(q, sort_keys=True))
        try:                                                   # event time, so replayed (queued) events dedupe correctly
            now = datetime.datetime.fromisoformat(str(ev.get("at")).replace("Z", "+00:00")).timestamp()
        except Exception:
            now = datetime.datetime.now().timestamp()
        with _LOCK:
            if abs(now - _RECENT.get(key, -1e9)) < 3:
                return False                                   # the UI fires the same request twice
            _RECENT[key] = now
            pending = path + ".pending.jsonl"
            queued = []
            if os.path.exists(pending):
                with open(pending, encoding="utf-8") as f:
                    queued = [json.loads(l) for l in f if l.strip()]
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                wb = _open(path)
                for e in queued + [ev]:
                    _apply(wb, e, os.path.join(os.path.dirname(path) or ".", "reports"))
                wb["Customers"].auto_filter.ref = wb["Customers"].dimensions
                wb.save(path)
                if queued:
                    os.remove(pending)
            except PermissionError:                            # open in Excel
                with open(pending, "a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False
