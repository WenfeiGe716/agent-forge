"""Exact-command, one-operation permissions. Approval cannot change frozen policy."""
import hashlib
from types import SimpleNamespace

from fastapi import Depends, HTTPException
from pydantic import Field
from sqlalchemy import JSON, Boolean, String, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, Run, Task, DecisionRequest, session, emit, now
from .schema import Strict, HumanAnswer
from . import engagement, local_executor
from .member_execution_policy import execution_project

CONSTRAINTS = {'policy_version': 1, 'child_processes': False, 'path_scope': 'exact_files', 'network_scope': 'outbound_ip_only',
    'message': '本次临时权限禁止派生子进程；需子进程的操作会失败关闭。不会改变项目权限或自动开启完全访问。'}


class ExecutionPermission(Base):
    __tablename__ = 'execution_permissions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    task_key: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str] = mapped_column(String(32))
    decision_id: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    command: Mapped[str] = mapped_column(String(4000))
    network: Mapped[bool] = mapped_column(Boolean, default=False)
    paths: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(24), default='pending')
    reviewed_by: Mapped[str] = mapped_column(String(80), default='')
    review_kind: Mapped[str] = mapped_column(String(20), default='')
    review_comment: Mapped[str] = mapped_column(String(2000), default='')
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class PermissionRequest(Strict):
    command: str = Field(min_length=1, max_length=4000)
    network: bool = Field(False, strict=True)
    paths: list[dict] = Field(default_factory=list, max_length=12)
    reason: str = Field(min_length=3, max_length=2000)


class PermissionReview(Strict):
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    approved: bool
    comment: str = Field('', max_length=2000)


class PermissionReference(Strict):
    permission_id: str = Field(pattern=r'^[a-f0-9]{64}$')


def output(row):
    result = {key: getattr(row, key) for key in ('id', 'run_id', 'task_key', 'agent_id', 'decision_id', 'digest', 'command',
         'network', 'paths', 'reason', 'status', 'reviewed_by', 'review_kind', 'review_comment', 'result', 'created_at', 'updated_at')}
    from .workspace import task_root
    result['cwd'] = str(task_root(row.run_id, row.task_key))
    result['scope'] = 'one exact command in one run/task; no permanent policy change'
    result['constraints'] = CONSTRAINTS
    return result


def authorize(s, run_id, key):
    run, task = s.get(Run, run_id), s.get(Task, run_id + ':' + key)
    if not run or not task or run.status in ('completed', 'cancelled', 'needs_attention'):
        raise ValueError('运行已结束或当前执行任务不存在')
    if run.snapshot['project'].get('execution_mode', 'container') != 'local': raise ValueError('此申请仅用于本机执行，不改变容器权限')
    if execution_project(run, task.agent_id)['permission_profile'] == 'full': raise ValueError('已明确选择完全访问的成员不使用临时权限；不得宣称单次隔离许可')
    agent = run.snapshot['agents'].get(task.agent_id)
    if not agent or 'run_command' not in agent['tools'] or task.kind not in ('develop', 'autonomous'): raise ValueError('成员没有该任务的命令执行权限')
    return run, task


def request_out(row, decision):
    return {**engagement.decision_out(decision), 'permission_id': row.id, 'permission_status': row.status,
            'permission_digest': row.digest, 'command': row.command, 'network': row.network, 'paths': row.paths,
            'review_url': '/api/execution/permissions/' + row.id + '/review'}


def apply_review(s, row, *, approved, actor, comment, kind='human'):
    if row.status != 'pending': raise ValueError('权限申请已审查；不能覆盖已有决定')
    run, task = authorize(s, row.run_id, row.task_key)
    local_executor.canonical_grants(row.paths)
    decision = s.get(DecisionRequest, row.decision_id)
    engagement.answer_request(s, run, decision.id,
        HumanAnswer(digest=decision.digest, choice_id='approve' if approved else 'reject', reject=not approved), actor)
    row.status, row.reviewed_by, row.review_kind = 'approved' if approved else 'rejected', actor.id, kind
    row.review_comment, row.updated_at = comment[:2000], now()
    emit(s, row.run_id, 'execution.permission.reviewed', {'permission_id': row.id, 'task_key': row.task_key,
         'agent_id': row.agent_id, 'digest': row.digest, 'approved': approved, 'actor': actor.id, 'review_kind': kind, 'comment': row.review_comment})


