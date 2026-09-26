# Start the credit report tool natively on Windows (no Docker), in local-only mode.
#
#   .\run-local.ps1
#
# First run: copy .env.example to .env and fill in the KW388_* credentials. Leave the
# BACKEND_ADMIN_* vars blank to keep LOCAL_ONLY on -- results land in .\data\output as JSON
# instead of being posted anywhere.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    Write-Host "No venv found - creating one and installing dependencies..." -ForegroundColor Yellow
    python -m venv .venv
    & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    & ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
    & ".\.venv\Scripts\python.exe" -m playwright install chromium
}

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example - fill in your KW388 credentials." -ForegroundColor Yellow
}

Write-Host "Starting scraper on http://127.0.0.1:8765 (Ctrl+C to stop)" -ForegroundColor Green
& ".\.venv\Scripts\python.exe" scraper_service.py
