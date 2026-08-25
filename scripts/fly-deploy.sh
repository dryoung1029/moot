#!/usr/bin/env bash
# Prime's one-shot Fly deploy for the moot hub.
# Clears a stuck machine lease, deploys from the repo root, then waits until
# /healthz reports this checkout's __version__.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP="${FLY_APP:-moot}"
HOST="${MOOT_HEALTH_HOST:-https://moot.fly.dev}"

if [[ ! -f fly.toml || ! -f Dockerfile ]]; then
  echo "error: run from the moot repo (need fly.toml + Dockerfile)" >&2
  exit 1
fi

EXPECT="$(python3 -c "from moot import __version__; print(__version__)")"
echo "Deploying $APP → expect healthz version $EXPECT"

MACHINE="$(fly machines list -a "$APP" --json 2>/dev/null | python3 -c "
import json,sys
ms=json.load(sys.stdin)
print(ms[0]['id'] if ms else '')
")"
if [[ -n "$MACHINE" ]]; then
  echo "Clearing lease on $MACHINE (ok if none)…"
  fly machine leases clear "$MACHINE" -a "$APP" 2>/dev/null || true
fi

fly deploy -a "$APP"

echo "Waiting for $HOST/healthz version=$EXPECT …"
for i in $(seq 1 40); do
  body="$(curl -fsS -m 10 "$HOST/healthz" 2>/dev/null || true)"
  if printf '%s' "$body" | grep -q "\"version\":\"$EXPECT\""; then
    echo "OK $body"
    exit 0
  fi
  printf '  [%02d] %s\n' "$i" "${body:-"(no response)"}"
  sleep 5
done

echo "error: healthz never reached version $EXPECT" >&2
echo "hint: fly logs -a $APP -n --no-tail | tail -80" >&2
exit 1
