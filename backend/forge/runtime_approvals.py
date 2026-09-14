"""Durable, one-shot transport for task-scoped Codex approval requests.

HTTP records a human decision; only the live executor may send it back to the
exact JSON-RPC callback. A pipe write is a delivery receipt, not execution
success. Lost connections/uncertain writes are never silently retried.
"""
from copy import deepcopy
import hashlib
import json

from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select, update

from .db import Event, Outbox, Run, RuntimeApproval, Task, emit, now, session
from .runtime_bridge import RuntimeBridgeError

KINDS = frozenset({
    'item/commandExecution/requestApproval',
    'item/fileChange/requestApproval',
    'item/permissions/requestApproval',
})
UNRESOLVED = ('pending', 'queued', 'dispatching')
LIVE = ('running', 'awaiting_approval')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def approval_out(row):
    # connection_id and raw JSON-RPC request ID are transport-only, not UI actions.
    return {key: deepcopy(getattr(row, key)) for key in (
        'id', 'run_id', 'task_key', 'agent_id', 'kind', 'thread_id', 'turn_id',
        'item_id', 'status', 'digest', 'payload', 'decision', 'error', 'created_at')}


def record_request(run_id, task_key, event, connection_id):
    if event.kind not in KINDS or event.request_id is None:
        return False
    if (type(event.request_id) not in (int, str)
            or (isinstance(event.request_id, str) and not event.request_id.strip())
            or not all(
            isinstance(value, str) and value for value in (event.thread_id, event.turn_id, event.item_id))):
        raise RuntimeBridgeError('Codex 审批缺少明确的回合或操作标识，不能授予权限')
    ident = _digest([run_id, task_key, connection_id, event.request_id])
    with session() as db:
        run, task = db.get(Run, run_id), db.get(Task, f'{run_id}:{task_key}')
        if not run or not task or run.status not in LIVE:
            raise RuntimeBridgeError('审批不属于正在执行的任务')
        existing = db.get(RuntimeApproval, ident)
        if existing:
            # A JSON-RPC request id is only idempotent for the same immutable
            # request.  Never let a reused id silently replace or hide a
            # different operation on the same connection.
            if existing.thread_id != event.thread_id or existing.turn_id != event.turn_id \
                    or existing.item_id != event.item_id or existing.kind != event.kind:
                raise RuntimeBridgeError('Codex 审批请求 ID 被重复使用，拒绝混淆不同操作')
            return True
        payload = deepcopy(event.payload)
        # A file approval only carries an item ID; show the proposed patch from
        # that exact item's preceding event instead of an unexplained grant root.
        if event.kind == 'item/fileChange/requestApproval':
            rows = db.scalars(select(Event).where(Event.run_id == run_id,
                Event.kind.in_(['runtime.item.started', 'runtime.item.updated']),
                Event.data['item_id'].as_string() == event.item_id,
                Event.data['thread_id'].as_string() == event.thread_id,
                Event.data['turn_id'].as_string() == event.turn_id).order_by(Event.id.desc()))
            for source in rows:
                if source.data.get('task_id') not in (f'{run_id}:{task_key}', None):
                    continue
                item = source.data.get('payload', {}).get('item')
                if isinstance(item, dict) and item.get('type') == 'fileChange':
                    payload['item'] = deepcopy(item)
                    break
        digest = _digest([run_id, task_key, connection_id, event.request_id,
                          event.thread_id, event.turn_id, event.item_id, event.kind, payload])
        row = RuntimeApproval(id=ident, run_id=run_id, task_key=task_key,
            agent_id=task.agent_id, connection_id=connection_id, request_id={'value': event.request_id},
            thread_id=event.thread_id, turn_id=event.turn_id, item_id=event.item_id,
            kind=event.kind, payload=payload, digest=digest)
        db.add(row)
        task.status = run.status = 'awaiting_approval'
        emit(db, run_id, 'runtime.approval.requested', {
            'approval_id': ident, 'task_key': task_key, 'agent_id': task.agent_id,
            'thread_id': event.thread_id, 'turn_id': event.turn_id, 'item_id': event.item_id})
    return True


