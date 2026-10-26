# Worker Tracker

Records what each worker does in the KR883 website - which customers they search (and the credit report that
came back), loans opened / disbursed, repayments keyed in, active time - and shows it on one dashboard, with
alerts and Excel exports.

```
Worker PC 1 ─ Chrome + extension ─┐
Worker PC 2 ─ Chrome + extension ─┼─ https://<ngrok-domain>/tracker/activity ─►  Tracker PC (runs fahoo + tracker)
Tracker PC  ─ Chrome + extension ─┘                                                 │
                                                          dashboard / customers / Excel (admin login)
```

* **Tracker PC** (the one that runs the fahoo scraper and the ngrok link): runs `server.py` on `127.0.0.1:8780`.
  fahoo forwards `/tracker/...` to it, so the existing ngrok link serves both. The dashboard and customer data
  need the fahoo **admin** login. Only `/tracker/activity` is open to the extensions, and it needs the shared key.
* **Every computer being tracked** (including the tracker PC itself): the Chrome extension in
  `extension/kr883-activity-logger`.

Holds full customer ICs and worker activity. Keep the repo **private**, never commit `config.json`,
`extension/**/config.js` or `data/`, and tell your staff they are being tracked (PDPA).

## Tracker PC (once)
1. fahoo must be running (`run-local.ps1`, which sets up its `.venv`) with its ngrok link. Nothing else to
   configure: fahoo already forwards `/tracker/...`. The tracker uses fahoo's Python environment.
2. Run `python make_config.py` once (in this folder): it creates `config.json` holding the shared **key**
   (never overwritten).
3. Start the tracker: `.\start-hidden.vbs`. To start it at every Windows logon, register `start-hidden.vbs` as
   a scheduled task (logon + every 5 min); it does nothing if already running.
4. Dashboard: `https://<ngrok-domain>/tracker/dashboard` (or `http://127.0.0.1:8780/dashboard` on this PC).

## Each tracked computer
```
git clone -b worker-tracker https://github.com/kentlim4096-netizen/fahoo.git
cd fahoo\worker-tracker
python install_extension.py --server https://<ngrok-domain>/tracker --key <KEY> --computer <NAME>
```
Then in Chrome (the profile used for KR883): `chrome://extensions` -> **Developer mode** -> **Load unpacked** ->
`worker-tracker\extension\kr883-activity-logger` -> reload the KR883 tab. On the tracker PC itself use
`--server local` instead.

Full steps for Claude to follow: see [CLAUDE.md](CLAUDE.md).

## What you get
* **Dashboard** - active time (30 s heartbeats while the KR883 tab is in use), loans opened / disbursed,
  repayments, customers searched, idle gaps, late start / early finish, team table, 14-day history, daily Excel.
* **Alerts** (bell button) - loan opened / disbursed / new KR883 account, as a Windows pop-up on the tracker PC
  and a dashboard pop-up with sound. Each can be switched on or off.
* **What each save means** - KR883's save requests are listed on the dashboard; label each as opened /
  disbursed / repayment / other / ignore, and it applies to all data and alerts.
* **Customers** - `/tracker/view`: the stored credit report per customer (same layout as fahoo's report page),
  updated on every new search.  **Excel** - `/tracker/tracker.xlsx`.
* Data lives in `data/` (`tracker.db`, `worker_tracker.xlsx`, `reports/`, `settings.json`) - local, not in git.


## LG-CRM sync
Loan opened / disbursed / repayment events, other saves, searches, new accounts and daily summaries are pushed
to LG-CRM automatically (queued + retried). Set it up in the dashboard's **LG-CRM sync** section. What LG-CRM
must implement and the exact JSON are in [LG-CRM-INTEGRATION.md](LG-CRM-INTEGRATION.md); `mock_crm.py` is a fake
LG-CRM for testing.

## Date pickers
Dashboard: any day (arrows / date box). Customers page (`/view`): filter by the dates customers were last searched.

## Update
`git pull` on the tracker PC, then restart the tracker (`Stop-Process` its python, run `start-hidden.vbs`).
Extension changes: pull on each PC and click reload on `chrome://extensions`.