async def request_permission(run_id, key, operation, arguments):
    data = PermissionRequest.model_validate(arguments)
    paths = local_executor.canonical_grants(data.paths)
    if not data.command.strip() or '\x00' in data.command: raise ValueError('命令内容无效')
    request_id = hashlib.sha256((run_id + ':permission:' + operation).encode()).hexdigest()
    decision_id = hashlib.sha256((run_id + ':decision:' + operation).encode()).hexdigest()
    with session() as s:
        s.execute(update(Run).where(Run.id == run_id).values(updated_at=Run.updated_at))
        run, task = authorize(s, run_id, key)
        digest = engagement.digest({'run_id': run_id, 'task_key': key, 'agent_id': task.agent_id,
                                    'command': data.command, 'network': data.network, 'paths': paths, 'reason': data.reason,
                                    'runtime': run.snapshot['agents'][task.agent_id]['runtime'], 'constraints': CONSTRAINTS})
        row = s.get(ExecutionPermission, request_id)
        if row:
            if row.digest != digest: raise ValueError('同一操作不能更换命令或申请权限')
            return request_out(row, s.get(DecisionRequest, row.decision_id))
        if s.scalar(select(func.count()).select_from(DecisionRequest).where(DecisionRequest.run_id == run_id)) >= 100:
            raise ValueError('本次运行的人工申请已达上限')
        question = {'question': '请审查一次本机命令权限',
                    'context': '命令：' + data.command + '\n联网：' + ('允许出站 IP 网络' if data.network else '禁止') + '\n精确路径：' + str(paths) + '\n理由：' + data.reason + '\n' + CONSTRAINTS['message'] + '\n只对本次命令有效。请在执行权限入口审查；普通回答不授予权限。',
                    'options': [{'id': 'approve', 'label': '批准这一次精确命令'}, {'id': 'reject', 'label': '拒绝'}], 'allow_text': False}
        decision = DecisionRequest(id=decision_id, run_id=run_id, task_key=key, agent_id=task.agent_id,
            digest=engagement.digest({'run_id': run_id, 'task_key': key, 'question': question}), question=question)
        row = ExecutionPermission(id=request_id, run_id=run_id, task_key=key, agent_id=task.agent_id,
            decision_id=decision_id, digest=digest, command=data.command, network=data.network, paths=paths, reason=data.reason)
        s.add_all([row, decision]); s.flush()
        emit(s, run_id, 'execution.permission.requested', {'permission_id': row.id, 'task_key': key, 'agent_id': task.agent_id,
             'digest': digest, 'command': data.command, 'network': data.network, 'paths': paths, 'request_id': decision.id})
        emit(s, run_id, 'human.decision.requested', {'request_id': decision.id, 'task_key': key, 'agent_id': task.agent_id, 'digest': decision.digest, **question})
        auto = execution_project(run, task.agent_id)['permission_profile'] == 'auto'
        agent_id = task.agent_id
    if auto:
        from .execution_auto_review import assess_request
        normalized = {'command': data.command, 'network': data.network, 'reason': data.reason,
                      'read_paths': [p['path'] for p in paths if p['access'] == 'read'],
                      'write_paths': [p['path'] for p in paths if p['access'] == 'write']}
        try:
            answer = await assess_request(run_id, key, agent_id, digest, normalized)
        except Exception:
            answer = {'decision': 'ask_user', 'reason': '自动审批不可用，请人工审查'}
        with session() as s:
            s.execute(update(ExecutionPermission).where(ExecutionPermission.id == request_id).values(updated_at=ExecutionPermission.updated_at))
            row = s.get(ExecutionPermission, request_id)
            if row.status == 'pending' and row.digest == digest and answer.get('decision') == 'approve' and answer.get('digest') == digest:
                apply_review(s, row, approved=True, actor=SimpleNamespace(id='auto:' + str(answer['reviewer_id'])),
                             comment=answer.get('reason', '独立审查通过'), kind='auto')
            elif row.status == 'pending':
                row.review_comment = str(answer.get('reason', '需要人工审查'))[:2000]
    with session() as s:
        row = s.get(ExecutionPermission, request_id)
        return request_out(row, s.get(DecisionRequest, row.decision_id))


