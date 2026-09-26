"""UI-driven Credit Report lookups (thin CLI over credit_report_provider.UiCreditReportProvider).

  python tools/ui_lookup.py 910113125882 960924085285 ...        # headless (default)
  python tools/ui_lookup.py --headed --slow-mo 300 --file ics.txt --out reports.json

Per IC: Member List -> IC filter -> Apply -> row Actions (3 dots) -> Credit report -> popup ->
extract -> close popup -> next.  One browser for the whole batch, in data/kw_profile_workflow.
Never logs in and never touches OTP: on an expired session it stops with exit code 3
(MANUAL_REAUTH_REQUIRED) - authenticate with  python tools/record_workflow.py  and re-run.
"""
import argparse, asyncio, json, os, random, re, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from credit_report_provider import UiCreditReportProvider, ReauthRequired, MANUAL_REAUTH_REQUIRED  # noqa: E402

BASE = (os.environ.get("KW388_BASE_URL") or "https://admin.kr883.com").rstrip("/")
PROFILE = os.environ.get("KW388_WORKFLOW_PROFILE", os.path.join(HERE, "data", "kw_profile_workflow"))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ics", nargs="*")
    ap.add_argument("--file", help="text file with one IC per line")
    ap.add_argument("--out", help="write full JSON results here")
    ap.add_argument("--headed", action="store_true", help="show the browser (default is headless)")
    ap.add_argument("--slow-mo", type=int, default=0, help="ms pause between UI actions (useful with --headed)")
    ap.add_argument("--delay-min", type=float, default=3.0)
    ap.add_argument("--delay-max", type=float, default=5.0)
    a = ap.parse_args()
    ics = list(a.ics)
    if a.file:
        ics += [l.strip() for l in open(a.file, encoding="utf-8") if l.strip()]
    ics = [re.sub(r"\D", "", i) for i in ics if re.sub(r"\D", "", i)]
    if not ics:
        ap.error("give at least one IC")

    prov = UiCreditReportProvider(BASE, PROFILE, headless=not a.headed, slow_mo=a.slow_mo, log=print)
    results, t0 = [], time.time()
    try:
        await prov.start()
        pid_before = id(prov.ctx)
        for i, ic in enumerate(ics):
            r = await prov.fetch(ic)                # ReauthRequired propagates -> queue stops
            results.append(r)
            cd = (r.get("report") or {}).get("customer_details") or {}
            tm = r["timings"]
            print(f"[{i + 1}/{len(ics)}] {ic} {r['status']:<10} total {tm['totalMs']:>5} ms  member-search {tm.get('memberSearchMs', 0):>5} ms  "
                  f"popup {tm.get('popupMs', '-'):>5}" + (f"  {cd.get('name', '')}: {len(r['report'].get('loan_details') or [])} loans"
                  if r["status"] == "ok" else f"  {r.get('error', '')}") + (f"  attempts={r['attempts']}" if r.get("attempts", 1) > 1 else ""))
            if i < len(ics) - 1 and a.delay_max > 0:
                await asyncio.sleep(random.uniform(a.delay_min, a.delay_max))
        print(f"\nbrowser launches: {prov.launches}   same context throughout: {id(prov.ctx) == pid_before}")
    except ReauthRequired as e:
        print(f"\nSTOPPED: {MANUAL_REAUTH_REQUIRED} - {e}\nRun  python tools/record_workflow.py , log in by hand, close the window, then re-run.")
        sys.exit(3)
    finally:
        await prov.close()
    print("metrics:", json.dumps(prov.metrics.as_dict()))
    print(f"wall time {time.time() - t0:.0f}s")
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)
        print("wrote " + a.out)


if __name__ == "__main__":
    asyncio.run(main())
