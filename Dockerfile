FROM python:3.11-slim

# System dependencies for Playwright/Chromium + cron
RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates cron \
    fonts-liberation libatk-bridge2.0-0 libatk1.0-0 \
    libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 \
    libnspr4 libnss3 libx11-xcb1 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libxss1 libxtst6 xdg-utils \
    libasound2t64 \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium for Playwright
RUN playwright install chromium && \
    playwright install-deps chromium

# Copy source code
COPY *.py ./

# Output folder (bind-mounted from host)
RUN mkdir -p output

# Copy entrypoint script
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Default: run once and exit
CMD ["python", "main.py"]
