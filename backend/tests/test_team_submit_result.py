"""The Codex team MCP result boundary uses the same real evidence rules as finish."""

import pytest

from forge import autonomy, engine, workspace as ws
from forge.db import Run, Task, session
from conftest import run_body


def _prepared(client, configured):
    lead = next(a for a in configured["agents"] if a["role"] == "planner")
    response = client.post("/api/runs", json=run_body(
        configured, execution_strategy="autonomous", entry_agent_id=lead["id"],
        agent_ids=[lead["id"]], title="submit result test"))
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    engine.prepare_run({"run_id": run_id})
    engine.set_stage({"run_id": run_id, "status": "running"})
    state = autonomy.autonomy_tick({"run_id": run_id, "tick": 0})
    key = state["runnable"][0]["key"]
    with session() as db:
        run, task = db.get(Run, run_id), db.get(Task, f"{run_id}:{key}")
        spec, title, agent_id = dict(task.spec), task.title, task.agent_id
    engine.create_task(run_id, key, "autonomous", agent_id, title, spec)
    return run_id, key


def test_submit_result_requires_real_done_evidence(client, configured):
    run_id, key = _prepared(client, configured)
    with pytest.raises(ValueError, match="实际成果"):
        autonomy.submit_result(run_id, key, {
            "outcome": "done", "summary": "完成了", "notes": "",
        })
    ws.write_workspace_file(ws.task_root(run_id, key), "result.md", b"real deliverable\n", 10000)
    result = autonomy.submit_result(run_id, key, {
        "outcome": "done", "summary": "已生成真实成果文件", "notes": "",
        "deliverables": ["result.md"],
    })
    assert result["status"] == "done"
    with session() as db:
        task = db.get(Task, f"{run_id}:{key}")
        assert task.status == "done"
        assert task.result["deliverables"] == ["result.md"]
        assert task.result["submission_digest"]


def test_submit_result_is_idempotent_and_rejects_overwrite(client, configured):
    run_id, key = _prepared(client, configured)
    ws.write_workspace_file(ws.task_root(run_id, key), "result.md", b"stable\n", 10000)
    payload = {
        "outcome": "done", "summary": "已提交稳定成果", "notes": "",
        "deliverables": ["result.md"],
    }
    first = autonomy.submit_result(run_id, key, payload)
    second = autonomy.submit_result(run_id, key, payload)
    assert first["status"] == second["status"] == "done"
    assert second["idempotent"] is True
    with pytest.raises(ValueError, match="不同内容覆盖"):
        autonomy.submit_result(run_id, key, {**payload, "summary": "篡改后的结果"})


def test_submit_result_requires_frozen_finish_permission(client, configured):
    run_id, key = _prepared(client, configured)
    with session() as db:
        run = db.get(Run, run_id)
        snapshot = dict(run.snapshot)
        agents = dict(snapshot["agents"])
        agent = dict(agents[next(iter(agents))])
        agent["tools"] = [tool for tool in agent.get("tools", []) if tool != "finish"]
        agents[agent["id"]] = agent
        snapshot["agents"] = agents
        run.snapshot = snapshot
    with pytest.raises(ValueError, match="finish"):
        autonomy.submit_result(run_id, key, {
            "outcome": "blocked", "summary": "没有提交权限", "notes": "",
        })


def test_submit_blocked_result_stays_recoverable(client, configured):
    run_id, key = _prepared(client, configured)
    result = autonomy.submit_result(run_id, key, {
        "outcome": "blocked", "summary": "工具执行失败，等待负责人读取真实错误并决定重试。", "notes": "",
    })
    assert result["outcome"] == "blocked"
    with session() as db:
        task = db.get(Task, f"{run_id}:{key}")
        assert task.status == "needs_attention"
    # A blocker is recoverable, but never auto-replayed by the autonomy tick.
    state = autonomy.autonomy_tick({"run_id": run_id, "tick": 1})
    assert state["runnable"] == []
