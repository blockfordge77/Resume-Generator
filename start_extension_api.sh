#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
export EXTENSION_API_HOST="${EXTENSION_API_HOST:-127.0.0.1}"
export EXTENSION_API_PORT="${EXTENSION_API_PORT:-8010}"
export EXTENSION_API_BASE_URL="${EXTENSION_API_BASE_URL:-http://$EXTENSION_API_HOST:$EXTENSION_API_PORT}"
python scripts/sync_extension_config.py
python -m uvicorn extension_api.main:app --host "$EXTENSION_API_HOST" --port "$EXTENSION_API_PORT" --reload
