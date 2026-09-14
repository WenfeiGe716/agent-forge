"""Task-scoped memory projection and deterministic compaction.

Codex owns its own thread history and may emit native compaction events.  Forge
still needs a small control-plane projection so a leader can recover a task
after restart and so parallel tasks cannot accidentally share context.  This
module never calls a model and never reads the user's ``~/.codex``.  It only
projects already persisted Run/Task/Event records into an explicitly scoped,
bounded JSON document.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any, Iterable

from sqlalchemy import select

from .db import Event, MemoryContext, Run, Task, session, now

_CATEGORIES = ("constraints", "decisions", "unresolved", "files", "tool_receipts")
_MAX_ITEM = 1800


def _text(value: Any, limit: int = _MAX_ITEM) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        value = value.strip()
    else:
        try:
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            value = str(value)
    return str(value)[:limit]


def _unique(values: Iterable[Any], limit: int = 100) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        if value in (None, "", [], {}):
            continue
        try:
            marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            marker = repr(value)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _event_task_key(event: Event) -> str | None:
    data = event.data if isinstance(event.data, dict) else {}
    task_id = data.get("task_id")
    if isinstance(task_id, str) and ":" in task_id:
        return task_id.rsplit(":", 1)[-1]
    key = data.get("task_key")
    return key if isinstance(key, str) else None


def _event_matches(event: Event, task: Task) -> bool:
    """Match only this task; run-level events are leader-only context."""
    key = _event_task_key(event)
    if key is not None:
        return key == task.key
    data = event.data if isinstance(event.data, dict) else {}
    # Runtime leader events historically had no task_id.  They belong to the
    # visible ``codex`` task only; never leak them into member contexts.
    return task.key == "codex" and data.get("source") == "codex"


def build_memory_context(run: Run, task: Task, events: Iterable[Event] = ()) -> dict[str, Any]:
    """Build a lossless-by-category memory projection for one Task × Member.

    The shape is intentionally stable and machine-readable.  It contains no
    raw model transcript; tool output and deltas remain in RuntimeEvent.  A
    caller can retain the returned context as-is or pass it through
    :func:`compress_memory_context` for a bounded prompt fragment.
    """
    if run.id != task.run_id:
        raise ValueError("任务记忆不能跨运行读取")
    snapshot = run.snapshot if isinstance(run.snapshot, dict) else {}
    project = snapshot.get("project") if isinstance(snapshot.get("project"), dict) else {}
    spec = task.spec if isinstance(task.spec, dict) else {}
    result = task.result if isinstance(task.result, dict) else {}

    goal = _text(spec.get("description") or spec.get("instruction") or task.title or run.request, 6000)
    if goal != _text(run.request, 6000) and task.key == "codex":
        # Keep the actual user goal even when a later continuation has a short
        # task-specific prompt.
        goal = _text(run.request, 6000) + "\n本任务分工：" + goal

    constraints = [
        _text(project.get("instructions"), 4000),
        _text(spec.get("acceptance"), 2500),
        _text(spec.get("constraints"), 2500),
        _text((snapshot.get("runtime") or {}).get("permission_profile"), 500),
        _text((spec.get("execution_policy") if isinstance(spec.get("execution_policy"), dict) else {}), 1800),
    ]
    decisions = []
    for source in (spec.get("decisions"), result.get("decisions"), result.get("decision")):
        if isinstance(source, list):
            decisions.extend(_text(item) for item in source)
        elif source:
            decisions.append(_text(source))
    unresolved = []
    for source in (result.get("open_issues"), result.get("unresolved"), result.get("issues")):
        if isinstance(source, list):
            unresolved.extend(_text(item) for item in source)
        elif source:
            unresolved.append(_text(source))
    if task.status not in {"done", "integrated", "completed", "superseded"}:
        unresolved.append(f"当前任务状态：{task.status}")
    if result.get("error"):
        unresolved.append("最近错误：" + _text(result.get("error"), 2200))
    if spec.get("last_error"):
        unresolved.append("最近运行错误：" + _text(spec.get("last_error"), 2200))

    files: list[Any] = []
    for source in (result.get("deliverables"), result.get("files"), spec.get("files"), spec.get("changed_files")):
        if isinstance(source, list):
            files.extend(_text(item, 1000) for item in source)
        elif source:
            files.append(_text(source, 1000))

    receipts: list[Any] = []
    for source in (result.get("tool_receipts"), spec.get("tool_receipts")):
        if isinstance(source, list):
            receipts.extend(deepcopy(source))
        elif source:
            receipts.append(deepcopy(source))
    for event in events:
        if not _event_matches(event, task):
            continue
        kind = str(event.kind or "")
        data = event.data if isinstance(event.data, dict) else {}
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        lower = kind.lower()
        if not (any(token in lower for token in ("tool", "command", "approval", "file", "mcp", "turn/failed"))
                or any(key in payload for key in ("operation_id", "call_id", "command", "path"))):
            continue
        receipt = {
            "event_id": event.id,
            "kind": kind,
            "sequence": data.get("sequence", 0),
            "operation_id": payload.get("operation_id") or payload.get("call_id") or payload.get("id"),
            "status": payload.get("status") or data.get("status"),
            "summary": _text(payload.get("summary") or payload.get("error") or payload.get("path"), 900),
        }
        receipts.append({key: value for key, value in receipt.items() if value not in (None, "")})

    # Legacy activations keep their transcript on Task.messages.  Keep only
    # bounded evidence from tool messages (the full transcript remains in the
    # task/event log), and promote explicit decision messages so a resumed
    # activation can see prior human choices without importing another task.
    messages = task.messages if isinstance(getattr(task, "messages", None), list) else []
    for message in messages[-200:]:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content", "")
        if isinstance(content, list):
            content = " ".join(_text(item, 500) for item in content)
        content = _text(content, 1200)
        if not content:
            continue
        if role == "tool":
            receipts.append({
                "source": "task_message", "name": _text(message.get("name"), 160),
                "status": "recorded", "summary": content,
            })
        elif role == "user" and any(token in content.lower() for token in ("决定", "决策", "审批", "approval", "decision")):
            decisions.append(content)

    return {
        "version": 1,
        "scope": {"run_id": run.id, "task_id": task.id, "task_key": task.key, "agent_id": task.agent_id},
        "goal": goal,
        "constraints": _unique(constraints),
        "decisions": _unique(decisions),
        "unresolved": _unique(unresolved),
        "files": _unique(files),
        "tool_receipts": _unique(receipts),
        "source": {"task_status": task.status, "updated_at": now()},
    }


def _compact_item(item: Any, limit: int = 900) -> Any:
    if isinstance(item, dict):
        compact = {}
        for key, value in item.items():
            compact[str(key)] = _compact_item(value, limit // 2 if isinstance(value, (dict, list)) else limit)
        return compact
    if isinstance(item, list):
        return [_compact_item(value, limit) for value in item[:20]]
    return _text(item, limit)


def compress_memory_context(context: dict[str, Any], max_chars: int = 12000) -> dict[str, Any]:
    """Deterministically bound a context while retaining every category.

    No category is discarded.  Long lists are compacted item-by-item and a
    ``truncated`` marker records what was omitted.  This makes compression
    auditable and safe to replay without a model-generated summary.
    """
    if not isinstance(context, dict):
        raise ValueError("任务记忆必须是对象")
    if not isinstance(max_chars, int) or max_chars < 1024:
        raise ValueError("记忆压缩上限至少为 1024 字符")
    compact = {
        "version": context.get("version", 1),
        "scope": deepcopy(context.get("scope", {})),
        "goal": _text(context.get("goal"), max(300, int(max_chars * 0.22))),
    }
    budgets = {"constraints": .17, "decisions": .15, "unresolved": .18, "files": .13, "tool_receipts": .25}
    for category in _CATEGORIES:
        source = context.get(category)
        values = source if isinstance(source, list) else ([source] if source not in (None, "") else [])
        budget = max(180, int(max_chars * budgets[category]))
        encoded: list[Any] = []
        used = 2
        omitted = 0
        for item in values:
            candidate = _compact_item(item, min(900, budget))
            size = len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) + 1
            if used + size > budget:
                omitted += 1
                continue
            encoded.append(candidate)
            used += size
        if omitted:
            encoded.append({"truncated": omitted})
        compact[category] = encoded
    compact["source"] = {"compressed": True, "original_version": context.get("version", 1)}
    # A tiny scope/goal can still be over budget with unusually large IDs.
    serialized = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > max_chars:
        # Keep the category keys even at the smallest supported budget.  Replace
        # the least important tail first; the marker records that compression
        # happened instead of silently deleting evidence.
        for category in reversed(_CATEGORIES):
            values = compact[category]
            if not values:
                continue
            if len(values) > 1:
                compact[category] = [values[0], {"truncated": len(values) - 1}]
            elif not (isinstance(values[0], dict) and set(values[0]) == {"truncated"}):
                compact[category] = [{"truncated": 1}]
            serialized = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
            if len(serialized) <= max_chars:
                break
        if len(serialized) > max_chars:
            compact["goal"] = _text(compact["goal"], max(120, max_chars // 16))
            scope = compact.get("scope", {}) if isinstance(compact.get("scope"), dict) else {}
            compact["scope"] = {key: _text(scope.get(key), 120)
                                 for key in ("run_id", "task_id", "task_key", "agent_id")}
        # A pathological scope (e.g. a generated ID) should never make the
        # advertised bound false.  This final pass is structural, not semantic:
        # category keys remain present and all omitted values are auditable.
        serialized = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) > max_chars:
            compact["goal"] = "[目标已压缩]"
            compact["scope"] = {"task_key": _text(compact.get("scope", {}).get("task_key"), 80)}
            for category in _CATEGORIES:
                compact[category] = [{"truncated": 1}] if compact[category] else []
    return compact


def context_digest(context: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def prompt_fragment(context: dict[str, Any], max_chars: int = 6000) -> str:
    bounded = compress_memory_context(context, max_chars=max_chars)
    return "任务记忆（仅限当前 run × task，不跨任务；压缩 projection，不替代实时事件）：\n" + json.dumps(
        bounded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compact_conversation(messages: list[dict[str, Any]], *, max_chars: int,
                         memory: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build a bounded provider history without mutating the durable transcript.

    Codex's own thread remains authoritative.  The legacy in-process adapter
    keeps every message on ``Task.messages`` but may need a smaller request when
    a task runs for many turns.  We retain the system contract, the initial
    task envelope and complete assistant/tool groups from the newest turns;
    omitted messages are represented by a deterministic, task-scoped digest and
    the persisted memory projection.  No message from another task is accepted.
    """
    if not isinstance(messages, list):
        raise ValueError("模型历史必须是列表")
    if not isinstance(max_chars, int) or max_chars < 1024:
        raise ValueError("模型历史压缩上限至少为 1024 字符")
    original = deepcopy(messages)
    encoded = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded(original)) <= max_chars:
        return original, {"compressed": False, "omitted_messages": 0}

    # Keep the protocol preamble.  The first system/user pair carries frozen
    # role and task scope; later messages are grouped into turns below.
    prefix = original[:2]
    tail = original[2:]
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for item in tail:
        role = item.get("role") if isinstance(item, dict) else None
        # A user message starts a new turn unless it is the tool result group
        # following an assistant call.  This preserves valid tool call/result
        # pairing for OpenAI, Anthropic and Gemini adapters.
        if current and (role == "user" or
                        (role == "assistant" and any(m.get("role") == "assistant" for m in current))):
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)

    scope = (memory or {}).get("scope", {}) if isinstance(memory, dict) else {}
    digest = context_digest({"scope": scope, "messages": original})
    omitted = len(tail)
    summary = {
        "scope": deepcopy(scope),
        "omitted_messages": omitted,
        "omitted_digest": digest,
        "memory": compress_memory_context(memory, max_chars=max(1024, min(2800, max_chars // 2))) if memory else None,
    }
    summary_message = {"role": "user", "content":
                       "此前回合已保留在当前任务的持久化记录中；以下是可审计的压缩摘要，不是新指令：\n" +
                       encoded(summary)}

    # Keep newest complete groups.  If a single group is too large, reducing
    # to the prefix + summary still preserves the exact durable transcript.
    kept: list[list[dict[str, Any]]] = []
    for group in reversed(groups):
        candidate = prefix + [summary_message] + group + [m for g in kept for m in g]
        if len(encoded(candidate)) > max_chars:
            break
        kept.insert(0, group)
    compact = prefix + [summary_message] + [m for group in kept for m in group]
    # Extremely small budgets can still exceed the cap due to the system
    # contract.  Shrink only the generated summary, never the live messages.
    if len(encoded(compact)) > max_chars:
        summary_message["content"] = "此前回合已压缩；完整内容保留在当前任务记录。\n" + encoded({
            "scope": deepcopy(scope), "omitted_messages": omitted, "omitted_digest": digest})
        compact = prefix + [summary_message]
    return compact, {"compressed": True, "omitted_messages": omitted - sum(len(group) for group in kept),
                     "omitted_digest": digest, "scope": deepcopy(scope)}


def refresh_task_memory(run_id: str, task_key: str, *, max_chars: int = 12000) -> dict[str, Any]:
    """Rebuild and persist one task's memory projection from durable facts."""
    task_id = f"{run_id}:{task_key}"
    with session() as db:
        run = db.get(Run, run_id)
        task = db.get(Task, task_id)
        if not run or not task:
            raise ValueError("任务记忆不存在")
        rows = list(db.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.id)))
        raw = build_memory_context(run, task, rows)
        bounded = compress_memory_context(raw, max_chars=max_chars)
        row = db.get(MemoryContext, task_id)
        if row is None:
            row = MemoryContext(task_id=task_id, run_id=run_id, task_key=task_key,
                                agent_id=task.agent_id, context=bounded, compressed=True)
            db.add(row)
        else:
            if row.run_id != run_id or row.task_key != task_key:
                raise ValueError("任务记忆作用域不匹配")
            row.agent_id = task.agent_id
            row.version = int(row.version or 0) + 1
            row.context = bounded
            row.compressed = True
            row.updated_at = now()
        return deepcopy(bounded)


def load_task_memory(run_id: str, task_key: str) -> dict[str, Any] | None:
    with session() as db:
        row = db.get(MemoryContext, f"{run_id}:{task_key}")
        if not row or row.run_id != run_id or row.task_key != task_key:
            return None
        return deepcopy(row.context)
