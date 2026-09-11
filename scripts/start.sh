#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if command -v node >/dev/null 2>&1; then node scripts/build-offline.mjs; fi
test -f frontend/dist/index.html || { echo '缺少已构建前端'; exit 1; }
bash scripts/preflight.sh
docker build -f infra/Dockerfile.sandbox -t agent-forge/sandbox:local .
docker compose up -d --build
echo 'Services requested. Check docker compose ps and docker compose logs -f api worker.'
echo 'Default UI: http://localhost:8080 . Username: admin; password is in .env.'
