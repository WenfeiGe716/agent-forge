#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] || { echo 'Run bash scripts/init.sh first.'; exit 1; }
command -v docker >/dev/null || { echo 'Docker Engine is required.'; exit 1; }
docker compose version >/dev/null
docker compose config --quiet
[[ "$(docker info --format '{{.OSType}}')" == linux ]] || { echo 'This deployment requires a Linux Docker host.'; exit 1; }
set -a; source .env; set +a
[[ "$FORGE_DATA_DIR" = /* ]] || { echo 'FORGE_DATA_DIR must be absolute.'; exit 1; }
[[ "$FORGE_IMPORT_DIR" = /* ]] || { echo 'FORGE_IMPORT_DIR must be absolute.'; exit 1; }
docker info --format '{{json .Runtimes}}' | python3 -c 'import json,os,sys; r=json.load(sys.stdin); name=os.environ["SANDBOX_RUNTIME"]; assert name in r, f"Docker runtime {name} is missing; no insecure fallback is performed"'
if [[ "$SANDBOX_RUNTIME" != runsc ]]; then echo 'WARNING: You selected a non-gVisor runtime. Security boundary is weaker; explicit operator decision.'; fi
echo 'Compose syntax, Docker daemon, absolute paths and configured runtime checked.'
echo 'Image/toolchain and real model compatibility still require smoke testing.'