def validate_grant(s, permission_id, run_id, key, command, paths, network):
    row = s.get(ExecutionPermission, permission_id)
    run, task = authorize(s, run_id, key)
    if (not row or row.run_id != run_id or row.task_key != key or row.agent_id != task.agent_id or not row.reviewed_by
        or row.status not in ('running', 'completed') or row.command != command or row.paths != paths or row.network != network):
        raise ValueError('精确审批许可与执行命令、路径、网络或成员不匹配')
    expected = engagement.digest({'run_id': run_id, 'task_key': key, 'agent_id': task.agent_id, 'command': row.command,
        'network': row.network, 'paths': row.paths, 'reason': row.reason, 'runtime': run.snapshot['agents'][task.agent_id]['runtime'], 'constraints': CONSTRAINTS})
    if row.digest != expected: raise ValueError('审批范围摘要不匹配')


async def invoke(name, arguments, run_id, key, operation, runtime):
    if name == 'request_execution_permission': return await request_permission(run_id, key, operation, arguments)
    ref = PermissionReference.model_validate(arguments)
    with session() as s:
        s.execute(update(ExecutionPermission).where(ExecutionPermission.id == ref.permission_id).values(updated_at=ExecutionPermission.updated_at))
        run, task = authorize(s, run_id, key)
        row = s.get(ExecutionPermission, ref.permission_id)
        if not row or row.run_id != run_id or row.task_key != key or row.agent_id != task.agent_id: raise ValueError('许可不属于当前任务成员')
        if row.status == 'completed': return {'permission_id': row.id, **row.result}
        if row.status != 'approved': raise ValueError('命令尚未批准或先前结果不确定；不会自动重放')
        row.status, row.updated_at = 'running', now()
        command, paths, network = row.command, row.paths, row.network
        frozen_runtime = run.snapshot['agents'][task.agent_id]['runtime']
    try:
        result = await local_executor.execute(run_id, key, 'approved:' + ref.permission_id, command, frozen_runtime,
                                              paths=paths, network=network, permission_id=ref.permission_id)
    except BaseException:
        with session() as s:
            row = s.get(ExecutionPermission, ref.permission_id); row.status, row.updated_at = 'uncertain', now()
        raise
    with session() as s:
        row = s.get(ExecutionPermission, ref.permission_id); row.status, row.result, row.updated_at = 'completed', result, now()
        emit(s, run_id, 'execution.permission.executed', {'permission_id': row.id, 'task_key': key, 'agent_id': task.agent_id, 'digest': row.digest, 'result': result})
    return {'permission_id': ref.permission_id, **result}


def register(app):
    from .api import admin, current_user, run_access

    @app.get('/api/execution/permissions')
    def permissions(run_id: str = '', user=Depends(current_user)):
        with session() as s:
            if run_id: run_access(s, run_id, user)
            elif user.role != 'admin': raise HTTPException(403, '请指定有权限的运行')
            query = select(ExecutionPermission).order_by(ExecutionPermission.created_at.desc())
            if run_id: query = query.where(ExecutionPermission.run_id == run_id)
            return {'items': [output(row) for row in s.scalars(query.limit(100))]}

    @app.post('/api/execution/permissions/{permission_id}/review')
    def review(permission_id: str, data: PermissionReview, user=Depends(admin)):
        with session() as s:
            s.execute(update(ExecutionPermission).where(ExecutionPermission.id == permission_id).values(updated_at=ExecutionPermission.updated_at))
            row = s.get(ExecutionPermission, permission_id)
            if not row: raise HTTPException(404, '权限申请不存在')
            run_access(s, row.run_id, user)
            if row.digest != data.digest: raise HTTPException(409, '权限申请摘要不匹配，请重新审查')
            if row.status != 'pending':
                if (data.approved and row.status in ('approved', 'running', 'completed')) or (not data.approved and row.status == 'rejected'): return output(row)
                raise HTTPException(409, '申请已处理，不能覆盖历史决定')
            apply_review(s, row, approved=data.approved, actor=user, comment=data.comment)
            return output(row)
