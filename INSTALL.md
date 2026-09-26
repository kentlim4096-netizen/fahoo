# Installing on another computer

1. Build (on a computer with the project set up): `.\build_exe.ps1` -> `dist\CreditReportTool-Setup.exe`
   (needs `pip install -r requirements-build.txt` and Inno Setup 6; `tools\bin\ngrok.exe` is bundled).
2. Copy `CreditReportTool-Setup.exe` to the other computer and run it (per-user install, no admin needed).
   Google Chrome must be installed there.
3. The setup window asks for the panel logins (worker/admin), optional ngrok token + domain, and optional
   KR883 details. Settings are saved in `%LOCALAPPDATA%\CreditReportTool\config.env`.
4. Start Menu -> **Credit Report Tool - Chrome for KR883 login**: opens Chrome with the debug port.
   Log in to KR883 there yourself (password + OTP) and keep that tab open. The tool mirrors that session.
5. Start Menu -> **Credit Report Tool**: starts the service and opens the panel.

Notes
- KR883 allows ONE active session per account: a second computer needs its own KR883 account, or use one at a time.
- A reserved ngrok domain can be online on one computer at a time.
- The tool never logs in to KR883 or handles OTPs for you (`KW388_ALLOW_AUTO_LOGIN=false`).
- Uninstalling keeps your settings and data in `%LOCALAPPDATA%\CreditReportTool` (delete that folder to remove them).

## Using a different KR883 ID on each computer (ID-1, ID-2, ID-3 ...)
- Each computer logs in to KR883 with its own ID in its own Chrome; the tool mirrors that session, so the IDs never log each other out.
- Every KR883 ID has its own permissions. The workflow needs **view_member_list** and **view_credit_report**.
  Setup step 4 ("Check connection") shows the ID that is logged in and warns if a permission is missing.
  The running service also logs it and shows it at /scrape/ui-status.
- The workflow only finds customers that appear in **that ID's own Member List**. A customer outside that ID's list is reported
  as "not a member" even though KR883 knows them - this is how the KR883 Member List works for downline accounts.
- If an ID cannot open Member List, or its Actions menu has no "Credit report", the result names that reason instead of timing out.
- Credit reports are only ever read through the page itself (Member List -> Actions -> Credit report); the tool makes no direct call to the
  credit-report API.
