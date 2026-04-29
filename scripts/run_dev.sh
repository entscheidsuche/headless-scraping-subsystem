#!/usr/bin/env bash
# Convenience launcher for local dev.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements-dev.txt
  .venv/bin/playwright install chromium
fi

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

exec .venv/bin/uvicorn app.main:app \
  --host "${BIND_HOST:-127.0.0.1}" --port "${BIND_PORT:-8088}" --reload
