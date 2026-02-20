FROM node:20-bookworm

# Python 3.12 + git + common tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git curl wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Playwright + Chromium with all system deps
RUN npx -y playwright@1.50.1 install --with-deps chromium

# Screenshot helper
COPY screenshot.js /usr/local/bin/screenshot.js
RUN chmod +x /usr/local/bin/screenshot.js

WORKDIR /workspace
CMD ["sleep", "infinity"]
