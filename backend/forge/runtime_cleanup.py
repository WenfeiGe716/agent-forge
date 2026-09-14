"""Explicit cleanup of persisted execution data.

Configuration assets (providers, models, agents, skills, teams/layouts and
projects) are deliberately outside this module's deletion set.  The command
is dry-run by default; ``--delete`` is required for mutation.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import delete as sql_delete, select, func

from .config import settings
from .db import (
    ActionItem, ActivityJournal, AgentMessage, DecisionRequest, Event,
    HumanInstruction, InstructionReceipt, Meeting, Outbox, Run, Step, Task,
    MemoryContext, Provider, RuntimeApproval, init_db, session,
)

_RUN_SCOPED = (
    Run, Task, Event, AgentMessage, ActivityJournal, Outbox,
    DecisionRequest, HumanInstruction, InstructionReceipt, Meeting, ActionItem,
    RuntimeApproval, MemoryContext,
)


def _safe_runtime_dir(path: Path, label: str) -> Path:
    """Validate a directory before deleting its children, never the directory."""
    raw = Path(path).expanduser()
    # Check the lexical target before resolve(); resolving first would hide a
    # symlink and could redirect deletion outside the configured data root.
    if raw.is_symlink():
        raise ValueError(f"拒绝清理符号链接目录（{label}）")
    value = raw.resolve(strict=False)
    home = Path.home().resolve()
    # Projects commonly live beneath the user's home; only the home itself,
    # Codex/OpenCodex state directories, and their descendants are forbidden.
    if value == home or any(part in {".codex", ".opencodex"} for part in value.parts):
        raise ValueError(f"拒绝清理个人目录（{label}）")
    if value == Path("/") or len(value.parts) < 3:
        raise ValueError(f"拒绝清理不安全的目录（{label}）")
    if value.exists() and not value.is_dir():
        raise ValueError(f"运行目录不是目录（{label}）")
    return value


def _fs_inventory(path: Path) -> dict[str, int]:
    entries = files = directories = bytes_ = 0
    if not path.exists():
        return {"entries": 0, "files": 0, "directories": 0, "bytes": 0}
    for child in path.iterdir():
        entries += 1
        if child.is_symlink():
            continue
        if child.is_dir():
            directories += 1
            nested = _fs_inventory(child)
            entries += nested["entries"]
            files += nested["files"]
            directories += nested["directories"]
            bytes_ += nested["bytes"]
        elif child.is_file():
            files += 1
            try:
                bytes_ += child.stat().st_size
            except OSError:
                pass
    return {"entries": entries, "files": files, "directories": directories, "bytes": bytes_}


def _remove_children(path: Path) -> None:
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for child in path.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)


def _count(s, model, where=None) -> int:
    statement = select(func.count()).select_from(model)
    if where is not None:
        statement = statement.where(where)
    return int(s.scalar(statement) or 0)


def _runtime_step_ids(s, run_ids: list[str], task_ids: list[str]) -> list[str]:
    # ``setup:*`` is the idempotency ledger for configuration probes and must
    # survive. Other Step rows are execution state, including orphaned rows
    # left by a prior interrupted cleanup.
    return [row.id for row in s.scalars(select(Step))
            if not row.id.startswith("setup:")]


def cleanup_runtime(*, delete: bool = False, data_root: Path | str | None = None,
                    runtime_data_root: Path | str | None = None,
                    session_factory: Callable = session) -> dict[str, Any]:
    """Report or delete run-scoped records and project runtime directories.

    ``delete=False`` performs no mutation.  The returned report is JSON-safe and
    includes counts before deletion; configuration table counts are not touched.
    """
    root = Path(data_root or settings.data_root).expanduser().resolve(strict=False)
    runs_dir = _safe_runtime_dir(root / "runs", "data/runs")
    runtime_dir = _safe_runtime_dir(Path(runtime_data_root or settings.runtime_data_root), "runtime")
    with session_factory() as s:
        run_ids = [row.id for row in s.scalars(select(Run))]
        task_ids = [row.id for row in s.scalars(select(Task).where(Task.run_id.in_(run_ids)))] if run_ids else []
        counts: dict[str, int] = {}
        for model in _RUN_SCOPED:
            name = model.__tablename__
            if model is Event:
                counts[name] = _count(s, model, Event.run_id.in_(run_ids)) if run_ids else 0
            elif model is Run:
                counts[name] = len(run_ids)
            elif hasattr(model, "run_id"):
                counts[name] = _count(s, model, model.run_id.is_not(None))
        step_ids = _runtime_step_ids(s, run_ids, task_ids)
        counts["steps"] = len(step_ids)
        report: dict[str, Any] = {
            "deleted": False,
            "database": counts,
            "filesystem": {"runs": _fs_inventory(runs_dir), "runtime": _fs_inventory(runtime_dir)},
            "kept": {"provider_id": (s.scalar(select(Provider.id).limit(1)) or None)},
        }
        if delete:
            # Delete children first; foreign keys are currently implicit but the
            # order keeps this safe if constraints are enabled in a future schema.
            for model in (InstructionReceipt, HumanInstruction, DecisionRequest,
                          ActionItem, Meeting, AgentMessage, ActivityJournal,
                          Outbox, Event, RuntimeApproval, MemoryContext, Task, Run):
                if model is Event:
                    if run_ids:
                        s.execute(sql_delete(model).where(model.run_id.in_(run_ids)))
                elif model is Run:
                    if run_ids:
                        s.execute(sql_delete(model).where(model.id.in_(run_ids)))
                elif hasattr(model, "run_id"):
                    s.execute(sql_delete(model).where(model.run_id.is_not(None)))
            if step_ids:
                s.execute(sql_delete(Step).where(Step.id.in_(step_ids)))
            _remove_children(runs_dir)
            _remove_children(runtime_dir)
            report["deleted"] = True
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理 Agent Forge 运行记录（默认仅预览）")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--dry-run", action="store_true", help="仅显示计数（默认）")
    action.add_argument("--delete", action="store_true", help="确认删除运行记录与临时目录内容")
    args = parser.parse_args(argv)
    # A fresh installation may not have opened the server yet; creating the
    # schema is non-destructive and lets the dry-run command report zeroes.
    init_db()
    report = cleanup_runtime(delete=bool(args.delete))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
