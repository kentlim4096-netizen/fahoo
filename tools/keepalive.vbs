' Runs autostart.ps1 hidden. Scheduled every 5 min as a keep-alive; autostart.ps1 only launches
' the service/ngrok if they're not already running, so this is a no-op when all is well.
CreateObject("WScript.Shell").Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\Users\User\Desktop\Scraper\kwscraper\tools\autostart.ps1""", 0, False
