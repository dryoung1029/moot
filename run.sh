#!/usr/bin/env bash
# Start the Moot hub. Creates a venv and installs deps on first run.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtualenv and installing dependencies..."
  python3 -m venv .venv
  ./.venv/bin/python -m pip install --quiet --upgrade pip
  ./.venv/bin/python -m pip install --quiet -r requirements.txt
fi

# Configuration (override via environment before running):
#   MOOT_HOST, MOOT_PORT, MOOT_DATA_DIR, MOOT_ADMIN_KEY, MOOT_JOIN_CODE,
#   MOOT_CHECKIN_HOURS, MOOT_MAX_FILE_BYTES
exec ./.venv/bin/python -m moot.server
