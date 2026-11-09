# Starts the worker tracker server (server.py on 127.0.0.1:8780) if it isn't already running.
# Safe to run repeatedly - idempotent. Used by the "WorkerTrackerServer" scheduled task
# (logon + network-connect + every 5 min) via start-tracker-hidden.vbs, so a crash or reboot self-heals.
#
# Uses a raw TCP probe instead of Test-NetConnection, which does extra route/diagnostic work and can
# take 1-5+ seconds per call - most of the "slow to come up after a restart" feeling was this cmdlet,
# not actually waiting on the server or the network.
$ErrorActionPreference = "SilentlyContinue"
$here = $PSScriptRoot
Set-Location $here

function Test-PortOpen($port, $timeoutMs = 250) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $task = $client.ConnectAsync("127.0.0.1", $port)
        $ok = $task.Wait($timeoutMs) -and $client.Connected
        $client.Close()
        return $ok
    } catch { return $false }
}

if (Test-PortOpen 8780) { exit }

if (-not (Test-Path "$here\data")) { New-Item -ItemType Directory "$here\data" | Out-Null }

# Prefer fahoo's own venv if it exists; otherwise fall back to the system Python on PATH.
$venvPy = Join-Path (Split-Path $here -Parent) ".venv\Scripts\python.exe"
$py = if (Test-Path $venvPy) { $venvPy } else { "python" }

Start-Process -WindowStyle Hidden -FilePath $py -ArgumentList "server.py" -WorkingDirectory $here `
    -RedirectStandardOutput "$here\data\server.log" -RedirectStandardError "$here\data\server.err.log"
