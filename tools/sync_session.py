"""Synchronize the worker's ALREADY-VALID KR883 session (normal Chrome) into the dedicated profile
and the API session file. Replaces tools/sync_api_session_from_profile.py (which went the other
way and only fed the API side).

    python tools/sync_session.py            # sync normal Chrome -> data/kw_profile_workflow + kw_session.json
    python tools/sync_session.py --status   # report which sessions are valid (never prints tokens)

Never logs in, never touches OTP, never modifies/closes/navigates the worker's Chrome (one
read-only localStorage read, or Chrome's on-disk storage). See session_sync.py for the model.
The dedicated profile must not be in use (no running UI scrape / recorder).
"""
import argparse, asyncio, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import scraper_service as s
from session_sync import SessionSync, seconds_left

PROFILE = os.environ.get("KW388_WORKFLOW_PROFILE", os.path.join(HERE, "data", "kw_profile_workflow"))


def fmt(sec):
    return "unknown" if sec is None else ("expired" if sec <= 0 else f"{int(sec // 60)} min left")


async def open_profile(p):
    launch = dict(headless=True, args=s.STEALTH_ARGS, channel="chrome")
    try:
        return await p.chromium.launch_persistent_context(PROFILE, **launch)
    except Exception:
        launch.pop("channel")
        return await p.chromium.launch_persistent_context(PROFILE, **launch)


async def profile_state(p):
    """('authenticated'|'logged-out', seconds-left) for the dedicated profile."""
    ctx = await open_profile(p)
    try:
        await ctx.route(__import__("re").compile(r"/token/refresh/"), lambda r: r.abort())
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await pg.goto(s.KW_BASE_URL + "/members", wait_until="domcontentloaded", timeout=60000)
        await pg.wait_for_timeout(2500)
        if "/login" in pg.url:
            return "logged-out", None
        tok = await pg.evaluate("() => localStorage.getItem('kw388_access') || ''")
        return "authenticated", seconds_left(tok) if tok else None
    finally:
        await ctx.close()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    sync = SessionSync(s.KW_BASE_URL, api_base=s.KW_API_BASE, cdp_url=s.KW_CDP_URL or "http://127.0.0.1:9222",
                       log=lambda m: print("  " + m))
    from playwright.async_api import async_playwright

    src = await sync.find_source()
    print("normal Chrome session :", ("VALID via %s (%s)" % (src[1], fmt(seconds_left(src[0]["kw388_access"])))) if src else "none valid (logged out / expired / not readable)")
    api_tok = (s.load_session() or {}).get("localStorage", {}).get("kw388_access", "")
    print("API session file      :", "server accepts it" if api_tok and await sync.server_accepts(api_tok) else "not accepted / missing")
    async with async_playwright() as p:
        st, left = await profile_state(p)
        print("dedicated UI profile  :", st, ("(%s)" % fmt(left)) if left is not None else "")
        if a.status:
            return
        if not src:
            print("\nNothing to sync: the normal Chrome has no valid KR883 session. "
                  "%s" % ("The dedicated profile is still authenticated, so the UI scraper can run." if st == "authenticated"
                          else "Log in to KR883 in your normal Chrome, then run this again."))
            sys.exit(0 if st == "authenticated" else 2)
        ls, where = src
        ctx = await open_profile(p)
        try:
            await ctx.route(__import__("re").compile(r"/token/refresh/"), lambda r: r.abort())
            pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await pg.goto(s.KW_BASE_URL + "/login", wait_until="domcontentloaded", timeout=60000)
            await sync.inject_into_page(pg, ls)
            await pg.goto(s.KW_BASE_URL + "/members", wait_until="domcontentloaded", timeout=60000)
            try:
                await pg.wait_for_selector("#filter-fields-region", timeout=20000)
                ok = "/login" not in pg.url
            except Exception:
                ok = False
            ua = await pg.evaluate("() => navigator.userAgent")
        finally:
            await ctx.close()
    print("\nsynchronized into dedicated profile:", "VALIDATED (Member List opened, no login)" if ok else "FAILED validation")
    if ok:
        sync.save_api_session(s.KW_SESSION_FILE, ls, ua)
        print("API session file updated to the same session.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
