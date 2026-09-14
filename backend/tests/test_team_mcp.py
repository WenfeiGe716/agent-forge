import json

from forge.team_mcp import TOOLS, dispatch


def _ctx(monkeypatch):
    monkeypatch.setenv("FORGE_TEAM_RUN_ID", "r" * 32)
    monkeypatch.setenv("FORGE_TEAM_TASK_KEY", "codex")
    monkeypatch.setenv("FORGE_TEAM_DB_URL", "sqlite:////tmp/forge.db")
    monkeypatch.setenv("FORGE_TEAM_DATA_ROOT", "/tmp/forge-data")
    from forge.team_mcp import TeamMcpContext
    return TeamMcpContext()


def test_team_mcp_advertises_bounded_leader_tools(monkeypatch):
    response = dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, _ctx(monkeypatch))
    names = {item["name"] for item in response["result"]["tools"]}
    assert {"inspect_team", "assign_work", "send_team_message", "retry_activation", "request_decision"} <= names
    assert len(names) == len(TOOLS)


def test_team_mcp_initialize_is_standard_and_notifications_are_silent(monkeypatch):
    ctx = _ctx(monkeypatch)
    response = dispatch({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {}}, ctx)
    assert response["result"]["protocolVersion"] == "2024-11-05"
    assert dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}, ctx) is None


def test_team_mcp_unknown_method_returns_json_rpc_error(monkeypatch):
    response = dispatch({"jsonrpc": "2.0", "id": 9, "method": "unknown"}, _ctx(monkeypatch))
    assert response["error"]["code"] == -32601


def test_team_mcp_routes_tool_call_with_run_and_task_scope(monkeypatch):
    ctx = _ctx(monkeypatch)
    seen = {}
    import forge.autonomy as autonomy
    monkeypatch.setattr(autonomy, "inspect_team", lambda run, task, args: seen.update({"run": run, "task": task, "args": args}) or {"members": []})
    response = dispatch({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                         "params": {"name": "inspect_team", "arguments": {}}}, ctx)
    assert not response["result"].get("isError", False)
    assert seen == {"run": "r" * 32, "task": "codex", "args": {}}
    assert json.loads(response["result"]["content"][0]["text"]) == {"members": []}


def test_team_mcp_recovery_copies_result_before_session_expires(monkeypatch):
    ctx = _ctx(monkeypatch)
    import forge.autonomy as autonomy
    from types import SimpleNamespace
    monkeypatch.setattr(autonomy, "request_recovery", lambda db, run, source, action, recipient, reason:
                        SimpleNamespace(key="auto_fix", status="pending"))
    # Avoid opening the real database for this routing test while retaining
    # the same transaction shape used by the production adapter.
    class FakeSession:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def get(self, model, key):
            return SimpleNamespace(id=ctx.run_id)
    monkeypatch.setattr("forge.team_mcp.session", lambda: FakeSession(), raising=False)
    # The adapter imports session from forge.db inside call_tool's branch.
    import forge.db as db
    monkeypatch.setattr(db, "session", lambda: FakeSession())
    response = dispatch({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "retry_activation", "arguments": {"task_key": "failed", "reason": "再试"}}}, ctx)
    assert not response["result"].get("isError", False)
    assert json.loads(response["result"]["content"][0]["text"]) == {"task_key": "auto_fix", "status": "pending", "action": "retry"}
