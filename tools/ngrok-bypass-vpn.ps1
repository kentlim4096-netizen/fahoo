# Keep ngrok on the normal Wi-Fi connection while everything else (KR883 etc.) stays on the VPN.
#
#   .\tools\ngrok-bypass-vpn.ps1            add the routes (asks for Administrator via UAC)
#   .\tools\ngrok-bypass-vpn.ps1 -Remove    delete them again
#   .\tools\ngrok-bypass-vpn.ps1 -Install   also re-apply every 5 min (ngrok's server IPs rotate)
#
# The VPN (Pritunl/OpenVPN) captures ALL traffic with 0.0.0.0/1 + 128.0.0.0/1. This adds a /32 host
# route for each ngrok server address via the Wi-Fi gateway, which is more specific than the VPN's
# routes, so only ngrok's own connection leaves through Wi-Fi. Nothing else changes.
param([switch]$Remove, [switch]$Install, [switch]$Elevated)

$ErrorActionPreference = "Stop"
$self = $MyInvocation.MyCommand.Path
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    $argList = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$self`"", "-Elevated")
    if ($Remove) { $argList += "-Remove" }
    if ($Install) { $argList += "-Install" }
    Start-Process powershell -Verb RunAs -ArgumentList $argList -Wait
    exit
}

$hosts = "connect.ngrok-agent.com", "tunnel.ngrok.com", "ap.tunnel.ngrok.com", "tunnel.ap.ngrok.com",
         "ngrok-agent.com", "update.ngrok-agent.com", "dashboard.ngrok.com", "api.ngrok.com"

# The physical (non-VPN) default gateway.
$def = Get-NetRoute -DestinationPrefix 0.0.0.0/0 |
       Where-Object { $_.InterfaceAlias -notmatch "Pritunl|TAP|VPN|TUN|WireGuard" -and $_.NextHop -ne "0.0.0.0" } |
       Sort-Object { $_.RouteMetric + $_.InterfaceMetric } | Select-Object -First 1
if (-not $def) { throw "No non-VPN default gateway found (is Wi-Fi connected?)." }
$gw, $ifx = $def.NextHop, $def.InterfaceIndex
Write-Host "Wi-Fi gateway $gw on interface $($def.InterfaceAlias)"

$ips = New-Object System.Collections.Generic.HashSet[string]
foreach ($h in $hosts) {
    try { [System.Net.Dns]::GetHostAddresses($h) | Where-Object { $_.AddressFamily -eq "InterNetwork" } |
          ForEach-Object { [void]$ips.Add($_.IPAddressToString) } } catch { }
}
# Also whatever the running ngrok agent is connected to right now.
$ng = Get-Process ngrok -ErrorAction SilentlyContinue
foreach ($p in $ng) {
    Get-NetTCPConnection -OwningProcess $p.Id -State Established -ErrorAction SilentlyContinue |
        Where-Object { $_.RemotePort -eq 443 -and $_.RemoteAddress -match '^\d+\.\d+\.\d+\.\d+$' } |
        ForEach-Object { [void]$ips.Add($_.RemoteAddress) }
}

$stateFile = Join-Path (Split-Path $self) "ngrok-bypass-routes.txt"
if ($Remove) {
    if (Test-Path $stateFile) { Get-Content $stateFile | ForEach-Object { cmd /c "route delete $_ >nul 2>&1" } ; Remove-Item $stateFile }
    Unregister-ScheduledTask -TaskName "NgrokBypassVpn" -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed ngrok bypass routes." ; exit
}

$known = @(); if (Test-Path $stateFile) { $known = Get-Content $stateFile }
foreach ($ip in $ips) {
    cmd /c "route delete $ip >nul 2>&1"   # ok if it does not exist yet
    cmd /c "route add $ip mask 255.255.255.255 $gw metric 1 if $ifx >nul 2>&1"
    if ($LASTEXITCODE -ne 0) { Write-Host "  $ip FAILED to add route" -ForegroundColor Red; continue }
    if ($known -notcontains $ip) { $known += $ip }
    Write-Host "  $ip -> Wi-Fi"
}
$known | Sort-Object -Unique | Set-Content $stateFile

if ($Install) {
    $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$self`" -Elevated"
    $trg = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName "NgrokBypassVpn" -Action $act -Trigger $trg -RunLevel Highest -Force | Out-Null
    Write-Host "Scheduled task NgrokBypassVpn installed (re-applies every 5 minutes)."
}
Write-Host "Done. Restart ngrok so it reconnects over Wi-Fi."
