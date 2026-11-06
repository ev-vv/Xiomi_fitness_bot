FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-core tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY extractor ./extractor
COPY analytics ./analytics
COPY ai ./ai
COPY bot ./bot
COPY charts ./charts
COPY core ./core
COPY storage ./storage
COPY worker ./worker
RUN pip install .

# Runs as the host user (see docker-compose.yml) so files in ./data keep their owner and modes.
CMD ["python", "-m", "bot"]
