"""Feature-flagged single-task Codex POC API."""

import json

from fastapi import Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import Field

from .codex_gateway import gateway_token_for
from .codex_task import CodexProvider, CodexSingleTask, CodexTaskError
from .runtime_bridge import RuntimeBridgeError, turn_terminal_status
from .config import settings
from .db import ModelProfile, Provider, ProviderRevision, Run, session, uid
from .schema import Strict
from .security import decrypt
from .runtime_events import RuntimeEventRecorder
from .codex_permissions import codex_execution_policy


class CodexPocInput(Strict):
    provider_id: str = Field(min_length=1, max_length=32)
    model_profile_id: str = Field(min_length=1, max_length=32)
    prompt: str = Field(min_length=1, max_length=100_000)
    # Optional bridge into an already-created Forge run.  The POC remains
    # transient when omitted, while callers that own a run get durable events
    # through the normal Event/SSE projection.
    run_id: str | None = Field(default=None, min_length=1, max_length=32)


class CodexRunStartInput(Strict):
    prompt: str | None = Field(default=None, min_length=1, max_length=100_000)


def _run_provider_snapshot(run: Run) -> tuple[dict, str, str]:
    """Resolve the frozen leader model without accepting client credentials."""
    snapshot = run.snapshot or {}
    leader_id = snapshot.get('entry_agent_id') or snapshot.get('planner_id')
    agents = snapshot.get('agents') if isinstance(snapshot.get('agents'), dict) else {}
    leader = agents.get(leader_id) if leader_id else None
    models = leader.get('models') if isinstance(leader, dict) else None
    model = models[0] if isinstance(models, list) and models else None
    connection = model.get('connection') if isinstance(model, dict) else None
    revision_id = model.get('credential_revision') if isinstance(model, dict) else None
    model_id = (model.get('model') or {}).get('model_id') if isinstance(model, dict) and isinstance(model.get('model'), dict) else None
    if not isinstance(connection, dict) or connection.get('protocol') != 'openai_responses':
        raise ValueError('负责人主模型协议不是 OpenAI Responses，暂不能接入 Codex 运行时')
    if not isinstance(revision_id, str) or not revision_id:
        raise ValueError('负责人主模型缺少供应商凭据版本')
    if not isinstance(model_id, str) or not model_id:
        raise ValueError('负责人主模型缺少上游模型 ID')
    return connection, revision_id, model_id


def _run_leader_execution_policy(run: Run) -> dict[str, str]:
    snapshot = run.snapshot or {}
    leader_id = snapshot.get('entry_agent_id') or snapshot.get('planner_id')
    agents = snapshot.get('agents') if isinstance(snapshot.get('agents'), dict) else {}
    leader = agents.get(leader_id) if leader_id else None
    frozen = leader.get('execution_policy') if isinstance(leader, dict) else None
    profile = frozen.get('profile') if isinstance(frozen, dict) else None
    if not isinstance(profile, str):
        project = snapshot.get('project') if isinstance(snapshot.get('project'), dict) else {}
        profile = project.get('permission_profile', 'ask')
    return codex_execution_policy(profile)


