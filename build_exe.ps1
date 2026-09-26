# Build the installable Credit Report Tool.
#
#   .\build_exe.ps1            -> dist\CreditReportTool\CreditReportTool.exe   (+ installer if Inno Setup is installed)
#
# Needs the project venv (run-local.ps1 creates it) with:  pip install pyinstaller
# The installer step needs Inno Setup 6 (https://jrsoftware.org/isinfo.php); it is skipped if missing.
# tools\bin\ngrok.exe must exist (download it from ngrok.com) - it is bundled for the ngrok link feature.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "No venv found. Run .\run-local.ps1 once first." }
if (-not (Test-Path "tools\bin\ngrok.exe")) { throw "tools\bin\ngrok.exe is missing (download from ngrok.com)." }

& $py -m PyInstaller --noconfirm --clean --onedir --noconsole --name CreditReportTool `
    --add-data "web;web" --add-data "tools\notify.ps1;tools" --add-data "tools\bin\ngrok.exe;tools\bin" `
    --collect-all playwright `
    --hidden-import gspread --hidden-import google.oauth2.service_account `
    --collect-submodules gspread --collect-submodules google.auth `
    --hidden-import cv2 --hidden-import PIL.ImageGrab --collect-submodules cv2 `
    --exclude-module matplotlib --exclude-module pandas `
    launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }
Write-Host "Built dist\CreditReportTool\CreditReportTool.exe" -ForegroundColor Green

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc installer.iss
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
    Write-Host "Built installer: dist\CreditReportTool-Setup.exe" -ForegroundColor Green
} else {
    Write-Host "Inno Setup not found - skipped the installer. Copy dist\CreditReportTool\ to the other computer, or install Inno Setup and re-run." -ForegroundColor Yellow
}