def response_for(row, approved):
    """Map only once/turn decisions, never session grants or persistent rules."""
    if row.kind not in KINDS:
        raise ValueError('不支持的 Codex 审批类型')
    if row.kind == 'item/permissions/requestApproval':
        requested = row.payload.get('permissions')
        if not isinstance(requested, dict) or set(requested) - {'network', 'fileSystem'}:
            if approved:
                raise ValueError('无法安全识别请求的权限范围，请拒绝并检查运行时协议')
            requested = {}
        return {'permissions': deepcopy(requested) if approved else {}, 'scope': 'turn'}
    if approved and row.kind == 'item/fileChange/requestApproval':
        if not (row.payload.get('item') or {}).get('changes'):
            raise ValueError('文件变更内容尚未完整记录，不能批准；请拒绝后重新提出明确变更')
    available = row.payload.get('availableDecisions')
    if approved and isinstance(available, list) and 'accept' not in available:
        raise ValueError('该申请不支持仅批准本次操作，不能静默授予会话权限')
    return {'decision': 'accept' if approved else 'decline'}


def _resume_state(db, run_id, task_key):
    pending = list(db.scalars(select(RuntimeApproval).where(
        RuntimeApproval.run_id == run_id, RuntimeApproval.status.in_(UNRESOLVED))))
    task, run = db.get(Task, f'{run_id}:{task_key}'), db.get(Run, run_id)
    if task and task.status == 'awaiting_approval' and not any(p.task_key == task_key for p in pending):
        task.status = 'running'
    if run and run.status == 'awaiting_approval' and not pending:
        run.status, run.updated_at = 'running', now()


def expire_requests(*, connection_id=None, run_id=None, task_key=None):
    """Called after connection teardown, or once after acquiring the runner lock."""
    with session() as db:
        statement = select(RuntimeApproval).where(RuntimeApproval.status.in_(UNRESOLVED))
        if connection_id is not None:
            statement = statement.where(RuntimeApproval.connection_id == connection_id)
        if run_id is not None:
            statement = statement.where(RuntimeApproval.run_id == run_id)
        if task_key is not None:
            statement = statement.where(RuntimeApproval.task_key == task_key)
        affected: set[tuple[str, str]] = set()
        for row in db.scalars(statement):
            row.status = 'delivery_unknown' if row.status == 'dispatching' else 'expired'
            row.error = '原运行连接已结束；不会在新回合重放这次审批'
            row.updated_at = now()
            affected.add((row.run_id, row.task_key))
            emit(db, row.run_id, 'runtime.approval.expired', {
                'approval_id': row.id, 'task_key': row.task_key, 'status': row.status})
        # A dead callback must not leave the business task in an infinite
        # ``awaiting_approval`` state.  Preserve a live sibling request, but
        # pause the affected task/run when no unresolved request remains so a
        # user can explicitly continue the original task.
        for affected_run, affected_task in affected:
            unresolved_task = db.scalar(select(RuntimeApproval.id).where(
                RuntimeApproval.run_id == affected_run,
                RuntimeApproval.task_key == affected_task,
                RuntimeApproval.status.in_(UNRESOLVED)))
            task = db.get(Task, f'{affected_run}:{affected_task}')
            if task and task.status == 'awaiting_approval' and not unresolved_task:
                task.status = 'paused'
            unresolved_run = db.scalar(select(RuntimeApproval.id).where(
                RuntimeApproval.run_id == affected_run,
                RuntimeApproval.status.in_(UNRESOLVED)))
            run = db.get(Run, affected_run)
            if run and run.status == 'awaiting_approval' and not unresolved_run:
                run.status, run.error, run.updated_at = 'paused', '审批连接已结束，请继续原任务后重新申请', now()


