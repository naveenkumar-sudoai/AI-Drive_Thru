#!/usr/bin/env bash
set -euo pipefail

# AI Drive-Thru launcher — starts the FastAPI backend, which also serves the
# dashboard static files. Runs on port 8000, bound to all interfaces so any
# device on the same WiFi can open http://<pi-ip>:8000.

cd "$(dirname "$0")"

PORT="${PORT:-8000}"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment (.venv)..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# Install only if the backend deps aren't already importable. This keeps
# repeated / boot-time starts fast and offline-safe (important when the Pi is
# sealed in an enclosure with no network).
if ! python -c "import fastapi, uvicorn, websockets" 2>/dev/null; then
  echo "Installing backend dependencies..."
  pip install -q --upgrade pip
  pip install -q -r backend/requirements.txt
fi

mkdir -p data/photos

RELOAD_FLAG=""
if [ "${RELOAD:-0}" = "1" ]; then
  RELOAD_FLAG="--reload"
fi

echo "Starting AI Drive-Thru backend on http://0.0.0.0:${PORT} ..."
exec uvicorn backend.app:app --host 0.0.0.0 --port "${PORT}" ${RELOAD_FLAG}
