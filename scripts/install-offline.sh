#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"
"$PYTHON" - <<'PY'
import platform,sys
if sys.version_info[:2]!=(3,13) or sys.platform!='linux' or platform.machine() not in ('x86_64','AMD64'):
    raise SystemExit('此离线wheel包用于 Linux x86_64 / CPython 3.13。其他环境请使用受信任包源安装backend/requirements-core.txt；不要强行安装不兼容wheel。')
PY
if [[ ! -x .venv/bin/python ]]; then "$PYTHON" -m venv .venv; fi
.venv/bin/python -m pip install --no-index --require-hashes --find-links=offline/wheelhouse -r offline/requirements-linux-cp313.lock
.venv/bin/python -m pip check
if command -v node >/dev/null 2>&1; then node scripts/build-offline.mjs; else echo '未发现Node；使用包内已经生成的frontend/dist。修改前端后需要Node重新构建。'; fi
.venv/bin/python - <<'PY'
from pathlib import Path
assert Path('frontend/dist/index.html').is_file(), 'Missing built UI'
import fastapi,sqlalchemy,httpx,cryptography
print('离线依赖安装完成。运行 bash start.sh 启动实际平台，登录后配置供应商。')
PY
