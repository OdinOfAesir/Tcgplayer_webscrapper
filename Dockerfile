# Dockerfile
# Match Playwright 1.55.0 as requested by the runtime
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

WORKDIR /app

# Install Python deps (do NOT force-install 'playwright' here)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the app
COPY . /app

# Use $PORT provided by Render (JSON CMD won’t expand $PORT, so use a shell)
CMD ["/bin/sh", "-c", "uvicorn app:app --host 0.0.0.0 --port $PORT"]
