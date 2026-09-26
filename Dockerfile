# credit report tool — standalone copy. Playwright + Chromium preinstalled, TOTP-automated login.
# Talks to the backend API over the network (API_INTERNAL_URL in .env) — it doesn't need
# anything else running anywhere near it, just network access to that URL.
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY scraper_service.py .
COPY web/ ./web/

CMD ["python", "scraper_service.py"]
