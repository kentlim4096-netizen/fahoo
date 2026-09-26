"""Helpers for the user's own Chrome: locate it, list its profiles, start it with the debug port.

The tool mirrors the KR883 session of the normal Chrome, so that Chrome must be started with
--remote-debugging-port=9222. Chrome only opens the port at startup, and we NEVER close the user's
Chrome for them - if it is already open without the port we say so and let them close it.
"""
import json, os, subprocess, urllib.request

DEBUG_PORT = 9222


def find_chrome():
    cands = [os.environ.get("KW388_CHROME_EXE", ""),
             os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
             os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe")]
    return next((c for c in cands if c and os.path.exists(c)), None)


def user_data_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")


def list_profiles():
    """[(folder_name, label)] from Chrome's own Local State, e.g. ('Profile 1', 'Profile 1 - me@gmail.com')."""
    out = []
    try:
        with open(os.path.join(user_data_dir(), "Local State"), encoding="utf-8") as f:
            cache = json.load(f).get("profile", {}).get("info_cache", {})
        for folder, info in cache.items():
            if not os.path.isdir(os.path.join(user_data_dir(), folder)):
                continue                                   # stale entry Chrome no longer has on disk
            who = info.get("user_name") or info.get("name") or ""
            out.append((folder, f"{folder} - {who}" if who else folder, bool(info.get("user_name"))))
    except Exception:
        pass
    return [(f, l) for f, l, _acct in sorted(out)] or [("Default", "Default")]


def default_profile(current=None):
    """Which profile to preselect: the saved one, else 'Default', else the first with a Google account."""
    profs = list_profiles()
    folders = [f for f, _l in profs]
    if current in folders:
        return current
    if "Default" in folders:
        return "Default"
    try:
        with open(os.path.join(user_data_dir(), "Local State"), encoding="utf-8") as f:
            cache = json.load(f).get("profile", {}).get("info_cache", {})
        for folder in folders:
            if cache.get(folder, {}).get("user_name"):
                return folder
    except Exception:
        pass
    return folders[0]


def debug_port_open():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version", timeout=2)
        return True
    except Exception:
        return False


def chrome_running():
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"], capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    return "chrome.exe" in r.lower()


def start_debug_chrome(profile="Default", url="https://admin.kr883.com"):
    """-> (ok, message). Starts the user's Chrome with the debug port, unless that is impossible."""
    chrome = find_chrome()
    if not chrome:
        return False, "Google Chrome was not found. Install Chrome first."
    if debug_port_open():
        return True, "Chrome is already running with the debug port. Log in to KR883 in it."
    if chrome_running():
        return False, ("Chrome is open WITHOUT the debug port. Save your work, close ALL Chrome windows "
                       "(also check the system tray), then try again.")
    subprocess.Popen([chrome, f"--remote-debugging-port={DEBUG_PORT}", f"--profile-directory={profile}",
                      f"--user-data-dir={user_data_dir()}", url])
    return True, "Chrome started. Log in to KR883 in it (password + OTP) and keep that tab open."
