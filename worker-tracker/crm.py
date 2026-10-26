"""LG-CRM sync: reliably push what workers do to LG-CRM.

Every loan opened / disbursed / repayment, every other save, every credit-report search, new KR883 accounts and
a rolling per-worker daily summary become an *event* in a local outbox (table `outbox` in tracker.db). A sender
loop delivers them in batches to the LG-CRM endpoint, retrying with back-off, so nothing is lost when LG-CRM
or the internet is down. Delivery is at-least-once; every event has a unique `event_id` so LG-CRM can ignore
duplicates. The wire format is documented in LG-CRM-INTEGRATION.md.

Connection settings live in crm.json (gitignored - it holds the API token):
    {"enabled": true, "url": "https://crm.example.com/api/worker-tracker/events", "auth": "bearer", "token": "..."}
"""
import asyncio, datetime, hashlib, json, os, re, sqlite3, threading, uuid

SCHEMA = "worker-tracker/1"
BATCH = 50
_LOCK = threading.Lock()
_MYT = datetime.timedelta(hours=8)
_ID_IN_PATH = re.compile(r"/loans?/([^/]+)/", re.I)
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "crm.json")
_DEFAULT = {"enabled": False, "url": "", "auth": "bearer", "token": "", "header": "Authorization"}
STATE = {"lastOk": "", "lastError": "", "lastErrorAt": ""}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ config
def load_config():
    c = dict(_DEFAULT)
    try:
        with open(CONFIG, encoding="utf-8") as f:
            c.update({k: v for k, v in json.load(f).items() if k in _DEFAULT})
    except Exception:
        pass
    return c


def save_config(patch):
    """Merge a patch ({enabled,url,auth,token,header}); an empty token / url in the patch leaves the old value."""
    c = load_config()
    for k, v in patch.items():
        if k not in _DEFAULT:
            continue
        if k in ("token", "url") and v in ("", None):
            continue
        c[k] = bool(v) if k == "enabled" else str(v).strip()
    if c["url"] and not re.match(r"^https?://", c["url"]):
        raise ValueError("The LG-CRM address must start with https:// (or http://)")
    if c["auth"] not in ("bearer", "header", "none"):
        raise ValueError("auth must be bearer, header or none")
    with _LOCK, open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=1)
    return c


def public_config():
    """Config for the dashboard - never includes the token."""
    c = load_config()
    return {"enabled": c["enabled"], "url": c["url"], "auth": c["auth"], "header": c["header"], "hasToken": bool(c["token"])}


# ------------------------------------------------------------------ outbox
def _conn(db):
    c = sqlite3.connect(db, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init(db):
    with _LOCK, _conn(db) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, type TEXT NOT NULL, occurred_at TEXT NOT NULL,
            payload TEXT NOT NULL, created TEXT NOT NULL, attempts INTEGER DEFAULT 0, next_try TEXT, sent_at TEXT,
            last_error TEXT, dead INTEGER DEFAULT 0)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox(sent_at, dead, next_try)")
        c.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")


def loan_id_of(path, request, response):
    """Best-effort KR883 loan id from the request address / body / reply (None when it cannot be told)."""
    m = _ID_IN_PATH.search(path or "")
    if m and m.group(1).lower() not in ("credit-report", "summary"):
        return m.group(1)
    for d in (request, response):
        if isinstance(d, dict):
            for k in ("loan", "loan_id", "loan_uuid"):
                if d.get(k) not in (None, ""):
                    return str(d[k])
    if isinstance(response, dict) and response.get("id") not in (None, "") and re.search(r"/loans?/?$", path or ""):
        return str(response["id"])
    return None


def enqueue(db, etype, at, source, data, event_id=None):
    """Add one event to the outbox (only when an LG-CRM address is configured). Never raises."""
    try:
        if not load_config()["url"]:
            return False
        try:
            d = datetime.datetime.fromisoformat(str(at).replace("Z", "+00:00"))
            d = d.astimezone(datetime.timezone.utc).replace(tzinfo=None) if d.tzinfo else d
        except Exception:
            d = _now()
        env = {"schema": SCHEMA, "event_id": event_id or str(uuid.uuid4()), "type": etype, "occurred_at": _iso(d),
               "occurred_at_myt": (d + _MYT).strftime("%Y-%m-%d %H:%M:%S"), "source": source, **data}
        with _LOCK, _conn(db) as c:
            c.execute("INSERT OR IGNORE INTO outbox(event_id,type,occurred_at,payload,created,next_try) VALUES(?,?,?,?,?,?)",
                      (env["event_id"], etype, env["occurred_at"], json.dumps(env, ensure_ascii=False), _iso(_now()), _iso(_now())))
        return True
    except Exception:
        return False