def register(app):
    from .api import developer, run_access

    @app.post('/api/runtime/poc-task')
    async def run_poc_task(data: CodexPocInput, user=Depends(developer)):
        if not settings.codex_poc_enabled:
            raise HTTPException(404, 'Codex POC 尚未开启')
        with session() as s:
            if data.run_id:
                from .api import run_access
                run_access(s, data.run_id, user)
            provider = s.get(Provider, data.provider_id)
            profile = s.get(ModelProfile, data.model_profile_id)
            if not provider or not provider.enabled:
                raise HTTPException(404, '供应商不存在、已停用或未保存')
            if not profile or profile.config.get('provider_id') != provider.id:
                raise HTTPException(404, '模型不存在，或不属于所选供应商')
            revision = s.get(ProviderRevision, provider.revision_id)
            if not revision or not revision.encrypted_key:
                raise HTTPException(409, '供应商尚未保存可用密钥')
            connection = dict(revision.config)
            if connection.get('protocol') != 'openai_responses':
                raise HTTPException(409, 'Codex POC 当前只支持 OpenAI Responses 供应商协议')
            secret = decrypt(revision.encrypted_key)
            model_name = profile.config.get('model_id')
            if not isinstance(model_name, str) or not model_name:
                raise HTTPException(409, '模型配置缺少上游模型 ID')
            provider_name = provider.name
            try:
                gateway_token = gateway_token_for(connection["base_url"])
            except CodexTaskError as exc:
                raise HTTPException(409, str(exc)) from exc

        run_root = settings.runtime_data_root / 'poc' / uid()
        task = CodexSingleTask(
            provider=CodexProvider(base_url=connection['base_url'], model=model_name,
                                   api_key=secret, name=provider_name,
                                   gateway_token=gateway_token),
            runtime_home=run_root / 'runtime-home', cwd=run_root / 'workspace',
        )
        secret = ''
        event_sink = RuntimeEventRecorder(data.run_id) if data.run_id else None

        async def body():
            terminal = 'failed'
            try:
                async for event in task.stream(data.prompt, event_sink=event_sink):
                    yield json.dumps({'type': 'event', **event.as_dict()}, ensure_ascii=False) + '\n'
                    normalized = turn_terminal_status(event)
                    if normalized:
                        terminal = normalized
            except (CodexTaskError, RuntimeBridgeError) as exc:
                terminal = 'failed'
                yield json.dumps({'type': 'error', 'error': str(exc)[:1200]}, ensure_ascii=False) + '\n'
            finally:
                yield json.dumps({'type': 'result', 'status': terminal}, ensure_ascii=False) + '\n'

        return StreamingResponse(body(), media_type='application/x-ndjson', headers={'X-Accel-Buffering': 'no'})

    @app.post('/api/runs/{run_id}/codex-start')
    async def start_codex_run(run_id: str, data: CodexRunStartInput, user=Depends(developer)):
        if not settings.codex_runtime_enabled:
            raise HTTPException(404, 'Codex 任务运行入口尚未开启')
        with session() as s:
            run = run_access(s, run_id, user)
            if run.status in {'completed', 'cancelled'}:
                raise HTTPException(409, '已完成或已取消的任务不能重新启动')
            try:
                connection, revision_id, model_id = _run_provider_snapshot(run)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            revision = s.get(ProviderRevision, revision_id)
            if not revision or not revision.encrypted_key:
                raise HTTPException(409, '冻结的供应商凭据不存在，请重新创建任务')
            secret = decrypt(revision.encrypted_key)
            try:
                gateway_token = gateway_token_for(connection["base_url"])
            except CodexTaskError as exc:
                raise HTTPException(409, str(exc)) from exc
            run.status = 'running'
            run.error = ''
            prompt = data.prompt or run.request

        runtime_root = settings.runtime_data_root / 'runs' / run_id
        task = CodexSingleTask(
            provider=CodexProvider(
                base_url=connection['base_url'], model=model_id, api_key=secret,
                name=str(connection.get('name') or 'Forge provider'),
                gateway_token=gateway_token,
            ),
            runtime_home=runtime_root / 'runtime-home', cwd=runtime_root / 'workspace',
            execution_policy=_run_leader_execution_policy(run),
        )
        secret = ''
        event_sink = RuntimeEventRecorder(run_id)

        async def body():
            terminal = 'failed'
            try:
                async for event in task.stream(prompt, event_sink=event_sink):
                    yield json.dumps({'type': 'event', **event.as_dict()}, ensure_ascii=False) + '\n'
                    normalized = turn_terminal_status(event)
                    if normalized:
                        terminal = normalized
            except (CodexTaskError, RuntimeBridgeError) as exc:
                terminal = 'needs_attention'
                yield json.dumps({'type': 'error', 'error': str(exc)[:1200]}, ensure_ascii=False) + '\n'
            finally:
                final_status = {'completed': 'completed', 'interrupted': 'paused', 'failed': 'needs_attention'}.get(terminal, 'needs_attention')
                with session() as s:
                    current = s.get(Run, run_id)
                    if current:
                        current.status = final_status
                        current.error = '' if final_status == 'completed' else f'Codex 运行终态：{terminal}'
                yield json.dumps({'type': 'result', 'status': terminal, 'run_id': run_id}, ensure_ascii=False) + '\n'

        return StreamingResponse(body(), media_type='application/x-ndjson', headers={'X-Accel-Buffering': 'no'})