async def deliver_decision(run_id, approval_id, connection_id, runtime):
    client = getattr(runtime, 'client', None)
    with session() as db:
        row = db.get(RuntimeApproval, approval_id)
        if not row or row.run_id != run_id or row.status != 'queued':
            return
        run, task = db.get(Run, run_id), db.get(Task, f'{run_id}:{row.task_key}')
        valid = (row.connection_id == connection_id and client is not None
                 and run is not None and run.status in LIVE and task is not None and task.status in LIVE
                 and getattr(runtime, 'thread_id', None) == row.thread_id
                 and getattr(runtime, 'turn_id', None) == row.turn_id
                 and client.has_server_request(row.request_id['value']))
        if not valid:
            row.status, row.error = 'expired', '原审批回调已失效；未发送任何授权'
            emit(db, run_id, 'runtime.approval.expired', {'approval_id': row.id, 'task_key': row.task_key})
            return
        result = response_for(row, row.decision['approved'])
        request_id, task_key = row.request_id['value'], row.task_key
        claimed = db.execute(update(RuntimeApproval).where(RuntimeApproval.id == row.id,
            RuntimeApproval.status == 'queued').values(status='dispatching', updated_at=now()))
        if claimed.rowcount != 1:
            return
    try:
        await client.respond(request_id, result)
    except BaseException:
        # Once a pipe write starts, delivery is uncertain. Never replay it.
        with session() as db:
            row = db.get(RuntimeApproval, approval_id)
            row.status, row.error = 'delivery_unknown', '审批发送时连接中断，送达结果未确认；请检查原任务后继续'
            emit(db, run_id, 'runtime.approval.delivery_unknown', {'approval_id': row.id, 'task_key': task_key})
        raise
    with session() as db:
        row = db.get(RuntimeApproval, approval_id)
        row.status, row.updated_at = 'delivered', now()
        db.flush()
        _resume_state(db, run_id, task_key)
        emit(db, run_id, 'runtime.approval.delivered', {
            'approval_id': row.id, 'task_key': task_key, 'agent_id': row.agent_id,
            'approved': row.decision['approved'], 'execution_completed': False})


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    approved: StrictBool
    comment: str = Field('', max_length=4000)


def register(app):
    from .api import current_user, developer, run_access

    @app.get('/api/runs/{run_id}/runtime-approvals')
    def list_approvals(run_id: str, user=Depends(current_user),
                       limit: int = Query(100, ge=1, le=500)):
        with session() as db:
            run_access(db, run_id, user)
            # Pending records first; large history never hides an actionable request.
            rows = db.scalars(select(RuntimeApproval).where(RuntimeApproval.run_id == run_id)
                .order_by(RuntimeApproval.status.in_(UNRESOLVED).desc(), RuntimeApproval.created_at.desc()).limit(limit))
            return {'items': [approval_out(row) for row in rows]}

    @app.post('/api/runs/{run_id}/runtime-approvals/{approval_id}/decision', status_code=202)
    def decide(run_id: str, approval_id: str, data: ApprovalDecision, user=Depends(developer)):
        with session() as db:
            run = run_access(db, run_id, user)
            row = db.get(RuntimeApproval, approval_id)
            if not row or row.run_id != run_id:
                raise HTTPException(404, '本任务中没有该审批申请')
            if row.digest != data.digest:
                raise HTTPException(409, '审批内容已变化，请刷新后核对')
            if row.status != 'pending':
                if row.status in ('queued', 'dispatching', 'delivered') and row.decision.get('approved') == data.approved:
                    return {'accepted': True, 'status': row.status, 'approval_id': row.id}
                raise HTTPException(409, '审批已处理或失效，不会重复发送')
            if run.status not in LIVE:
                raise HTTPException(409, '任务已停止，请继续原任务后处理新的审批申请')
            stop = db.scalar(select(Outbox.id).where(Outbox.run_id == run_id,
                Outbox.action == 'control', Outbox.sent == False,
                Outbox.payload['action'].as_string().in_(['pause', 'cancel'])))
            if stop:
                raise HTTPException(409, '任务正在停止，不能发送新的授权')
            try:
                response_for(row, data.approved)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            decision = {**data.model_dump(exclude={'digest'}), 'actor': user.id, 'at': now()}
            changed = db.execute(update(RuntimeApproval).where(RuntimeApproval.id == row.id,
                RuntimeApproval.status == 'pending', RuntimeApproval.digest == data.digest)
                .values(status='queued', decision=decision, updated_at=now()))
            if changed.rowcount != 1:
                raise HTTPException(409, '审批已被处理，请刷新查看结果')
            command = Outbox(run_id=run_id, action='runtime_approval', payload={'approval_id': row.id})
            db.add(command)
            emit(db, run_id, 'runtime.approval.decision_queued', {
                'approval_id': row.id, 'task_key': row.task_key,
                'actor': user.id, 'approved': data.approved})
            return {'accepted': True, 'status': 'queued', 'approval_id': row.id,
                    'effect': '决定已排队，待送回原 Codex 回合；尚不代表工具已执行'}
