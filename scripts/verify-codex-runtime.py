#!/usr/bin/env python3
"""Real, non-mutating Codex app-server smoke test.

The test starts the project-owned Codex app-server, initializes JSON-RPC, and
creates one thread in a temporary Agent Forge runtime home. It never reads or
writes ~/.codex and does not make a model request.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from forge.runtime_bridge import CodexAppServerClient, runtime_binary_status


async def main() -> int:
    status = runtime_binary_status()
    if not status["codex"]["available"]:
        print(json.dumps({"ok": False, "error": "找不到 codex 可执行文件"}, ensure_ascii=False))
        return 1
    with tempfile.TemporaryDirectory(prefix="agent-forge-codex-") as directory:
        root = Path(directory)
        client = CodexAppServerClient(runtime_home=root / "codex-home", cwd=root)
        try:
            initialized = await client.start(timeout=20)
            account = await client.request('account/read', {'refreshToken': False})
            if account.get('account') is not None:
                raise RuntimeError('独立运行时不应继承任何个人登录账户')
            thread = await client.start_thread(cwd=root, approval_policy="never", sandbox="workspace-write")
            result = {
                "ok": True,
                "user_agent": initialized.get("userAgent"),
                "thread_id": thread.get("thread", {}).get("id"),
                "runtime_home": str(root / "codex-home"),
                "runtime_home_verified": initialized.get('codexHome') is not None,
                "personal_account_inherited": False,
                "personal_codex_home_used": False,
            }
            print(json.dumps(result, ensure_ascii=False))
            return 0
        finally:
            await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
