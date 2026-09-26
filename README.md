# Credit Report Tool (standalone)

A portable, self-contained copy of the credit report tool with a local web control panel.

Runs entirely on its own — upload a customer list, scrape KW388, then download the results as
Excel or push them to Google Sheets. It can optionally post to a backend API instead, so results
land in a shared candidate list; that mode is off by default (`LOCAL_ONLY`).

## Setup — local only, no backend (Windows, no Docker)

Runs the scraper natively against Python + Playwright, with **no backend involved at all**.
Scraped results are written to `data/output/kw_leads_<date>.json` instead of being posted
anywhere. This is the mode to use before the CRM backend is wired up.

1. Install [Python 3.12](https://www.python.org/downloads/).
2. `.\run-local.ps1` — creates a venv, installs dependencies + Chromium, and starts the service.
3. Fill in `KW388_USERNAME`, `KW388_PASSWORD` and `KW388_TOTP_SECRET` in the `.env` it creates.
   Leave every `BACKEND_ADMIN_*` var blank — that's what keeps `LOCAL_ONLY` on.
4. Drop your candidate list `.xlsx` into `data/storage/kw-candidates/`.
5. Re-run `.\run-local.ps1`.

Check what it thinks its configuration is at any time:
```
curl http://localhost:8765/health
```

**The candidate list's tabs must be named as plain numbers** (e.g. `307`, `78`) — those are read
as dated snapshots, newest first. A workbook whose only tab is `Sheet1` parses to zero rows and
the run stops with a warning rather than scraping nothing.

To point it at the backend later, fill in the `BACKEND_ADMIN_*` vars and set `LOCAL_ONLY=false`.

## Setup — Docker (with backend)

1. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/).
2. `cp .env.example .env` and fill in real credentials (KW388 login + TOTP secret, a backend
   admin account, and the backend API's URL). Never commit `.env`.
3. `mkdir -p data/storage/kw-candidates` and copy your candidate list Excel file(s) in there.
4. `docker compose up -d --build`

Check it started cleanly:
```
docker compose logs -f
```

## Using it — the control panel

Open **http://localhost:8765** once the service is running.

**1 · Upload customer list** — drag `.xlsx` files in. The parsed candidate count comes straight
back, so a wrongly-named tab is caught immediately rather than surfacing later as a zero-row
scrape. **Sheet tabs must be named as plain numbers** (`307`, `78`) — those are read as dated
snapshots, newest first. A workbook whose only tab is `Sheet1` parses to zero rows.

**2 · Saved lists** — every list ever uploaded is kept. Untick one to leave it out of the next
scrape without deleting it; tick it to bring it back. Uploads are additive, and even the
"use only the new upload" option just deactivates the older lists rather than removing them.

**3 · Scrape KW388** —
  - *Fast mode*: skips the full loan/transaction detail for anyone with no loan today. Roughly
    250-370/min instead of ~70/min. Their `period` / `recent 5` / `overdue` fields are left as
    they were rather than refreshed.
  - *Fresh* (on by default): re-scrapes everyone, ignoring today's cache. Untick it only to
    continue a run that was interrupted.
  - Live progress bar with rate and time remaining, and the results table fills in as rows are
    scraped rather than only at the end.

**4 · Check one IC** — look up a single customer immediately. Dashes optional. This uses its own
browser session, so it works while a full scrape is running.

**5 · Scraped data** — download what you need:

| Control | Options |
| --- | --- |
| Filter | Took a loan today · Everyone · Overdue only · Loan today & not overdue · No overdue |
| Remark | any code present in the data (`IO`, `O`, `W9`, `SK (i)`, …) |
| Sort | Today · Overdue days · Completed · Disbursed · Open loans · Name A-Z |

Downloads as `.xlsx` or `.json`, with the filter in the filename
(`kw_leads_2026-08-28_loan-today.xlsx`). Click a column heading in the on-page table to sort it.

Every export uses the same columns as the working BLASTER sheet — `Customer, IC, Phone, Today,
Completed, Disbursed, Recent 5 (days/RM), Loans Disbursed Per Date` — plus **column I**, a
per-remark breakdown (`IO: 25, W9: 6, P9: 2`).

**6 · Send to Google Sheets** — optional; see below. The page shows a checklist that ticks off
each setup step as it detects it.

### If a run is interrupted

Nothing is lost. Every scraped record is cached to `data/kw_cache/raw_<date>.json` as it goes.
If a run ends before writing results, the Scraped data section offers a **build results** button
to turn that cache into a normal results file. Phone and loan amount come from the candidate
list, so load the matching list before rebuilding if you want those columns filled.

### Importing the TOTP secret from a QR code

If you have the KW388 2FA setup QR as an image, import it rather than transcribing it by hand:

```
.\.venv\Scripts\python.exe tools\import_totp_qr.py path\to\qr.png
```

It writes `KW388_TOTP_SECRET` into `.env` without ever printing the secret, and prints the code
valid right now so you can confirm it matches your authenticator app. Add `--dry-run` to check a
QR without touching `.env`. Requires `opencv-python-headless`.

### Google Sheets setup

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable
   the **Google Sheets API**.
2. Create a **Service Account**, then **Keys → Add key → JSON**. Save it as
   `google-credentials.json` next to `scraper_service.py` (gitignored).
3. Open that JSON, copy the `client_email` value, and **share your Google Sheet with that address
   as an Editor.** Skipping this is the single most common cause of a 403 on export.
4. Put the spreadsheet ID in `.env` as `GOOGLE_SHEETS_SPREADSHEET_ID` — it's the long segment in
   `docs.google.com/spreadsheets/d/<THIS PART>/edit`.

The target worksheet is **cleared and rewritten** on every export, so point it at a sheet you're
happy to have overwritten.

## Using it — HTTP API

The same operations are available directly on `localhost:8765` if you'd rather script them.

**Trigger a full scrape** (normal mode — every candidate, full history):
```
curl -X POST http://localhost:8765/scrape -H "Content-Type: application/json" -d "{\"mode\":\"all\"}"
```

**Trigger a fast scrape** (skips full history for anyone with no loan found today — much quicker):
```
curl -X POST http://localhost:8765/scrape -H "Content-Type: application/json" -d "{\"mode\":\"all\",\"fast\":true}"
```

Other useful `mode` values: `"new"` (only candidates never seen before) or `"old"` (only
already-known candidates). Add `"limit": 500` to cap how many get scraped in one run.

Every scrape starts fresh by default — today's cache is cleared so all candidates are re-fetched.
Pass `"fresh": false` to continue an interrupted run instead of re-scraping what it already got.

**Check progress:**
```
curl http://localhost:8765/status
```

**Check candidate list status** (what's currently loaded, without scraping anything):
```
curl http://localhost:8765/candidates/preview
```

**Upload a candidate list:**
```
curl -F "files=@my_list.xlsx" "http://localhost:8765/candidates/upload?replace=true"
```

**Check a single IC** (works during a running scrape):
```
curl -X POST http://localhost:8765/check -H "Content-Type: application/json" -d "{\"ic\":\"960924085285\"}"
```

**Download results**, filtered and sorted. `filter` is one of `all`, `today`, `overdue`,
`today_not_overdue`, `clean`; add `remark=IO` to narrow to one channel, `sort=<column>` and
`dir=asc|desc` to order it, `format=json` for raw rows:
```
curl -OJ "http://localhost:8765/results/kw_leads_2026-08-28.json/download?filter=today&sort=overdueDays"
```

**List result files and scrape caches:**
```
curl http://localhost:8765/results
```

**Rebuild results from a scrape cache** (for a run that ended before writing results):
```
curl -X POST http://localhost:8765/results/rebuild -H "Content-Type: application/json" -d "{\"date\":\"2026-08-28\"}"
```

**Export to Google Sheets** (`source` is `results` or `candidates`):
```
curl -X POST http://localhost:8765/export/sheets -H "Content-Type: application/json" -d "{\"source\":\"results\"}"
```

**Config check** — what mode it's in, which credentials are set, where files live:
```
curl http://localhost:8765/health
```

## Notes

- This only runs the KW388 pipeline — nothing else is configured here.
- `data/storage/`, the Chromium profile, and the scrape cache all persist across
  `docker compose down`/`up` — only `docker compose down -v` wipes them.
- If KW388 ever requires connecting through their own VPN to log in, that VPN client would need
  to run on this machine (or as another sidecar container) so this scraper's traffic goes
  through it — ask if you get to that point and need help wiring it in.
