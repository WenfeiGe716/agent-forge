"""A small, run-scoped MCP server exposed to the Codex team lead.

The server is intentionally boring: it is a stdio JSON-RPC adapter over the
existing Forge autonomy functions.  It does not contain an agent loop, grant
permissions, or read another run.  A separate process is used so a model can
only reach the bounded business tools that were placed in its frozen runtime
configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from typing import Any

from .schema import AutonomousFinish


PROTOCOL_VERSION = "2024-11-05"


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required or [],
        },
    }


def _schema_model(model: Any) -> dict[str, Any]:
    """Expose a strict Forge schema without leaking Pydantic internals."""
    schema = model.model_json_schema()
    schema.pop("$defs", None)
    return schema


TOOLS = [
    _tool("inspect_team", "查看当前任务冻结团队的成员能力、模型、Skill、工具和工作状态。", {
        "member_id": {"type": "string", "description": "可选；指定后查看该成员完整职责。"},
    }),
    _tool("assign_work", "由负责人把一项工作委派给当前团队的其他成员。", {
        "recipient_id": {"type": "string"}, "title": {"type": "string"},
        "instruction": {"type": "string"}, "acceptance": {"type": "string"},
    }, ["recipient_id", "title", "instruction", "acceptance"]),
    _tool("send_team_message", "向当前团队成员发送有界协作消息，可选择是否请求回复。", {
        "recipient_id": {"type": "string"}, "body": {"type": "string"},
        "request_reply": {"type": "boolean"},
    }, ["recipient_id", "body"]),
    {"name": "submit_result", "description": "提交当前成员真实工作结果；done/blocked 会更新本成员任务，ready 仅负责人可提交为交付候选。",
     "inputSchema": _schema_model(AutonomousFinish)},
    _tool("read_work_result", "读取本次运行中已集成成员成果的指定文件。", {
        "task_key": {"type": "string"}, "path": {"type": "string"},
    }, ["task_key", "path"]),
    _tool("read_work_issue", "读取成员失败或冲突的真实证据。", {
        "task_key": {"type": "string"}, "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0},
    }, ["task_key"]),
    _tool("retry_activation", "检查失败证据后，在原任务内重试原成员工作。", {
        "task_key": {"type": "string"}, "reason": {"type": "string"},
    }, ["task_key"]),
    _tool("reassign_activation", "检查失败证据后，把工作转给当前团队的另一成员。", {
        "task_key": {"type": "string"}, "recipient_id": {"type": "string"}, "reason": {"type": "string"},
    }, ["task_key", "recipient_id"]),
    _tool("continue_activation", "在原任务内继续一次可恢复的成员工作。", {
        "task_key": {"type": "string"}, "reason": {"type": "string"},
    }, ["task_key"]),
    _tool("resolve_work_issue", "负责人引用真实修复成果并关闭一个已核验的问题。", {
        "task_key": {"type": "string"}, "evidence_task_keys": {"type": "array", "items": {"type": "string"}},
        "resolution": {"type": "string"},
    }, ["task_key", "evidence_task_keys", "resolution"]),
    _tool("request_decision", "向用户提出一个有界、可审计的任务决策问题。", {
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "object"}},
        "allow_text": {"type": "boolean"},
    }, ["question", "options"]),
    _tool("start_discussion", "仅在确实需要多人同步时召集当前团队成员讨论。", {
        "participants": {"type": "array", "items": {"type": "string"}}, "agenda": {"type": "string"},
    }, ["participants", "agenda"]),
    _tool("end_discussion", "记录讨论结论并结束当前团队会议。", {
        "meeting_id": {"type": "string"}, "summary": {"type": "string"},
    }, ["meeting_id", "summary"]),
]


class TeamMcpError(RuntimeError):
    pass


class TeamMcpContext:
    def __init__(self) -> None:
        self.run_id = os.environ.get("FORGE_TEAM_RUN_ID", "")
        self.task_key = os.environ.get("FORGE_TEAM_TASK_KEY", "")
        db_url = os.environ.get("FORGE_TEAM_DB_URL", "")
        data_root = os.environ.get("FORGE_TEAM_DATA_ROOT", "")
        if not self.run_id or not self.task_key or not db_url or not data_root:
            raise TeamMcpError("团队 MCP 运行上下文不完整")
        # db.py reads DATABASE_URL at import time.  Only this child process is
        # changed; the parent and the operator's personal configuration remain
        # untouched.
        os.environ["DATABASE_URL"] = db_url
        os.environ["DATA_ROOT"] = data_root

    @property
    def operation(self) -> str:
        return hashlib.sha256((self.run_id + ":" + self.task_key).encode()).hexdigest()[:32]


def _result(value: Any) -> dict[str, Any]:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {"content": [{"type": "text", "text": text}]}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": str(message)[:4000]}], "isError": True}


def call_tool(ctx: TeamMcpContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from . import autonomy
    from .collaboration import send_message
    from .engagement import request_decision
    from .work_issues import read_work_issue, resolve_work_issue

    args = arguments if isinstance(arguments, dict) else {}
    operation = hashlib.sha256(json.dumps([name, args], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:40]
    if name == "inspect_team": value = autonomy.inspect_team(ctx.run_id, ctx.task_key, args)
    elif name == "assign_work": value = autonomy.assign_work(ctx.run_id, ctx.task_key, operation, args)
    elif name == "submit_result": value = autonomy.submit_result(ctx.run_id, ctx.task_key, args)
    elif name == "send_team_message":
        value = send_message(ctx.run_id, ctx.task_key, operation, args.get("recipient_id", ""), args.get("body", ""), args.get("request_reply"))
    elif name == "read_work_result": value = autonomy.read_work_result(ctx.run_id, ctx.task_key, args)
    elif name == "read_work_issue": value = read_work_issue(ctx.run_id, ctx.task_key, args)
    elif name in {"retry_activation", "reassign_activation", "continue_activation"}:
        action = {"retry_activation": "retry", "reassign_activation": "reassign", "continue_activation": "continue"}[name]
        from .db import Run, session
        with session() as db:
            run = db.get(Run, ctx.run_id)
            if not run: raise TeamMcpError("任务不存在")
            recovery = autonomy.request_recovery(db, run, args.get("task_key", ""), action,
                args.get("recipient_id"), args.get("reason", ""))
            # Session commits expire ORM objects; copy the bounded fields while
            # the transaction is still open instead of returning a detached row.
            value = {"task_key": recovery.key, "status": recovery.status, "action": action}
    elif name == "resolve_work_issue": value = resolve_work_issue(ctx.run_id, ctx.task_key, operation, args)
    elif name == "request_decision": value = request_decision(ctx.run_id, ctx.task_key, operation, args)
    elif name == "start_discussion": value = autonomy.start_discussion(ctx.run_id, ctx.task_key, operation, args)
    elif name == "end_discussion": value = autonomy.end_discussion(ctx.run_id, ctx.task_key, operation, args)
    else: raise TeamMcpError("未知团队工具：" + name)
    return _result(value)


def dispatch(message: dict[str, Any], ctx: TeamMcpContext) -> dict[str, Any] | None:
    method = message.get("method")
    ident = message.get("id")
    if method == "notifications/initialized" or (method and method.startswith("notifications/")):
        return None
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": ident, "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "agent-forge-team", "version": "0.1.0"},
        }}
    if method == "ping": return {"jsonrpc": "2.0", "id": ident, "result": {}}
    if method == "tools/list": return {"jsonrpc": "2.0", "id": ident, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        try:
            body = call_tool(ctx, params.get("name", ""), params.get("arguments") or {})
        except Exception as exc: body = _error(str(exc))
        return {"jsonrpc": "2.0", "id": ident, "result": body}
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "方法不支持"}}


def main() -> int:
    try:
        ctx = TeamMcpContext()
        for line in sys.stdin:
            if not line.strip(): continue
            try:
                message = json.loads(line)
                if not isinstance(message, dict): raise ValueError("JSON-RPC 消息必须是对象")
                response = dispatch(message, ctx)
            except Exception as exc:
                ident = message.get("id") if isinstance(message, dict) else None
                response = {"jsonrpc": "2.0", "id": ident, "error": {"code": -32000, "message": str(exc)[:1000]}}
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
                sys.stdout.flush()
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
