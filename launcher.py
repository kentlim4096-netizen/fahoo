"""Entry point of the installed Credit Report Tool (CreditReportTool.exe).

    CreditReportTool.exe               start the service (first run: opens the setup window), open the panel
    CreditReportTool.exe --setup       (re)open the settings window
    CreditReportTool.exe --totp        show the live KR883 authenticator code (replaces the phone app)
    CreditReportTool.exe --chrome      start Chrome with the debug port so its KR883 session can be mirrored
    CreditReportTool.exe --stop        stop the running service
    CreditReportTool.exe --no-browser  start without opening the panel in a browser
    CreditReportTool.exe --setup-json f.json   write settings from a JSON file (silent installs / tests)

Settings live in %LOCALAPPDATA%\\CreditReportTool\\config.env (override the folder with CRT_HOME); data,
browser profile and logs live beside it, so the program folder itself is never written to.
"""
import argparse, json, os, subprocess, sys, threading, time, urllib.request

APP = "CreditReportTool"
FROZEN = getattr(sys, "frozen", False)
BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))     # web/, tools/ live here


def home():
    return os.environ.get("CRT_HOME") or os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP)


def load_config(path):
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ[k.strip()] = v.strip()                # config wins over stray environment values


def redirect_logs(h):
    """No console when packaged: send stdout/stderr to a size-capped log file."""
    logdir = os.path.join(h, "logs")
    os.makedirs(logdir, exist_ok=True)
    path = os.path.join(logdir, "service.log")
    try:
        if os.path.exists(path) and os.path.getsize(path) > 5_000_000:
            os.replace(path, path + ".1")
    except OSError:
        pass
    f = open(path, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = f
    return path


def msgbox(title, text):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
    except Exception:
        print(title + ": " + text)


def cmd_chrome():
    """Start the user's own Chrome with the debug port (never closes their Chrome for them)."""
    import chrome_helper
    ok, msg = chrome_helper.start_debug_chrome(os.environ.get("KW388_CHROME_PROFILE", "Default"))
    if not ok or "already running" in msg:
        msgbox("Credit Report Tool", msg)


def cmd_stop(quiet=False):
    me = os.getpid()
    exe = os.path.basename(sys.executable) if FROZEN else "python.exe"
    subprocess.run(["taskkill", "/F", "/T", "/FI", f"IMAGENAME eq {exe}", "/FI", f"PID ne {me}"],
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not quiet:
        msgbox("Credit Report Tool", "Stopped.")


def open_panel_when_ready(port):
    def go():
        import webbrowser
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=1)
                webbrowser.open(f"http://localhost:{port}/")
                return
            except Exception:
                time.sleep(1)
    threading.Thread(target=go, daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--setup-json")
    ap.add_argument("--chrome", action="store_true")
    ap.add_argument("--totp", action="store_true", help="show the live KR883 authenticator code")
    ap.add_argument("--qr-test", metavar="IMAGE", help="decode a TOTP QR image and print issuer/account (never the secret)")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="with --stop: no message box (used by the uninstaller)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()

    h = home()
    os.makedirs(h, exist_ok=True)
    cfg = os.path.join(h, "config.env")
    sys.path.insert(0, BUNDLE)

    if a.stop:
        return cmd_stop(a.quiet)
    if a.qr_test:
        import qr_totp
        ts = qr_totp.qr_from_file(a.qr_test)
        print("no QR found" if not ts else f"QR ok: issuer={ts.issuer} account={ts.account} digits={ts.digits} period={ts.period} "
              f"code_now_len={len(qr_totp.code_now(ts)[0])}")
        return 0 if ts else 1
    if a.totp:
        import setup_wizard
        return setup_wizard.run_totp_window(h)
    if a.setup_json:
        import setup_wizard
        v = json.load(open(a.setup_json, encoding="utf-8"))
        bad = setup_wizard.validate(v)
        if bad:
            print("\n".join(bad)); return 2
        print(setup_wizard.save(h, BUNDLE, v) or "saved"); return 0
    if a.setup or not os.path.exists(cfg):
        import setup_wizard
        if not setup_wizard.run_wizard(h, BUNDLE):
            if not os.path.exists(cfg):
                return 1
        if a.setup:
            return 0
    if a.chrome:
        load_config(cfg)
        return cmd_chrome()

    load_config(cfg)
    data = os.path.join(h, "data")
    os.makedirs(data, exist_ok=True)
    os.environ.setdefault("DATA_DIR", data)
    os.environ.setdefault("KW388_WORKFLOW_PROFILE", os.path.join(data, "kw_profile_workflow"))
    os.environ.setdefault("KW388_SESSION_FILE", os.path.join(data, "kw_session.json"))
    os.environ.setdefault("GOOGLE_SHEETS_CREDENTIALS", os.path.join(h, "google-credentials.json"))
    os.environ.setdefault("KW388_UI_HEADLESS", "true")
    if FROZEN:
        redirect_logs(h)
    port = int(os.environ.get("PORT", "8765"))
    if not a.no_browser:
        open_panel_when_ready(port)
    import scraper_service
    return scraper_service.main()


if __name__ == "__main__":
    sys.exit(main() or 0)
