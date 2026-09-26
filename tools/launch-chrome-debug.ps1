$ErrorActionPreference = "SilentlyContinue"
Get-Process chrome | Stop-Process -Force
Start-Sleep -Seconds 3
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
$udd = "$env:LOCALAPPDATA\Google\Chrome\User Data"
Start-Process -FilePath $chrome -ArgumentList @(
    "--remote-debugging-port=9222",
    "--restore-last-session",
    "--profile-directory=Profile 1",
    "--user-data-dir=$udd"
)
Write-Output "chrome relaunched on Profile 1 with debug port 9222"
