# Playwright Python base image with browsers pre-installed
# See https://playwright.dev/python/docs/docker
FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

# Install app dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Browsers (Chromium, etc.) are included in the base image

# Copy application code
COPY spike.py state_store.py emailer.py ./
COPY storage/ storage/
COPY targets.json ./

# Run the change detection job
CMD ["python", "spike.py"]
