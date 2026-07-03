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

# Install the package properly (not just COPY) so the console scripts
# (moot-hub, moot-admin) land on PATH — `fly ssh console -C "moot-admin ..."`
# depends on that. Deps are already satisfied by requirements.txt above.
COPY pyproject.toml README.md ./
COPY moot ./moot
RUN pip install --no-deps .

# Persistent state (SQLite + file archive) lives on a mounted volume.
VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "-m", "moot.server"]
