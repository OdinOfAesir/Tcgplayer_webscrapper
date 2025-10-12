# Dockerfile — Python slim + Playwright (Chromium)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_ROOT_USER_ACTION=ignore

# Base OS deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates wget gnupg curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps first (cache-friendly)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /app/requirements.txt

# Install Chromium + required system libs via Playwright
RUN python -m playwright install --with-deps chromium

# App code
COPY . /app

# Default command (Render sets $PORT)
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
