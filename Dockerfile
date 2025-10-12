# Dockerfile — keeps Playwright + browsers preinstalled and version-matched
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

WORKDIR /app

# Silence root pip warning (harmless in containers)
ENV PIP_ROOT_USER_ACTION=ignore

# Install Python deps
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy your app after deps (better cache)
COPY . /app

# Make logs unbuffered
ENV PYTHONUNBUFFERED=1

# Use Render's $PORT at runtime (shell to expand env)
CMD ["/bin/sh", "-c", "uvicorn app:app --host 0.0.0.0 --port $PORT"]
