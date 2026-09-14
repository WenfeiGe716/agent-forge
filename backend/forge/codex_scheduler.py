"""Codex-first default scheduler seam.

The local scheduler owns process lifetime and admission; this module owns one
real Codex turn.  It is deliberately opt-in per *new* Run via the
``execution_engine`` snapshot field so enabling the feature never changes an
already-created task or silently replays its old workflow.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any
from sqlalchemy import select

from .codex_permissions import codex_execution_policy
from .codex_gateway import gateway_token_for
from .codex_task import CodexProvider, CodexSingleTask, CodexTaskError
from .config import settings
from .db import ProviderRevision, Run, RuntimeApproval, Task, emit, now, session, uid
from .runtime_bridge import RuntimeBridgeError, RuntimeEvent, turn_terminal_status
from .runtime_approvals import deliver_decision, expire_requests, record_request
from .runtime_events import RuntimeEventRecorder
from .security import decrypt
from .workspace import copy_repo, run_root


def _memory_prompt(run_id: str, task_key: str) -> str:
    """Read the durable projection without making memory a runtime blocker."""
    try:
        from .memory_context import prompt_fragment, refresh_task_memory
        return prompt_fragment(refresh_task_memory(run_id, task_key), max_chars=6000)
    except Exception:
        # Runtime execution remains governed by Codex if an optional projection
        # cannot be rebuilt; the failure is visible through normal runtime
        # events and can be diagnosed from the durable task/event records.
        return ""


def _team_mcp_context(run_id: str, task_key: str) -> dict[str, str] | None:
    if not settings.codex_team_mcp_enabled:
        return None
    # Resolve SQLite's relative path before the MCP child changes cwd to the
    # run worktree.  Non-SQLite deployments can pass the configured URL as-is.
    db_url = settings.database_url
    if db_url.startswith('sqlite:///') and not db_url.startswith('sqlite:////'):
        from pathlib import Path
        db_path = Path(db_url.removeprefix('sqlite:///')).resolve()
        db_url = 'sqlite:///' + str(db_path)
    return {'run_id': run_id, 'task_key': task_key, 'db_url': db_url,
            'data_root': str(settings.data_root)}


def _leader_prompt(run: Run, request: str, *, team_mcp: bool) -> str:
    """Give the real Codex lead a small, explicit control-plane contract."""
    if not team_mcp:
        return request
    return ("你是本次任务的负责人。你可以使用当前运行提供的 forge_team MCP 工具读取冻结团队能力。"
            "先判断是否需要协作：简单任务直接完成；复杂任务再用 assign_work 或 send_team_message。"
            "成员失败时先 read_work_issue/read_work_result，再决定 retry_activation、reassign_activation 或向用户 request_decision。"
            "不要虚构成员执行、工具结果、文件或测试；只有真实证据才能提交完成。"
            "每次负责人回合结束前必须调用 submit_result，准确报告 done、waiting、ready 或 blocked；"
            "ready 只在所有成员成果和验收证据已核对后使用。"
            "所有协作必须留在当前任务和冻结团队内，不能扩大权限或访问其他任务。\n\n"
            "用户任务：\n" + request)


def _frozen_provider(run: Run) -> tuple[dict[str, Any], str, str]:
    """Resolve only the frozen leader model; live config cannot retarget a run."""
    snapshot = run.snapshot or {}
    leader_id = snapshot.get("entry_agent_id") or snapshot.get("planner_id")
    agents = snapshot.get("agents") if isinstance(snapshot.get("agents"), dict) else {}
    leader = agents.get(leader_id) if leader_id else None
    models = leader.get("models") if isinstance(leader, dict) else None
    model = models[0] if isinstance(models, list) and models else None
    connection = model.get("connection") if isinstance(model, dict) else None
    revision_id = model.get("credential_revision") if isinstance(model, dict) else None
    model_id = ((model.get("model") or {}).get("model_id")
                if isinstance(model, dict) and isinstance(model.get("model"), dict) else None)
    if not isinstance(connection, dict) or connection.get("protocol") != "openai_responses":
        raise ValueError("负责人主模型协议不是 OpenAI Responses，暂不能接入 Codex 运行时")
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError("负责人主模型缺少供应商凭据版本")
    if not isinstance(model_id, str) or not model_id:
        raise ValueError("负责人主模型缺少上游模型 ID")
    return connection, revision_id, model_id


def _frozen_policy(run: Run) -> dict[str, str]:
    snapshot = run.snapshot or {}
    leader_id = snapshot.get("entry_agent_id") or snapshot.get("planner_id")
    agents = snapshot.get("agents") if isinstance(snapshot.get("agents"), dict) else {}
    leader = agents.get(leader_id) if leader_id else None
    frozen = leader.get("execution_policy") if isinstance(leader, dict) else None
    profile = frozen.get("profile") if isinstance(frozen, dict) else None
    if not isinstance(profile, str):
        project = snapshot.get("project") if isinstance(snapshot.get("project"), dict) else {}
        profile = project.get("permission_profile", "ask")
    return codex_execution_policy(profile)


def _frozen_agent_provider(run: Run, agent_id: str) -> tuple[dict[str, Any], str, str]:
    """Resolve a member's provider from the immutable run snapshot.

    Codex can only speak the Responses wire protocol in this adapter.  The
    member is deliberately rejected when its frozen model is incompatible;
    the leader can then see a concrete, task-scoped failure instead of the
    scheduler silently using another model.
    """
    snapshot = run.snapshot or {}
    agent = (snapshot.get("agents") or {}).get(agent_id)
    models = agent.get("models") if isinstance(agent, dict) else None
    model = models[0] if isinstance(models, list) and models else None
    connection = model.get("connection") if isinstance(model, dict) else None
    revision_id = model.get("credential_revision") if isinstance(model, dict) else None
    model_id = ((model.get("model") or {}).get("model_id")
                if isinstance(model, dict) and isinstance(model.get("model"), dict) else None)
    if not isinstance(connection, dict) or connection.get("protocol") != "openai_responses":
        raise ValueError(f"成员 {agent_id} 的主模型协议不是 OpenAI Responses")
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError(f"成员 {agent_id} 的主模型缺少供应商凭据版本")
    if not isinstance(model_id, str) or not model_id:
        raise ValueError(f"成员 {agent_id} 的主模型缺少上游模型 ID")
    return connection, revision_id, model_id


def _frozen_agent_policy(run: Run, agent_id: str) -> dict[str, str]:
    snapshot = run.snapshot or {}
    agent = (snapshot.get("agents") or {}).get(agent_id)
    frozen = agent.get("execution_policy") if isinstance(agent, dict) else None
    profile = frozen.get("profile") if isinstance(frozen, dict) else None
    if not isinstance(profile, str):
        project = snapshot.get("project") if isinstance(snapshot.get("project"), dict) else {}
        profile = project.get("permission_profile", "ask")
    return codex_execution_policy(profile)


def _set_run_state(run_id: str, status: str, *, error: str = "") -> None:
    with session() as s:
        run = s.get(Run, run_id)
        if not run or run.status in {"completed", "cancelled"}:
            return
        run.status, run.error, run.updated_at = status, error[:3000], now()
        emit(s, run_id, "run.state", {"status": status, "error": run.error, "source": "codex"})


def _ensure_codex_task(run: Run, leader_id: str | None) -> None:
    """Create the single visible task row used by the Codex-first slice."""
    key = "codex"
    with session() as s:
        task_id = f"{run.id}:{key}"
        task = s.get(Task, task_id)
        if task is None:
            task = Task(id=task_id, run_id=run.id, key=key, kind="autonomous",
                        agent_id=leader_id or "", title=run.title,
                        spec={"description": run.request, "engine": "codex"})
            s.add(task)
            emit(s, run.id, "task.prepared", {"task_key": key,
                "agent_id": leader_id, "title": run.title, "kind": "autonomous"})


def _update_codex_task(run_id: str, *, thread_id: str | None = None,
                       turn_id: str | None = None, status: str | None = None,
                       error: str | None = None) -> None:
    """Persist only runtime identity/state; never overwrite the frozen spec."""
    with session() as s:
        task = s.get(Task, f"{run_id}:codex")
        if not task:
            return
        spec = dict(task.spec or {})
        if thread_id:
            spec["thread_id"] = thread_id
        if turn_id:
            spec["turn_id"] = turn_id
        if error:
            spec["last_error"] = str(error)[:2200]
        task.spec = spec
        if status:
            task.status = status


class CodexRunExecutor:
    """Execute one frozen Run through Codex and persist every runtime event."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.task: CodexSingleTask | None = None
        # Each delegated member gets a real Codex thread, task working copy and
        # runtime home.  Keeping these maps on the run executor also gives the
        # UI/control plane a stable seam for per-member controls later.
        self.member_tasks: dict[str, CodexSingleTask] = {}
        self.member_jobs: dict[str, asyncio.Task] = {}
        self.cancel_requested = False
        self.approval_connection_id = uid()

    async def deliver_approval(self, approval_id: str) -> None:
        with session() as db:
            row = db.get(RuntimeApproval, approval_id)
            if not row or row.run_id != self.run_id:
                raise CodexTaskError('审批不属于此运行')
            key = row.task_key
        target = self.task if key == 'codex' else self.member_tasks.get(key)
        await deliver_decision(self.run_id, approval_id, self.approval_connection_id, target)

    def request_cancel(self) -> None:
        """Mark a scheduler cancellation distinct from a recoverable pause."""
        self.cancel_requested = True

    async def steer(self, prompt: str, task_key: str = "codex") -> dict[str, Any]:
        """Forward a live user/leader instruction to this frozen Codex turn."""
        target = self.task if task_key == "codex" else self.member_tasks.get(task_key)
        if not target:
            raise CodexTaskError(f"任务 {task_key} 的 Codex 回合尚未建立，暂时不能追加指令")
        result = await target.steer(prompt)
        with session() as s:
            emit(s, self.run_id, "runtime.control.steer", {
                "prompt_length": len(prompt), "task_key": task_key,
                "thread_id": target.thread_id, "turn_id": target.turn_id, "engine": "codex",
            })
        return result

    async def interrupt(self, task_key: str | None = None) -> None:
        """Interrupt one member turn, or all live turns for a run."""
        targets = []
        if task_key in (None, "codex") and self.task:
            targets.append(("codex", self.task))
        if task_key is None:
            targets.extend(self.member_tasks.items())
        elif task_key != "codex" and task_key in self.member_tasks:
            targets.append((task_key, self.member_tasks[task_key]))
        if not targets:
            raise CodexTaskError("当前没有可中断的 Codex 回合")
        await asyncio.gather(*(target.interrupt() for _, target in targets))

    async def _run_member(self, task_key: str) -> None:
        """Start one queued autonomous member activation in isolation.

        There is intentionally no fabricated success path here.  Codex member
        turns currently have no structured Forge ``finish`` tool, so a terminal
        turn is persisted as ``needs_attention`` for the leader to inspect and
        accept/retry/reassign.
        """
        with session() as s:
            run = s.get(Run, self.run_id)
            row = s.get(Task, f"{self.run_id}:{task_key}") if run else None
            if not run or not row or row.agent_id == (run.snapshot or {}).get("entry_agent_id"):
                return
            agent_id = row.agent_id
            title = row.title
            description = (row.spec or {}).get("description") or title
            prior_thread_id = (row.spec or {}).get("thread_id")
            if not isinstance(prior_thread_id, str):
                prior_thread_id = None
            try:
                connection, revision_id, model_id = _frozen_agent_provider(run, agent_id)
                policy = _frozen_agent_policy(run, agent_id)
                revision = s.get(ProviderRevision, revision_id)
                if not revision or not revision.encrypted_key:
                    raise ValueError(f"成员 {agent_id} 的供应商凭据版本不存在")
                secret = decrypt(revision.encrypted_key)
                provider_name = str(connection.get("name") or "Forge provider")
                workspace = run_root(self.run_id) / "tasks" / task_key
                source = run_root(self.run_id) / "integration"
                row.status = "running"
                row.started_at = row.started_at or now()
                row.spec = {**(row.spec or {}), "engine": "codex",
                            "runtime_home": str(run_root(self.run_id) / "codex-home" / task_key),
                            "workspace": str(workspace)}
                emit(s, self.run_id, "task.started", {"task_key": task_key,
                    "agent_id": agent_id, "engine": "codex"})
            except ValueError as exc:
                row.status = "needs_attention"
                row.result = {"outcome": "blocked", "error": str(exc)[:2200]}
                row.spec = {**(row.spec or {}), "last_error": str(exc)[:2200]}
                emit(s, self.run_id, "task.failed", {"task_key": task_key,
                    "agent_id": agent_id, "error": str(exc)[:1200], "engine": "codex"})
                return

        workspace = run_root(self.run_id) / "tasks" / task_key
        if not workspace.exists():
            copy_repo(run_root(self.run_id) / "integration", workspace)
        base_commit = ""
        try:
            base_commit = __import__("forge.workspace", fromlist=["git"]).git(
                workspace, "rev-parse", "HEAD").stdout.decode().strip()
        except Exception:
            pass
        with session() as s:
            row = s.get(Task, f"{self.run_id}:{task_key}")
            if row:
                row.base_commit = row.base_commit or base_commit
                row.spec = {**(row.spec or {}), "base_commit": row.base_commit}

        runtime_home = run_root(self.run_id) / "codex-home" / task_key
        try:
            member = CodexSingleTask(
                provider=CodexProvider(base_url=str(connection["base_url"]), model=model_id,
                                       api_key=secret, name=provider_name,
                                       gateway_token=gateway_token_for(connection["base_url"])),
                runtime_home=runtime_home, cwd=workspace,
                executable=settings.codex_binary, execution_policy=policy,
                team_mcp_context=_team_mcp_context(self.run_id, task_key),
            )
        except (CodexTaskError, RuntimeBridgeError, ValueError, KeyError) as exc:
            detail = str(exc)[:2200]
            with session() as s:
                row = s.get(Task, f"{self.run_id}:{task_key}")
                if row:
                    row.status = "needs_attention"
                    row.result = {"outcome": "blocked", "error": detail}
                emit(s, self.run_id, "task.failed", {"task_key": task_key,
                    "agent_id": agent_id, "error": detail, "engine": "codex"})
            return
        self.member_tasks[task_key] = member
        recorder = RuntimeEventRecorder(self.run_id)
        terminal = "failed"

        def record(event: RuntimeEvent):
            nonlocal terminal
            scoped = replace(event, task_id=f"{self.run_id}:{task_key}")
            recorder(scoped)
            with session() as s:
                row = s.get(Task, f"{self.run_id}:{task_key}")
                if not row:
                    return
                spec = dict(row.spec or {})
                if scoped.thread_id:
                    spec["thread_id"] = scoped.thread_id
                if scoped.turn_id:
                    spec["turn_id"] = scoped.turn_id
                row.spec = spec
                kind = scoped.kind.lower()
                if kind.endswith("turn/started") or kind.endswith("turn.started"):
                    row.status = "running"
                terminal = turn_terminal_status(scoped) or terminal
            record_request(self.run_id, task_key, scoped, self.approval_connection_id)

        try:
            prompt = ("你是团队成员，只处理当前分工，不要声称其他成员已完成。\n"
                      f"工作标题：{title}\n工作说明：{description}\n"
                      "请在当前工作副本中执行真实操作并输出证据；完成后调用 submit_result，"
                      "准确填写 outcome、summary、notes 和真实 deliverables。无法完成时提交 blocked，"
                      "不要只输出文字冒充完成；负责人会读取结果并决定验收、重试或转派。")
            memory = _memory_prompt(self.run_id, task_key)
            if memory:
                prompt += "\n\n" + memory
            from .engine import reserve_call
            reserve_call(self.run_id)
            async for _event in member.stream(prompt, event_sink=record,
                                               thread_id=prior_thread_id):
                pass
            # A structured `done` is an actual deliverable boundary. Reuse
            # Forge's existing deterministic integration path so the leader
            # can read it through read_work_result. Conflicts remain durable
            # work issues; waiting/blocked outcomes are never integrated.
            with session() as s:
                submitted_row = s.get(Task, f"{self.run_id}:{task_key}")
                outcome = (submitted_row.result or {}).get("outcome") if submitted_row else None
            if outcome == "done":
                from .engine import integrate_task
                integration = await asyncio.to_thread(
                    integrate_task, {"run_id": self.run_id, "key": task_key})
                if integration.get("integration_conflict"):
                    outcome = "conflicted"
            with session() as s:
                row = s.get(Task, f"{self.run_id}:{task_key}")
                if row:
                    submitted = isinstance(row.result, dict) and row.result.get("submission_digest")
                    if outcome == "done" and submitted and row.status != "conflicted":
                        row.status = "integrated"
                        emit(s, self.run_id, "task.integrated", {
                            "task_key": task_key, "agent_id": row.agent_id,
                            "engine": "codex"})
                    elif outcome == "waiting" and submitted:
                        row.status = "waiting"
                    elif outcome == "blocked" and submitted:
                        row.status = "failed"
                    elif not submitted:
                        row.status = "paused" if terminal == "interrupted" else "needs_attention"
                        row.result = {"outcome": "blocked",
                                      "summary": ("成员 Codex 回合已中断，可继续原 Thread。"
                                                  if terminal == "interrupted" else
                                                  "成员 Codex 回合已结束，尚未提交结构化成果；请由负责人读取输出后决定下一步。"),
                                      "terminal": terminal}
                        emit(s, self.run_id, "task.paused" if terminal == "interrupted" else "task.needs_attention", {
                            "task_key": task_key, "agent_id": row.agent_id,
                            "reason": "member_turn_interrupted" if terminal == "interrupted" else "member_turn_requires_leader_review",
                            "terminal": terminal})
        except asyncio.CancelledError:
            with session() as s:
                row = s.get(Task, f"{self.run_id}:{task_key}")
                if row:
                    row.status = "paused"
                    row.result = {"outcome": "blocked", "summary": "成员 Codex 回合已中断"}
            raise
        except (CodexTaskError, RuntimeBridgeError, ValueError) as exc:
            detail = str(exc)[:2200]
            with session() as s:
                row = s.get(Task, f"{self.run_id}:{task_key}")
                if row:
                    row.status = "needs_attention"
                    row.result = {"outcome": "blocked", "error": detail}
                emit(s, self.run_id, "task.failed", {"task_key": task_key,
                    "agent_id": agent_id, "error": detail, "engine": "codex"})
        finally:
            expire_requests(connection_id=self.approval_connection_id, run_id=self.run_id, task_key=task_key)
            _memory_prompt(self.run_id, task_key)
            self.member_tasks.pop(task_key, None)

    async def _sync_members(self) -> None:
        """Wake queued member activations without creating duplicate threads."""
        with session() as s:
            run = s.get(Run, self.run_id)
            if not run:
                return
            leader_id = (run.snapshot or {}).get("entry_agent_id")
            max_parallel = int((run.snapshot or {}).get("runtime", {}).get("max_parallel", 3) or 3)
            rows = list(s.scalars(select(Task).where(Task.run_id == self.run_id)))
            queued = [row for row in rows if row.key != "codex" and row.agent_id != leader_id
                      and row.kind == "autonomous" and row.status in {"pending", "queued"}]
            capacity = max(0, max_parallel - len(self.member_jobs))
            for row in queued[:capacity]:
                if row.key in self.member_jobs:
                    continue
                job = asyncio.create_task(self._run_member(row.key))
                self.member_jobs[row.key] = job
        for key, job in list(self.member_jobs.items()):
            if job.done():
                self.member_jobs.pop(key, None)

    async def _wait_members(self) -> None:
        if self.member_jobs:
            await asyncio.gather(*list(self.member_jobs.values()), return_exceptions=True)

    async def run(self, prompt: str | None = None) -> dict[str, str]:
        with session() as s:
            run = s.get(Run, self.run_id)
            if not run:
                raise ValueError("任务不存在")
            snapshot = run.snapshot or {}
            leader_id = snapshot.get("entry_agent_id") or snapshot.get("planner_id")
            try:
                connection, revision_id, model_id = _frozen_provider(run)
                policy = _frozen_policy(run)
            except ValueError as exc:
                _set_run_state(self.run_id, "needs_attention", error=str(exc))
                raise
            revision = s.get(ProviderRevision, revision_id)
            if not revision or not revision.encrypted_key:
                error = "冻结的供应商凭据不存在，请重新创建任务"
                _set_run_state(self.run_id, "needs_attention", error=error)
                raise ValueError(error)
            _ensure_codex_task(run, leader_id)
            task_row = s.get(Task, f"{self.run_id}:codex")
            prior_spec = dict(task_row.spec or {}) if task_row else {}
            prior_thread_id = prior_spec.get("thread_id")
            if prior_thread_id and not isinstance(prior_thread_id, str):
                prior_thread_id = None
            secret = decrypt(revision.encrypted_key)
            team_enabled = settings.codex_team_mcp_enabled
            request = prompt or (run.request if not prior_thread_id else
                "继续处理原任务。请先检查上一回合已经产生的文件、工具收据和失败原因，确认副作用后从中断位置继续。\n\n" + run.request)
            request = _leader_prompt(run, request, team_mcp=team_enabled)
            memory = _memory_prompt(self.run_id, "codex")
            if memory:
                request += "\n\n" + memory
            provider_name = str(connection.get("name") or "Forge provider")
            project_root = run_root(self.run_id) / "integration"
            source = settings.data_root / "projects" / run.project_id
            # The control plane owns the project snapshot.  Codex receives a
            # copy under this run only; it never receives the original path.
            if not project_root.exists():
                copy_repo(source, project_root)
            runtime_home = run_root(self.run_id) / "codex-home"
            _set_run_state(self.run_id, "running")
            with session() as db:
                emit(db, self.run_id, "runtime.start.requested", {
                    "provider": provider_name, "model": model_id,
                    "protocol": connection.get("protocol"), "engine": "codex",
                })
        try:
            from .engine import reserve_call
            reserve_call(self.run_id)
            self.task = CodexSingleTask(
                provider=CodexProvider(base_url=str(connection["base_url"]), model=model_id,
                                       api_key=secret, name=provider_name,
                                       gateway_token=gateway_token_for(connection["base_url"])),
                runtime_home=runtime_home, cwd=project_root,
                executable=settings.codex_binary, execution_policy=policy,
                team_mcp_context=_team_mcp_context(self.run_id, 'codex'),
            )
            recorder = RuntimeEventRecorder(self.run_id)
            terminal = "failed"

            def record(event: RuntimeEvent):
                recorder(replace(event, task_id=f'{self.run_id}:codex'))
                _update_codex_task(self.run_id, thread_id=event.thread_id,
                                   turn_id=event.turn_id)
                kind = event.kind.lower()
                if kind.endswith("turn/started") or kind.endswith("turn.started"):
                    _set_run_state(self.run_id, "running")
                    _update_codex_task(self.run_id, status="running")
                record_request(self.run_id, 'codex', event, self.approval_connection_id)

            async for event in self.task.stream(request, event_sink=record,
                                                thread_id=prior_thread_id):
                # MCP assignments are committed while the leader turn is
                # streaming. Wake member Codex sessions as soon as they appear
                # so the UI shows parallel work instead of a static queue.
                if team_enabled:
                    await self._sync_members()
                terminal = turn_terminal_status(event) or terminal
            if team_enabled:
                await self._sync_members()
                await self._wait_members()
                with session() as s:
                    member_rows = list(s.scalars(select(Task).where(
                        Task.run_id == self.run_id, Task.key != "codex")))
                unresolved_members = [row for row in member_rows
                                      if row.status not in {"integrated", "superseded"}]
                if unresolved_members:
                    # A member turn ending is not a verified delivery. Keep the
                    # run recoverable and let the leader/human inspect evidence.
                    terminal = "member_review"
                else:
                    with session() as s:
                        leader_task = s.get(Task, f"{self.run_id}:codex")
                        leader_outcome = (leader_task.result or {}).get("outcome") if leader_task else None
                        leader_submitted = bool(leader_task and isinstance(leader_task.result, dict) and (
                            leader_task.result.get("submission_digest") or
                            leader_task.result.get("outcome") in {"done", "ready"}))
                    if leader_outcome == "blocked":
                        terminal = "failed"
                    elif leader_outcome == "waiting":
                        terminal = "leader_waiting"
                    elif terminal == "completed" and not leader_submitted:
                        terminal = "leader_review"
            mapped = {"completed": "completed", "interrupted": "paused",
                      "failed": "needs_attention", "member_review": "needs_attention",
                      "leader_review": "needs_attention", "leader_waiting": "paused"}.get(
                          terminal, "needs_attention")
            _set_run_state(self.run_id, mapped,
                           error="" if mapped == "completed" else (
                               "成员 Codex 回合已结束，等待负责人检查成果" if terminal == "member_review"
                               else f"Codex 运行终态：{terminal}"))
            with session() as s:
                task = s.get(Task, f"{self.run_id}:codex")
                if task:
                    task.status = "done" if mapped == "completed" else mapped
            return {"status": mapped}
        except asyncio.CancelledError:
            for job in self.member_jobs.values():
                job.cancel()
            if self.member_jobs:
                await asyncio.gather(*self.member_jobs.values(), return_exceptions=True)
            terminal_state = "cancelled" if self.cancel_requested else "paused"
            message = "Codex 运行已取消" if self.cancel_requested else "Codex 运行被本地调度器中断，可继续原任务"
            _set_run_state(self.run_id, terminal_state, error=message)
            with session() as s:
                task = s.get(Task, f"{self.run_id}:codex")
                if task:
                    task.status = terminal_state
            raise
        except (CodexTaskError, RuntimeBridgeError, ValueError) as exc:
            detail = str(exc)[:2200]
            _set_run_state(self.run_id, "needs_attention", error=detail)
            with session() as s:
                task = s.get(Task, f"{self.run_id}:codex")
                if task:
                    task.status = "needs_attention"
                emit(s, self.run_id, "runtime.failed", {"error": detail, "engine": "codex"})
            return {"status": "needs_attention"}
        finally:
            expire_requests(connection_id=self.approval_connection_id, run_id=self.run_id)
            _memory_prompt(self.run_id, "codex")
            secret = ""


async def run_codex_run(run_id: str) -> dict[str, str]:
    """Convenience entry used by the local scheduler and tests."""
    return await CodexRunExecutor(run_id).run()
