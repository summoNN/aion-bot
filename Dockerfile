FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FFMPEG_EXECUTABLE=ffmpeg \
    HEALTHCHECK_PATH=/tmp/aion2-event-announcer-ready

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

EXPOSE 8765

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/cache /app/config \
    && chown -R bot:bot /app

USER bot

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "from pathlib import Path; import sys, time; p=Path('/tmp/aion2-event-announcer-ready'); sys.exit(0 if p.exists() and time.time() - p.stat().st_mtime < 120 else 1)"

CMD ["python", "main.py"]
