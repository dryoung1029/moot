# The Moot hub — a small always-on ASGI server (MCP + dashboard).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MOOT_HOST=0.0.0.0 \
    MOOT_PORT=8080 \
    MOOT_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# Only the package is needed at runtime (schema.sql ships inside it).
COPY moot ./moot

# Persistent state (SQLite + file archive) lives on a mounted volume.
VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "-m", "moot.server"]
