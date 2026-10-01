# AI Test Generator studio: everything it needs at run time is inside the image, so it works
# without internet access (TESTGEN_OFFLINE=on): browsers, axe-core, the MCP servers of the presets.
#
#   docker build -t ai-testgen .
#   docker compose up          # the studio + a local model (vLLM), see docker-compose.yml
FROM mcr.microsoft.com/playwright/python:v1.55.0-noble

ARG AXE_VERSION=4.13.0
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 PORT=8765 \
    TESTGEN_DATA_DIR=/data TESTGEN_SECRETS_DIR=/secrets \
    TESTGEN_AXE_JS=/opt/testgen/axe.min.js

RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium firefox webkit

# MCP servers of the presets: installed and cached, so `npx --offline` finds them without a network.
# The studio takes their launch commands from TESTGEN_*_MCP (mcp_hub.py).
RUN npm install -g @playwright/mcp mcp-zephyr-scale \
    && npm cache add @playwright/mcp mcp-zephyr-scale
ENV TESTGEN_PLAYWRIGHT_MCP="npx --offline @playwright/mcp" \
    TESTGEN_ZEPHYR_MCP="npx --offline mcp-zephyr-scale"
# axe-core for accessibility checks (the studio downloads it from a CDN when it may).
RUN mkdir -p /opt/testgen \
    && curl -fsSL "https://cdn.jsdelivr.net/npm/axe-core@${AXE_VERSION}/axe.min.js" -o /opt/testgen/axe.min.js

COPY . .
RUN mkdir -p /data /secrets
VOLUME ["/data", "/secrets"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fs http://127.0.0.1:8765/api/health || exit 1
CMD ["python", "server.py"]
