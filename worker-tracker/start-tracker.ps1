# Starts the worker tracker server (server.py on 127.0.0.1:8780) if it isn't already running.
# Safe to run repeatedly - idempotent. Used by the "WorkerTrackerServer" scheduled task
# (logon + every 5 min) via start-tracker-hidden.vbs, so a crash or reboot self-heals.
$ErrorActionPreference = "SilentlyContinue"
$here = $PSScriptRoot
Set-Location $here

$up = $false
try { $up = Test-NetConnection -ComputerName 127.0.0.1 -Port 8780 -InformationLevel Quiet -WarningAction SilentlyContinue } catch {}
if ($up) { exit }

if (-not (Test-Path "$here\data")) { New-Item -ItemType Directory "$here\data" | Out-Null }

# Prefer fahoo's own venv if it exists; otherwise fall back to the system Python on PATH.
$venvPy = Join-Path (Split-Path $here -Parent) ".venv\Scripts\python.exe"
$py = if (Test-Path $venvPy) { $venvPy } else { "python" }

Start-Process -WindowStyle Hidden -FilePath $py -ArgumentList "server.py" -WorkingDirectory $here `
    -RedirectStandardOutput "$here\data\server.log" -RedirectStandardError "$here\data\server.err.log"
