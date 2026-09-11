#!/usr/bin/env bash
# Conservative offline backup, not an online or cross-machine HA solution.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
umask 077
for service in api worker sandbox-broker temporal; do
 if [[ -n "$(docker compose ps --status running -q "$service")" ]]; then
   echo "Stop and quiesce $service before offline backup. No service is stopped automatically."; exit 1
 fi
done
if [[ -n "$(docker ps -q --filter 'name=^/forge-[0-9a-f]{24}$')" ]]; then echo 'A sandbox container is still running. Resolve it before backup.';exit 1;fi
out="${1:-./backups}/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$out"
docker compose exec -T database pg_dumpall -U postgres > "$out/databases.sql"
tar -C "$FORGE_DATA_DIR" -czf "$out/data.tar.gz" .
cp docker-compose.yml "$out/compose.yml"
sha256sum "$out/databases.sql" "$out/data.tar.gz" > "$out/SHA256SUMS"
echo "Backup created: $out"
echo 'Keep .env and ENCRYPTION_KEY in a separate protected backup. Test restoration before relying on backups.'
