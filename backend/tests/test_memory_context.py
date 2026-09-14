from types import SimpleNamespace
import json

import pytest

from forge.memory_context import (
    build_memory_context,
    compress_memory_context,
    context_digest,
)


def _run(run_id="run-a"):
    return SimpleNamespace(
        id=run_id,
        request="完成支付模块修复并运行回归测试",
        snapshot={
            "project": {"instructions": "保持公开 API 兼容；不得提交密钥"},
            "runtime": {"permission_profile": "ask"},
        },
    )


def _task(run_id="run-a", key="member-a"):
    return SimpleNamespace(
        id=f"{run_id}:{key}", run_id=run_id, key=key, agent_id="agent-a",
        title="修复支付模块", status="running",
        spec={"description": "修复支付模块中的金额校验", "acceptance": "现有回归测试通过"},
        result={"open_issues": ["等待测试证据"], "deliverables": ["src/payment.py"]},
    )


def _event(event_id, kind, data):
    return SimpleNamespace(id=event_id, kind=kind, data=data)


def test_memory_is_scoped_to_task_and_keeps_receipts():
    run = _run()
    task = _task()
    events = [
        _event(1, "runtime.item.started", {
            "source": "codex", "task_id": "run-a:member-a",
            "sequence": 2, "payload": {"operation_id": "op-a", "status": "started", "path": "src/payment.py"},
        }),
        _event(2, "runtime.item.completed", {
            "source": "codex", "task_id": "run-a:other-member",
            "sequence": 3, "payload": {"operation_id": "op-other", "status": "completed"},
        }),
        _event(3, "runtime.item.started", {
            "source": "codex", "sequence": 4, "payload": {"operation_id": "leader-only", "status": "started"},
        }),
    ]
    context = build_memory_context(run, task, events)
    assert context["scope"] == {"run_id": "run-a", "task_id": "run-a:member-a", "task_key": "member-a", "agent_id": "agent-a"}
    assert "等待测试证据" in context["unresolved"]
    assert "src/payment.py" in context["files"]
    assert any(item.get("operation_id") == "op-a" for item in context["tool_receipts"])
    assert all(item.get("operation_id") != "op-other" for item in context["tool_receipts"])
    assert all(item.get("operation_id") != "leader-only" for item in context["tool_receipts"])


def test_compression_retains_required_categories_and_is_deterministic():
    context = {
        "version": 1,
        "scope": {"run_id": "r", "task_id": "r:t", "task_key": "t", "agent_id": "a"},
        "goal": "目标 " * 1000,
        "constraints": ["约束 " * 1000],
        "decisions": ["决定 " * 1000],
        "unresolved": ["未完成 " * 1000],
        "files": ["src/result.py"],
        "tool_receipts": [{"operation_id": "op-1", "status": "completed", "summary": "工具收据 " * 1000}],
    }
    compact = compress_memory_context(context, max_chars=3000)
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= 3000
    assert all(key in compact for key in ("goal", "constraints", "decisions", "unresolved", "files", "tool_receipts"))
    assert compact["files"] == ["src/result.py"]
    assert context_digest(compact) == context_digest(compress_memory_context(context, max_chars=3000))


def test_cross_run_memory_is_rejected():
    with pytest.raises(ValueError, match="跨运行"):
        build_memory_context(_run("run-a"), _task("run-b"))

