#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export FORGE_E2E_STATE="${FORGE_E2E_STATE:-/tmp/agent-forge-e2e}"
echo '仅验证环境：双协议脚本化模型，不调用真实LLM；执行器仅运行固定可信测试文件。'
exec .venv/bin/python tests/e2e/start_fixture.py --seed "$@"
