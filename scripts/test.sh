#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../backend"
PYTHONPATH=. python -m pytest tests -q "$@"
