# Launch Chrome so the scraper can share your logged-in session.
#
# KR883 allows one session per account, so the scraper logging in separately kicks you out
# (and you log in, it kicks the scraper out). Attaching to your browser sidesteps that: your
# login IS the scraper's login, and no TOTP is needed.
#
# Chrome only opens the debugging port at startup, so it must be fully closed first.
$ErrorActionPreference = "Stop"
$port = 9222

$chrome = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { throw "Chrome not found." }

if (Get-Process chrome -ErrorAction SilentlyContinue) {
    Write-Host "Chrome is running and must be restarted to open the debugging port." -ForegroundColor Yellow
    Write-Host "Save your work, then press Enter to close and reopen it (tabs are restored)."
    Read-Host
    Get-Process chrome | Stop-Process -Force
    Start-Sleep -Seconds 3
}

# Your normal profile - so you stay signed in to KR883 and everything else.
$profileDir = "$env:LOCALAPPDATA\Google\Chrome\User Data"
Start-Process $chrome -ArgumentList "--remote-debugging-port=$port", "--restore-last-session", "--user-data-dir=`"$profileDir`""

for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 700
    try {
        $v = Invoke-RestMethod "http://127.0.0.1:$port/json/version" -TimeoutSec 2
        Write-Host "`nChrome is sharing its session on port $port" -ForegroundColor Green
        Write-Host "  $($v.Browser)"
        Write-Host "`nNow log in to KR883 in Chrome, then run the scrape."
        exit 0
    } catch { }
}
throw "Chrome did not open the debugging port. Make sure every Chrome window was closed."
