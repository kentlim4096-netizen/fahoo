# Instructions for Claude: installing the KR883 Activity Logger on a computer

Use this when the user says things like "install the worker tracker / KR883 activity logger on this computer".
This computer is a **worker PC** unless the user says it is the tracker PC.

## Ask the user for (do not guess, do not invent)
1. **Server address** - the fahoo ngrok link plus `/tracker`, e.g. `https://YOUR-NGROK-DOMAIN.ngrok.app/tracker`.
2. **Key** - the shared key from the tracker PC (`worker-tracker/config.json`, field `key`). The user gives it
   to you; never print it back more than needed and never commit it.
3. **Computer name** - a short label for this PC (e.g. `WORKER-PC-2`), shown on the dashboard.

## Steps
1. Make sure Python 3 and git exist (`python --version`, `git --version`); if not, tell the user and stop.
2. Get the code: `git clone -b worker-tracker https://github.com/kentlim4096-netizen/fahoo.git` (use the repo URL / branch
   the user gives you if different; if the repo is private the user must be signed in to git). If it is already
   cloned, run `git pull`. You only need the `worker-tracker` folder inside it.
3. From `worker-tracker`, run:
   `python install_extension.py --server <server> --key <key> --computer <name>`
   (Tracker PC itself: `--server local`, no key needed.)
   This writes `extension/kr883-activity-logger/config.js`. **Never commit `config.js`** (it is gitignored).
4. The next part cannot be automated - ask the user to do it in the Chrome profile they use for KR883:
   open `chrome://extensions`, switch on **Developer mode**, **Load unpacked**, choose the path the script
   printed, then reload the KR883 tab. (Chrome may show a "developer mode extensions" notice at startup; that
   is expected.)
5. Verify: ask the user to open the KR883 website and search any customer, then check that the dashboard
   (`<server>/dashboard`, admin login) lists this computer name. If not: check `chrome://extensions` -> the
   extension's "service worker" / Errors; check the server address and key; check the tracker PC is on.

## Rules
* Never edit `config.json` or put keys in committed files. The key only goes into `config.js` (gitignored).
* Do not install the extension on a computer without the user confirming that the people using it know they
  are being tracked.
* To remove it: delete the extension in `chrome://extensions`.
