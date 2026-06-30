# VoiceAgent server image. One command to a running AI sales agent: docker-compose up.
FROM python:3.11-slim

# Avoid interactive prompts, write .pyc off, unbuffered logs for real-time visibility.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
COPY voiceagent ./voiceagent
RUN pip install --upgrade pip && pip install .

# Bring in the example config and contacts so the container runs out of the box.
COPY examples ./examples

EXPOSE 8000

# The Media Streams WebSocket server. Point Twilio at wss://<public-host>/stream.
CMD ["uvicorn", "voiceagent.server:app", "--host", "0.0.0.0", "--port", "8000"]
