# Instructions for Claude: installing the KR883 Activity Logger on a computer

Use this when the user says things like "install the extension / worker tracker / KR883 activity logger on
this computer". Just do it end to end, no back-and-forth beyond what's listed below as a real blocker.
This computer is a **worker PC** unless the user says it is the tracker PC.

## This deployment's pinned settings (do not ask for these - they are fixed)
* **Server address**: `https://yahoo1818.ngrok.app` - this is ngrok pointed **directly** at the tracker
  server's port 8780 on the tracker PC (not through fahoo's `/tracker` proxy - this repo's own
  `scraper_service.py`/KR883 scraper is not part of this deployment). So the server flag is the bare
  domain, no `/tracker` suffix.
* Live, no-login customer view at `https://yahoo1818.ngrok.app/view` (or `/` - it redirects there). Use this
  to verify a new install worked - no admin login needed, unlike `/dashboard`.

## Ask the user only for
1. **Key** - the shared key from the tracker PC (`worker-tracker/config.json`, field `key`). Can't be
   skipped: it is gitignored on purpose, never committed, so a fresh clone on another computer has no way
   to know it except being told. Never print it back more than needed and never put it in a committed file.
2. **Computer name** - a short label for this PC (e.g. `WORKER-PC-2`), shown on the dashboard. If the user
   doesn't care, default to the machine's hostname (`$env:COMPUTERNAME` / `hostname`).

## Steps - do these automatically, don't just explain them
1. Check for `git` and `python` (`git --version`, `python --version`). If either is missing, **install it
   yourself**: `winget install --id Git.Git -e --accept-package-agreements --accept-source-agreements` and
   `winget install --id Python.Python.3.12 -e --scope user --accept-package-agreements --accept-source-agreements`.
   Winget installs can be `--silent`; still needs the user to click through any UAC prompt for Git (machine
   scope). After either install, PATH changes only apply in a *new* process - re-read
   `[System.Environment]::GetEnvironmentVariable("PATH","User")`/`"Machine"` and merge into `$env:PATH`
   rather than relying on the current shell picking it up.
2. Get the code: `git clone -b worker-tracker https://github.com/kentlim4096-netizen/fahoo.git` into a normal
   folder (e.g. the user's Desktop). If it's already cloned, `git pull` instead. You only need the
   `worker-tracker` folder inside it for this task.
3. From `worker-tracker`: `pip install -r requirements.txt`.
4. From `worker-tracker`, run:
   `python install_extension.py --server https://yahoo1818.ngrok.app --key <key> --computer <name>`
   (Tracker PC itself instead: `--server local`, no key needed - and see `README.md` for also running the
   tracker server and ngrok locally.)
   This writes `extension/kr883-activity-logger/config.js`. **Never commit `config.js`** (it is gitignored).
5. The Chrome part cannot be automated by a normal script, but if you (Claude) have browser control
   available in this session, drive it yourself instead of just telling the user the clicks: open
   `chrome://extensions`, switch on **Developer mode**, **Load unpacked**, choose the path
   `install_extension.py` printed (`worker-tracker\extension\kr883-activity-logger`), then open/reload the
   KR883 tab. If you have no browser control, tell the user those exact steps once, clearly, in order.
6. Verify: ask the user to search any customer on the real KR883 site in that browser profile, then check
   `https://yahoo1818.ngrok.app/view` (no login) shows that customer with a recent "last searched" time. If
   not: check `chrome://extensions` -> the extension's "service worker" / Errors console; confirm the key
   matches the tracker PC's `config.json`; confirm the tracker PC and its ngrok tunnel are both up (ask the
   user, or check `https://yahoo1818.ngrok.app/view` loads at all - if it 404s/timeouts the tracker PC or
   tunnel is down, not this install).

## Rules
* Never edit `config.json` or put keys in committed files. The key only goes into `config.js` (gitignored).
* Do not install the extension on a computer without the user confirming that the people using it know they
  are being tracked (PDPA).
* To remove it: delete the extension in `chrome://extensions`.
* If the user ever says the ngrok domain or tracker setup changed, update the pinned address at the top of
  this file to match - don't silently keep using a stale one.
