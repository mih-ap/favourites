FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FAV_DATA_DIR=/data \
    FAV_VAULT=/vault \
    HOME=/tmp

# ffmpeg нужен yt-dlp для склейки и для звука под расшифровку (faster-whisper).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web

EXPOSE 8090
CMD ["python", "-m", "app.web"]
