#!/usr/bin/env python3
"""Run one explicitly configured real Codex task.

Required environment variables are deliberately explicit so this command
cannot silently select a provider or send project data. The default prompt
asks for pwd only and uses a disposable working directory.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from forge.codex_task import CodexProvider, CodexSingleTask, CodexTaskError


async def main() -> int:
    base_url = os.environ.get("FORGE_POC_BASE_URL", "").strip()
    model = os.environ.get("FORGE_POC_MODEL", "").strip()
    api_key = os.environ.get("FORGE_POC_API_KEY", "")
    if not base_url or not model or not api_key:
        print(json.dumps({
            "ok": False,
            "error": "请显式设置 FORGE_POC_BASE_URL、FORGE_POC_MODEL、FORGE_POC_API_KEY",
        }, ensure_ascii=False))
        return 2
    prompt = os.environ.get(
        "FORGE_POC_PROMPT",
        "在当前工作目录执行 pwd，然后只回复 POC_OK。不要修改文件。",
    )
    requested_cwd = os.environ.get("FORGE_POC_CWD", "").strip()
    with tempfile.TemporaryDirectory(prefix="agent-forge-codex-task-") as directory:
        cwd = Path(requested_cwd).resolve() if requested_cwd else Path(directory) / "workspace"
        runtime_home = Path(directory) / "runtime-home"
        task = CodexSingleTask(
            provider=CodexProvider(base_url=base_url, model=model, api_key=api_key),
            runtime_home=runtime_home,
            cwd=cwd,
        )
        try:
            result = await task.run(prompt)
        except CodexTaskError as exc:
            print(json.dumps({"ok": False, "error": str(exc)[:1200]}, ensure_ascii=False))
            return 1
        print(json.dumps({
            "ok": result.status == "completed",
            "status": result.status,
            "thread_id": result.thread_id,
            "turn_id": result.turn_id,
            "event_count": len(result.events),
            "event_kinds": [event["kind"] for event in result.events],
            "text": result.text[:4000],
            "errors": result.errors,
            "summary": ("任务完成" if result.status == "completed" else "任务未完成，请根据 errors 检查供应商、模型和权限"),
        }, ensure_ascii=False))
        return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
