# Starts the ngrok tunnel (-> 127.0.0.1:8780, pinned to yahoo1818.ngrok.app) if it isn't already
# running. Safe to run repeatedly - idempotent. Used by the "WorkerTrackerNgrok" scheduled task
# (logon + every 5 min) via start-ngrok-hidden.vbs, so a crash or reboot self-heals.
$ErrorActionPreference = "SilentlyContinue"
$here = $PSScriptRoot

$alive = $false
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:4040/api/tunnels" -TimeoutSec 3
    $alive = ($r.tunnels | Where-Object { $_.public_url -eq "https://yahoo1818.ngrok.app" }) -ne $null
} catch {}
if ($alive) { exit }

if (-not (Test-Path "$here\data")) { New-Item -ItemType Directory "$here\data" | Out-Null }

$ngrok = (Get-Command ngrok -ErrorAction SilentlyContinue).Source
if (-not $ngrok) { $ngrok = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Ngrok.Ngrok_*\ngrok.exe" -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName }
if (-not $ngrok) { exit }   # not installed - nothing to launch

Start-Process -WindowStyle Hidden -FilePath $ngrok `
    -ArgumentList "http", "8780", "--domain=yahoo1818.ngrok.app", "--log=$here\data\ngrok.log" `
    -WorkingDirectory $here
