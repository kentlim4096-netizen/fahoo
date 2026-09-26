@echo off
REM One-click: excludes the ngrok folder from Windows Defender so it stops deleting ngrok.exe.
REM Self-elevates (you'll get a UAC "Yes/No" prompt - click Yes).
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator permission...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
echo Adding Windows Defender exclusions for ngrok...
powershell -NoProfile -Command "Add-MpPreference -ExclusionPath 'C:\Users\User\Desktop\Scraper\kwscraper\tools\bin'; Add-MpPreference -ExclusionProcess 'ngrok.exe'"
echo.
echo Verifying...
powershell -NoProfile -Command "$e=(Get-MpPreference).ExclusionPath; if ($e -contains 'C:\Users\User\Desktop\Scraper\kwscraper\tools\bin') { Write-Host '  SUCCESS - ngrok folder is now excluded. Defender will no longer remove it.' -ForegroundColor Green } else { Write-Host '  Something went wrong - exclusion not found.' -ForegroundColor Red }"
echo.
echo You can close this window.
pause
