#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c 'import fastapi, sqlalchemy, httpx, cryptography' >/dev/null 2>&1; then
  bash scripts/install-offline.sh
fi
exec .venv/bin/python scripts/start-local.py "$@"
