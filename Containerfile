FROM node:20-bookworm

# Python 3.12 + git + common tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git curl wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Playwright + Chromium with all system deps
RUN mkdir -p /opt/playwright && cd /opt/playwright && npm init -y && npm install playwright@1.50.1 && npx playwright install --with-deps chromium

# Screenshot helper
COPY screenshot.js /opt/playwright/screenshot.js

WORKDIR /workspace
CMD ["sleep", "infinity"]
