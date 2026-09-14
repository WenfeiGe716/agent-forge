#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ "${1:-}" == "--legacy" ]]; then
  shift
  exec .venv/bin/python scripts/start-local.py "$@"
fi
exec node scripts/start-rust.mjs "$@"
