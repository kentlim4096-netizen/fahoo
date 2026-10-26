# LG-CRM integration (worker tracker -> LG-CRM)

The tracker pushes what workers do to **one HTTPS endpoint you add to LG-CRM**. Events are queued on the
tracker PC and retried until LG-CRM accepts them, so LG-CRM may be down or slow without losing anything.

## 1. Connect it
Dashboard -> **LG-CRM sync** section: enter the endpoint address and API token, choose how the token is sent,
switch **Send to LG-CRM** on, press **Send test event**. Settings are stored in `worker-tracker/crm.json`
(gitignored, holds the token). Nothing is sent until the switch is on; events queue meanwhile.

## 2. What LG-CRM must provide
`POST <your address>` — e.g. `https://lg-crm.example/api/worker-tracker/events`

* **Auth:** `Authorization: Bearer <token>` (default), or the token in an `X-API-Key` header — pick in the dashboard.
* **Request:** `Content-Type: application/json`, header `Idempotency-Key: <hash of the batch>`.
  ```json
  { "schema": "worker-tracker/1", "sent_at": "2026-10-26T03:10:00Z", "events": [ { ...event... }, ... ] }
  ```
  Up to 50 events per request.
* **Response:** any **2xx** = every event in the batch is stored (the tracker then never resends them).
  **5xx / 408 / 429 / network error** = retried with back-off (10 s, 20 s, 40 s ... max 1 h) for as long as it takes.
  **Other 4xx** = the batch is rejected; after 5 tries those events are parked as "failed" on the dashboard
  (a **Retry failed** button re-queues them). So answer 2xx for events you merely don't care about.
* **Idempotent:** delivery is *at least once*. Store/upsert by `event_id` (unique, a UUID) and ignore repeats.
* Order is roughly chronological but not guaranteed; use `occurred_at`.

## 3. Event envelope (every event)
| field | meaning |
|---|---|
| `schema` | always `worker-tracker/1` |
| `event_id` | unique UUID - dedupe on this |
| `type` | see section 4 |
| `occurred_at` / `occurred_at_myt` | when it happened, UTC ISO / Malaysia time `YYYY-MM-DD HH:MM:SS` |
| `source.worker` | the KR883 account that did it (e.g. `ONEWTL`) |
| `source.computer` | the tracked computer's name (e.g. `WORKER-PC-2`) |

## 4. Event types
**`loan.opened` / `loan.disbursed` / `loan.repayment`** — the loan lifecycle. Use these to track each loan.
```json
{ "schema": "worker-tracker/1", "event_id": "8f0c...", "type": "loan.disbursed",
  "occurred_at": "2026-10-26T03:04:05Z", "occurred_at_myt": "2026-10-26 11:04:05",
  "source": { "worker": "ONEWTL", "computer": "WORKER-PC-2" },
  "loan": { "kr883_loan_id": "L-100",
            "customer": { "name": "CRM TEST CUSTOMER", "ic": "900202-02-2222", "kr883_member_id": "777" },
            "amount": "3000.00", "on_hand_amount": "2700.00", "tenure_days": "30" },
  "result": { "ok": true, "http_status": 200 },
  "request": { "method": "POST", "path": "/api/loans/L-100/disburse/" },
  "raw_request": { "member": 777 } }
```
* For `loan.repayment`, `loan.amount` is the **repayment amount keyed in**.
* `result.ok = false` means KR883 rejected the save (the worker *tried*); keep it in the log, don't advance the loan.
* Amounts are strings (no float rounding). Any field may be `null` when KR883 did not provide it.

**`activity.search`** — a worker pulled a customer's credit report: `customer{name,ic}`, `blacklist_count`,
`overdue_days`, `loans_total`.

**`activity.action`** — any other save the worker made in KR883: `request{method,path}`, `result`, `customer{ic}`.

**`worker.new_account`** — a KR883 login never seen on any tracked computer before.

**`worker.summary`** — rolling daily numbers per worker (sent every ~10 min while they change; **upsert** by
`worker` + `date`): `date, worker, active_minutes, first_active, last_active, credit_reports, customers_searched,
loans_opened, loans_disbursed, repayments, saves, active_minutes_by_hour[24]`.

**`test.ping`** — from the dashboard's *Send test event* button.

## 5. Suggested LG-CRM model
* `worker_events` — append-only log: one row per event (`event_id` unique, `type`, `occurred_at`, worker, computer, JSON payload). This alone gives the **activity log**.
* `loans` — upsert by `loan.kr883_loan_id` (fallback: customer IC + date when it is null): customer, amount, on-hand, tenure, `opened_at/by`, `disbursed_at/by`, status (`opened` -> `disbursed`), repayment total.
* `loan_events` — the timeline per loan (each `loan.*` event, including failed attempts).
* `worker_daily` — upsert by `(worker, date)` from `worker.summary`.
* Notify "us" in LG-CRM when a `loan.opened` / `loan.disbursed` row arrives.

## 6. Test without LG-CRM
```
python mock_crm.py --token test-token --fail-first 2      # a fake LG-CRM on http://127.0.0.1:8790/events
```
Point the dashboard at `http://127.0.0.1:8790/events` with token `test-token`. It answers 503 twice on purpose
so you can watch the retry, ignores duplicate `event_id`s and prints each event.

## 7. Known limits (check on the first real data)
* Which KR883 save means "open loan", "disburse" and "repayment" is **labelled on the dashboard** (*What each save / update means*). Until a worker has done each once and you have confirmed the labels, `loan.*` events may arrive as `activity.action`.
* `kr883_loan_id` is read from the request address / reply. If KR883 does not expose it for a save, it is `null`; correlate by `customer.ic` and time.
* Events are kept 14 days after delivery on the tracker PC, then pruned.
