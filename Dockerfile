# VoiceAgent: one image for the web app (API + dashboard + Twilio media streams) and the worker.

# --- 1. Build the dashboard -------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- 2. Python runtime ------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8080 \
    VOICEAGENT_WEB_DIST=/app/web/dist

WORKDIR /app

# Dependencies first for layer caching.
COPY pyproject.toml README.md ./
COPY voiceagent ./voiceagent
RUN pip install --upgrade pip && pip install .

COPY examples ./examples
COPY docker/entrypoint.sh /usr/local/bin/voiceagent-entrypoint
COPY --from=web /web/dist ./web/dist

# Run as an unprivileged user; call logs live on a volume it owns.
RUN useradd --create-home --uid 10001 voiceagent \
    && mkdir -p /app/call_logs \
    && chown -R voiceagent:voiceagent /app/call_logs \
    && chmod +x /usr/local/bin/voiceagent-entrypoint
USER voiceagent

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/health', timeout=4)" || exit 1

ENTRYPOINT ["voiceagent-entrypoint"]
CMD ["app"]
