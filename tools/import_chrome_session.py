"""Copy your KR883 login from your running Chrome into the scraper, so the scraper works
headlessly (hidden) while you keep using KR883 in Chrome.

KR883 is a single-page app that keeps its auth as JWT tokens in localStorage (kw388_access /
kw388_refresh / kw388_profile), not cookies. This reads those from your Chrome over its debug
port and saves them to data/kw_session.json. The scraper injects them into its own headless
browser before the page loads, so the SPA comes up already logged in - no password, no TOTP, and
no second login that would kick you out (KR883 allows one session per account).

Chrome must be running with --remote-debugging-port=9222 (tools/launch-chrome-debug.ps1), and you
must be logged in to KR883 in it.

    python tools/import_chrome_session.py
"""
import asyncio, datetime, json, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import scraper_service as s

CDP = os.environ.get("KW388_CDP_URL") or "http://127.0.0.1:9222"


async def main():
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp(CDP)
        except Exception as e:
            print(f"Could not reach Chrome's debug port at {CDP}.")
            print("Run tools/launch-chrome-debug.ps1, log in to KR883, then run this again.")
            print(f"  ({e})")
            sys.exit(1)

        ua = ""
        try:
            ua = (await (await browser.new_browser_cdp_session()).send("Browser.getVersion")).get("userAgent", "")
        except Exception:
            pass

        page = None
        for ctx in browser.contexts:
            for pg in ctx.pages:
                if "kr883" in pg.url:
                    page = pg
                    break
        if page is None:
            print("No KR883 tab open in Chrome. Log in to KR883 in the debug-port Chrome first.")
            await browser.close(); sys.exit(1)

        data = await page.evaluate("""() => ({
            ls: Object.fromEntries(Object.entries(localStorage)),
            cookie: document.cookie
        })""")
        await browser.close()

    ls = data["ls"]
    if not any(k.startswith("kw388") for k in ls):
        print(f"KR883 tab has no auth tokens in localStorage (keys: {list(ls)}). Are you logged in?")
        sys.exit(1)
    print("Captured localStorage keys:", list(ls.keys()))

    os.makedirs(os.path.dirname(s.KW_SESSION_FILE), exist_ok=True)
    with open(s.KW_SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump({"userAgent": ua, "origin": s.KW_BASE_URL, "localStorage": ls,
                   "cookies": [], "capturedAt": datetime.datetime.now().isoformat()}, f, indent=2)
    print(f"saved -> {s.KW_SESSION_FILE}")

    # Verify the headless scraper comes up logged in with these tokens.
    print("verifying headless scraper with the copied session...")
    from playwright.async_api import async_playwright as ap2
    async with ap2() as p:
        ctx, close = await s.open_browser(p, s.PROFILE_DIR)
        try:
            page = await s.acquire_page(ctx)
            await page.goto(f"{s.KW_BASE_URL}/reports/credit", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(3500)
            if await s._is_cloudflare_block(page):
                print("  BLOCKED by Cloudflare - is the VPN connected?"); sys.exit(2)
            if "login" in page.url:
                print(f"  tokens didn't authenticate ({page.url}). They may have expired; re-import."); sys.exit(1)
            box = await page.get_by_placeholder("Enter IC number", exact=False).count()
            print(f"  landed on {page.url} | IC search box: {bool(box)}")
            print("\nSESSION IMPORTED AND VERIFIED. The hidden scraper is logged in.")
            print("Keep using KR883 in Chrome. If a scrape later says the session expired, re-run this.")
        finally:
            await close()


if __name__ == "__main__":
    asyncio.run(main())
