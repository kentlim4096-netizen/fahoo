"""A stand-in for LG-CRM, for testing the tracker's sync. NOT for production.

    python mock_crm.py [--port 8790] [--token SECRET] [--fail-first N]

Accepts POST /events like the real LG-CRM endpoint should (see LG-CRM-INTEGRATION.md): checks the bearer
token, ignores duplicate event_ids (idempotent), prints one line per event. --fail-first N answers the first N
requests with HTTP 503 so the tracker's retry can be watched.
"""
import argparse, json

from aiohttp import web

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8790)
ap.add_argument("--token", default="test-token")
ap.add_argument("--fail-first", type=int, default=0)
A = ap.parse_args()
SEEN, STATE = {}, {"requests": 0}


async def events(request):
    STATE["requests"] += 1
    if A.token and request.headers.get("Authorization") != "Bearer " + A.token:
        return web.json_response({"error": "unauthorized"}, status=401)
    if STATE["requests"] <= A.fail_first:
        print(f"request #{STATE['requests']}: answering 503 on purpose", flush=True)
        return web.json_response({"error": "try later"}, status=503)
    body = await request.json()
    new = dup = 0
    for e in body.get("events", []):
        if e["event_id"] in SEEN:
            dup += 1
            continue
        SEEN[e["event_id"]] = e
        new += 1
        who = (e.get("source") or {}).get("worker", "")
        loan = e.get("loan") or {}
        extra = f" {loan.get('customer', {}).get('name')} amount={loan.get('amount')} on_hand={loan.get('on_hand_amount')} tenure={loan.get('tenure_days')}" if loan else ""
        print(f"  {e['occurred_at_myt']}  {e['type']:<18} {who}{extra}", flush=True)
    print(f"request #{STATE['requests']}: {new} new, {dup} duplicate", flush=True)
    return web.json_response({"ok": True, "accepted": new, "duplicates": dup})


async def dump(request):
    return web.json_response(list(SEEN.values()))


app = web.Application()
app.add_routes([web.post("/events", events), web.get("/dump", dump)])
web.run_app(app, host="127.0.0.1", port=A.port, print=lambda *_: None)
