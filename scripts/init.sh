#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then echo '.env already exists; refusing to overwrite secrets.'; exit 1; fi
umask 077
python3 - <<'PY'
import os,secrets,base64
from pathlib import Path
root=Path.cwd().resolve()
values={
 'HOST_UID':str(os.getuid()),'HOST_GID':str(os.getgid()),
 'FORGE_DATA_DIR':str(root/'data'),'FORGE_IMPORT_DIR':str(root/'imports'),
 'POSTGRES_ADMIN_PASSWORD':secrets.token_urlsafe(30),'FORGE_DB_PASSWORD':secrets.token_urlsafe(30),
 'TEMPORAL_DB_PASSWORD':secrets.token_urlsafe(30),'ADMIN_PASSWORD':secrets.token_urlsafe(20),
 'ENCRYPTION_KEY':base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
 'SANDBOX_TOKEN':secrets.token_urlsafe(40),'SANDBOX_RUNTIME':'runsc',
 'SANDBOX_IMAGES':'agent-forge/sandbox:local','MAX_ACTIVE_ACTIVITIES':'4','MAX_SANDBOXES':'4',
 'MAX_PROJECT_MB':'200','BIND_ADDRESS':'127.0.0.1','WEB_PORT':'8080',
 'ALLOWED_ORIGINS':'http://localhost:8080,http://127.0.0.1:8080','SECURE_COOKIE':'false'
}
for directory in ('data','imports','backups'):(root/directory).mkdir(exist_ok=True,mode=0o700)
(root/'.env').write_text('\n'.join(f'{k}="{v}"' for k,v in values.items())+'\n')
os.chmod(root/'.env',0o600)
print('Created .env (0600). Username: admin. Read ADMIN_PASSWORD locally; no password is printed here.')
print('Back up ENCRYPTION_KEY separately. Do not commit .env.')
PY
if [[ ! -d imports/tiny-project ]]; then cp -R examples/tiny-project imports/tiny-project; fi
