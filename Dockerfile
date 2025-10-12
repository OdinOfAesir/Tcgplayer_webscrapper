# Official Playwright image: includes Chromium + all system deps
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Copy requirements first to leverage Docker layer cache
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the rest of your app
COPY . /app

# Render sets $PORT for you; uvicorn will listen on it
ENV PYTHONUNBUFFERED=1

# Start FastAPI
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "10000"]