def status(db):
    with _LOCK, _conn(db) as c:
        q = lambda sql: c.execute(sql).fetchone()[0]
        out = {"pending": q("SELECT COUNT(*) FROM outbox WHERE sent_at IS NULL AND dead = 0"),
               "failed": q("SELECT COUNT(*) FROM outbox WHERE dead = 1"),
               "sent": q("SELECT COUNT(*) FROM outbox WHERE sent_at IS NOT NULL")}
        last = c.execute("SELECT type, last_error, attempts FROM outbox WHERE sent_at IS NULL AND last_error IS NOT NULL ORDER BY id LIMIT 1").fetchone()
    out.update(STATE)
    out["stuck"] = dict(last) if last else None
    return out


def retry_failed(db):
    with _LOCK, _conn(db) as c:
        n = c.execute("UPDATE outbox SET dead = 0, attempts = 0, next_try = ? WHERE dead = 1", (_iso(_now()),)).rowcount
    return n


def _prune(db):
    cut = _iso(_now() - datetime.timedelta(days=14))
    with _LOCK, _conn(db) as c:
        c.execute("DELETE FROM outbox WHERE sent_at IS NOT NULL AND sent_at < ?", (cut,))


# ------------------------------------------------------------------ delivery
def _headers(cfg, batch_id):
    h = {"Content-Type": "application/json", "Idempotency-Key": batch_id, "User-Agent": "worker-tracker/1"}
    if cfg["token"] and cfg["auth"] == "bearer":
        h["Authorization"] = "Bearer " + cfg["token"]
    elif cfg["token"] and cfg["auth"] == "header":
        h[cfg["header"] or "X-API-Key"] = cfg["token"]
    return h


async def deliver_once(db, session):
    """Send one batch of due events. Returns the number delivered (0 = nothing due / failed)."""
    cfg = load_config()
    if not (cfg["enabled"] and cfg["url"]):
        return 0
    now = _iso(_now())
    with _LOCK, _conn(db) as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM outbox WHERE sent_at IS NULL AND dead = 0 AND next_try <= ? ORDER BY id LIMIT ?", (now, BATCH))]
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    body = {"schema": SCHEMA, "sent_at": now, "events": [json.loads(r["payload"]) for r in rows]}
    batch_id = hashlib.sha1("".join(r["event_id"] for r in rows).encode()).hexdigest()
    err = None
    try:
        async with session.post(cfg["url"], json=body, headers=_headers(cfg, batch_id)) as r:
            status_code = r.status
            text = (await r.text())[:200]
        if 200 <= status_code < 300:
            with _LOCK, _conn(db) as c:
                c.execute(f"UPDATE outbox SET sent_at = ?, last_error = NULL WHERE id IN ({','.join('?' * len(ids))})", [now] + ids)
            STATE.update(lastOk=now, lastError="", lastErrorAt="")
            return len(ids)
        err = f"HTTP {status_code}: {text}"
        permanent = 400 <= status_code < 500 and status_code not in (408, 409, 425, 429)
    except Exception as e:
        err, permanent = f"{type(e).__name__}: {str(e)[:150]}", False
    STATE.update(lastError=err, lastErrorAt=now)
    with _LOCK, _conn(db) as c:
        for r in rows:
            n = r["attempts"] + 1
            wait = min(3600, 5 * 2 ** min(n, 10))
            c.execute("UPDATE outbox SET attempts = ?, last_error = ?, next_try = ?, dead = ? WHERE id = ?",
                      (n, err, _iso(_now() + datetime.timedelta(seconds=wait)), 1 if (permanent and n >= 5) else 0, r["id"]))
    return 0


async def sender_loop(db, make_session):
    """Runs forever inside the server: deliver due events every few seconds, tidy old rows hourly."""
    init(db)
    tick = 0
    while True:
        try:
            async with make_session() as s:
                while (await deliver_once(db, s)) == BATCH:      # a full batch: more may be waiting
                    pass
            tick += 1
            if tick % 720 == 0:
                _prune(db)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            STATE.update(lastError=f"{type(e).__name__}: {str(e)[:150]}", lastErrorAt=_iso(_now()))
        await asyncio.sleep(5)


# ------------------------------------------------------------------ rolling daily summaries
def summaries(db, store_mod, rules, settings, source_computer=""):
    """Queue a `worker.summary` for every worker active today whose numbers changed since the last one sent."""
    today = (_now() + _MYT).strftime("%Y-%m-%d")
    board = store_mod.leaderboard(db, today, rules, settings)
    n = 0
    for b in board:
        d = store_mod.day(db, today, b["worker"], rules, settings)
        data = {"date": today, "worker": b["worker"], "active_minutes": d["activeMinutes"], "first_active": d["firstActive"],
                "last_active": d["lastActive"], "credit_reports": d["searches"], "customers_searched": d["customers"],
                "loans_opened": d["opened"], "loans_disbursed": d["disbursed"], "repayments": d["repayments"],
                "saves": d["actions"], "active_minutes_by_hour": [h["activeMin"] for h in d["hours"]]}
        h = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()
        key = f"summary:{today}:{b['worker']}"
        with _LOCK, _conn(db) as c:
            row = c.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        if row and row[0] == h:
            continue
        if enqueue(db, "worker.summary", _iso(_now()), {"worker": b["worker"]}, data):
            with _LOCK, _conn(db) as c:
                c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, h))
            n += 1
    return n
