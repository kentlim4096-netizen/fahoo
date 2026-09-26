' Auto-starts the credit report tool service + ngrok tunnel at login, with no visible window.
' To disable autostart: delete this file from the Startup folder.
Set sh = CreateObject("WScript.Shell")
' Wait for network to come up before the tunnel/login.
WScript.Sleep 15000
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\Users\User\Desktop\Scraper\kwscraper\tools\autostart.ps1""", 0, False
