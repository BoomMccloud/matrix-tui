FROM node:20-bookworm

# Python 3.12 + git + gh CLI + common tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip git curl wget ca-certificates \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Playwright + Chromium with all system deps
RUN mkdir -p /opt/playwright && cd /opt/playwright && npm init -y && npm install playwright@1.50.1 && npx playwright install --with-deps chromium

# Screenshot helper
COPY screenshot.js /opt/playwright/screenshot.js

# uv + ruff + pytest for Python project testing
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"
RUN uv tool install ruff && uv tool install pytest

# Gemini CLI coding agent
RUN npm install -g @google/gemini-cli

WORKDIR /workspace
CMD ["sleep", "infinity"]
