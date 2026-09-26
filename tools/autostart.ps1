# Auto-start the credit report tool + ngrok tunnel at login. Hidden, with guards so nothing
# double-starts. Launched by the Startup-folder VBS.
$ErrorActionPreference = "SilentlyContinue"
$app = "C:\Users\User\Desktop\Scraper\kwscraper"
Set-Location $app

# 1. Scraper service (self-exits if port 8765 is already served, so a double-launch is harmless)
$py = "$app\.venv\Scripts\python.exe"
$up = $false
try { $up = (Test-NetConnection -ComputerName 127.0.0.1 -Port 8765 -InformationLevel Quiet -WarningAction SilentlyContinue) } catch {}
if (-not $up) {
    Start-Process -WindowStyle Hidden -FilePath $py `
        -ArgumentList "scraper_service.py" -WorkingDirectory $app `
        -RedirectStandardOutput "$app\data\service.log" -RedirectStandardError "$app\data\service.err.log"
    Start-Sleep -Seconds 4
}

# 2. ngrok tunnel (only if not already running). Uses a pinned domain if NGROK_DOMAIN is set in
#    .env, otherwise a random free URL.
if (-not (Get-Process ngrok -ErrorAction SilentlyContinue)) {
    $domain = ""
    if (Test-Path "$app\.env") {
        $line = Select-String -Path "$app\.env" -Pattern '^NGROK_DOMAIN=(.+)$' | Select-Object -First 1
        if ($line) { $domain = $line.Matches.Groups[1].Value.Trim() }
    }
    $args = @("http", "8765", "--log", "$app\data\ngrok.log")
    if ($domain) { $args += "--url=$domain" }
    Start-Process -WindowStyle Hidden -FilePath "$app\tools\bin\ngrok.exe" -ArgumentList $args -WorkingDirectory $app
}
