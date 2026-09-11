"""A run/task-scoped request → admin approval → offline sandbox installation ledger."""
import hashlib
import json
from typing import Literal

from fastapi import Depends, HTTPException
from pydantic import Field
from sqlalchemy import JSON, String, func, select, update
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, Run, Task, DecisionRequest, Outbox, session, emit, now
from .schema import Strict, HumanAnswer
from . import engagement, offline_tool_packages


class ToolInstallation(Base):
    __tablename__ = 'tool_installations'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    task_key: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str] = mapped_column(String(32))
    decision_id: Mapped[str] = mapped_column(String(64))
    digest: Mapped[str] = mapped_column(String(64))
    manifest: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(24), default='pending')
    reviewed_by: Mapped[str] = mapped_column(String(32), default='')
    review_comment: Mapped[str] = mapped_column(String(2000), default='')
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=now)
    updated_at: Mapped[str] = mapped_column(String(40), default=now)


class InstallRequest(Strict):
    package_id: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    reason: str = Field(min_length=3, max_length=2000)


class InstallationReference(Strict):
    installation_id: str = Field(pattern=r'^[a-f0-9]{64}$')


class InstalledInvocation(InstallationReference):
    operation: Literal['text_metrics']
    path: str = Field(min_length=1, max_length=500)


class InstallationReview(Strict):
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    approved: bool
    comment: str = Field('', max_length=2000)


class InstallationReuse(InstallationReference):
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')


def output(row):
    result = {key: getattr(row, key) for key in ('id', 'run_id', 'task_key', 'agent_id', 'decision_id', 'digest',
            'manifest', 'reason', 'status', 'reviewed_by', 'review_comment', 'result', 'created_at', 'updated_at')}
    from .workspace import task_root
    with session() as s:
        run = s.get(Run, row.run_id)
        project = run.snapshot['project'] if run else {}
    result.update({'cwd': str(task_root(row.run_id, row.task_key)), 'execution_mode': project.get('execution_mode', 'container'),
                   'permission_profile': project.get('permission_profile', 'ask'), 'scope': 'one run/task/agent only'})
    return result


def authorized_task(s, run_id, key):
    run, task = s.get(Run, run_id), s.get(Task, run_id + ':' + key)
    if not run or not task or run.status in ('completed', 'cancelled', 'needs_attention'):
        raise ValueError('运行已结束或执行任务不存在')
    agent = run.snapshot['agents'].get(task.agent_id)
    if not agent or 'run_command' not in agent['tools'] or task.kind not in ('develop', 'autonomous'):
        raise ValueError('只有已获命令权限的执行任务可申请或使用离线工具；讨论和只读角色不可扩权')
    return run, task


def request_installation(run_id, key, operation, arguments):
    data = InstallRequest.model_validate(arguments)
    manifest = offline_tool_packages.get_package(data.package_id, data.version)
    installation_id = hashlib.sha256((run_id + ':installation:' + operation).encode()).hexdigest()
    decision_id = hashlib.sha256((run_id + ':decision:' + operation).encode()).hexdigest()
    digest = engagement.digest({'run_id': run_id, 'task_key': key, 'manifest': manifest, 'reason': data.reason})
    with session() as s:
        s.execute(update(Run).where(Run.id == run_id).values(updated_at=Run.updated_at))
        run, task = authorized_task(s, run_id, key)
        old = s.get(ToolInstallation, installation_id)
        if old:
            if old.digest != digest: raise ValueError('同一安装申请操作不可更换来源、版本或理由')
            question = s.get(DecisionRequest, old.decision_id)
            return {**engagement.decision_out(question), 'installation_id': old.id, 'installation_status': old.status,
                    'review_url': '/api/tool-installations/' + old.id + '/review'}
        if s.scalar(select(func.count()).select_from(ToolInstallation).where(ToolInstallation.run_id == run_id)) >= 20:
            raise ValueError('每次运行最多 20 个工具安装申请')
        if s.scalar(select(func.count()).select_from(DecisionRequest).where(DecisionRequest.run_id == run_id)) >= 100:
            raise ValueError('本次运行的人工决策请求已达上限')
        question = {'question': '请管理员审查离线工具安装：' + manifest['id'] + '@' + manifest['version'],
                    'context': '来源：' + manifest['source'] + '\nSHA256：' + manifest['sha256'] + '\n理由：' + data.reason + '\n仅此运行/任务；在所选执行环境的断网任务副本内安装。须在工具安装审批入口批准，普通问题答复不会授予安装权。',
                    'options': [{'id': 'approve', 'label': '管理员批准安装'}, {'id': 'reject', 'label': '拒绝安装'}], 'allow_text': False}
        decision = DecisionRequest(id=decision_id, run_id=run_id, task_key=key, agent_id=task.agent_id,
                                   digest=engagement.digest({'run_id': run_id, 'task_key': key, 'question': question}), question=question)
        row = ToolInstallation(id=installation_id, run_id=run_id, task_key=key, agent_id=task.agent_id,
                               decision_id=decision_id, digest=digest, manifest=manifest, reason=data.reason)
        s.add_all([decision, row]); s.flush()
        emit(s, run_id, 'tool.installation.requested', {'installation_id': row.id, 'task_key': key, 'agent_id': task.agent_id,
             'package_id': manifest['id'], 'version': manifest['version'], 'digest': digest, 'source_sha256': manifest['sha256'], 'request_id': decision_id})
        emit(s, run_id, 'human.decision.requested', {'request_id': decision.id, 'task_key': key, 'agent_id': task.agent_id, 'digest': decision.digest, **question})
        return {**engagement.decision_out(decision), 'installation_id': row.id, 'installation_status': row.status,
                'review_url': '/api/tool-installations/' + row.id + '/review'}


async def invoke(name, arguments, run_id, key, operation, runtime, image):
    if name == 'request_tool_installation': return request_installation(run_id, key, operation, arguments)
    data = (InstalledInvocation if name == 'run_installed_tool' else InstallationReference).model_validate(arguments)
    with session() as s:
        run, task = authorized_task(s, run_id, key)
        row = s.get(ToolInstallation, data.installation_id)
        if not row or row.run_id != run_id or row.task_key != key or row.agent_id != task.agent_id:
            mistaken = s.scalar(select(ToolInstallation).where(ToolInstallation.decision_id == data.installation_id,
                ToolInstallation.run_id == run_id, ToolInstallation.task_key == key, ToolInstallation.agent_id == task.agent_id))
            if mistaken:
                raise ValueError('传入的是审批问题 request_id，不是安装许可 installation_id；本次没有执行。'
                    + '请使用 installation_id=' + mistaken.id + '（状态 ' + mistaken.status + '）；无需重复申请，仍须已批准才能安装。')
            raise ValueError('安装许可不属于当前运行、任务和成员')
        manifest = offline_tool_packages.get_package(row.manifest['id'], row.manifest['version'])
        if manifest != row.manifest: raise ValueError('固定离线来源已变化，必须重新申请审查')
        if name == 'install_approved_tool':
            if row.status == 'installed': return {'installation_id': row.id, 'status': 'installed', **row.result}
            locked = s.execute(update(ToolInstallation).where(ToolInstallation.id == row.id, ToolInstallation.status == 'approved').values(status='installing', updated_at=now()))
            if locked.rowcount != 1: raise ValueError('安装尚未获管理员批准，或先前结果不确定；不会自动重放')
        elif row.status != 'installed':
            raise ValueError('工具尚未完成经审批的安装')
    from .tools import command
    from .workspace import safe_path, task_root
    if name == 'run_installed_tool': safe_path(task_root(run_id, key), data.path)
    text = offline_tool_packages.command(manifest, install=name == 'install_approved_tool', path=getattr(data, 'path', ''))
    try:
        result = await command(run_id, key, operation, text, runtime, image)
        if result.get('exit_code') != 0 or result.get('timed_out') or result.get('truncated'):
            raise RuntimeError('离线工具执行未成功；保留沙箱输出，不自动安装重试。' + str(result.get('output', ''))[:700])
        parsed = json.loads(result['output'])
        if not isinstance(parsed, dict): raise ValueError('工具结果不是对象')
        if name == 'install_approved_tool' and (parsed.get('installed') is not True or parsed.get('sha256') != manifest['sha256'] or parsed.get('package_id') != manifest['id'] or parsed.get('version') != manifest['version']):
            raise ValueError('安装回执与批准的版本/摘要不一致')
    except Exception:
        if name == 'install_approved_tool':
            with session() as s:
                row = s.get(ToolInstallation, data.installation_id); row.status = 'uncertain'; row.updated_at = now()
                emit(s, run_id, 'tool.installation.uncertain', {'installation_id': row.id, 'task_key': key, 'agent_id': task.agent_id})
        raise
    with session() as s:
        row = s.get(ToolInstallation, data.installation_id)
        if name == 'install_approved_tool':
            row.status, row.result, row.updated_at = 'installed', parsed, now()
        emit(s, run_id, 'tool.installation.completed' if name == 'install_approved_tool' else 'tool.installed.invoked',
             {'installation_id': row.id, 'task_key': key, 'agent_id': task.agent_id, 'package_id': manifest['id'],
              'version': manifest['version'], 'source_sha256': manifest['sha256'], 'result': parsed})
    return {'installation_id': data.installation_id, 'status': 'installed' if name == 'install_approved_tool' else 'completed', 'result': parsed}


def register(app):
    from .api import admin, current_user, run_access

    @app.get('/api/tool-packages')
    def packages(user=Depends(current_user)):
        return {'items': [offline_tool_packages.package()], 'policy': '仅固定版本离线示例包；需要管理员审批；在所选本机或容器任务副本内安装；禁止联网和项目外安装'}

    @app.get('/api/tool-installations')
    def installations(run_id: str = '', user=Depends(current_user)):
        with session() as s:
            if run_id: run_access(s, run_id, user)
            elif user.role != 'admin': raise HTTPException(403, '非管理员需指定有权访问的运行')
            query = select(ToolInstallation).order_by(ToolInstallation.created_at.desc())
            if run_id: query = query.where(ToolInstallation.run_id == run_id)
            return {'items': [output(row) for row in s.scalars(query.limit(100))]}

    @app.post('/api/tool-installations/{installation_id}/review')
    def review(installation_id: str, data: InstallationReview, user=Depends(admin)):
        with session() as s:
            s.execute(update(ToolInstallation).where(ToolInstallation.id == installation_id).values(updated_at=ToolInstallation.updated_at))
            row = s.get(ToolInstallation, installation_id)
            if not row: raise HTTPException(404, '安装申请不存在')
            run = run_access(s, row.run_id, user)
            if row.digest != data.digest: raise HTTPException(409, '申请摘要已变化，请重新检查固定来源、权限和版本')
            if row.status != 'pending':
                if (data.approved and row.status in ('approved', 'installing', 'installed')) or (not data.approved and row.status == 'rejected'): return output(row)
                raise HTTPException(409, '该申请已审查，不能覆盖审计历史')
            authorized_task(s, row.run_id, row.task_key)
            manifest = offline_tool_packages.get_package(row.manifest['id'], row.manifest['version'])
            if manifest != row.manifest: raise HTTPException(409, '登记包内容变化，需重新申请')
            decision = s.get(DecisionRequest, row.decision_id)
            engagement.answer_request(s, run, row.decision_id,
                HumanAnswer(digest=decision.digest, choice_id='approve' if data.approved else 'reject', reject=not data.approved), user)
            row.status = 'approved' if data.approved else 'rejected'
            row.reviewed_by, row.review_comment, row.updated_at = user.id, data.comment, now()
            emit(s, row.run_id, 'tool.installation.reviewed', {'installation_id': row.id, 'task_key': row.task_key,
                 'agent_id': row.agent_id, 'actor': user.id, 'approved': data.approved, 'digest': row.digest, 'comment': data.comment})
            return output(row)

    @app.post('/api/tool-installations/{installation_id}/reuse-approved')
    def reuse_approved(installation_id: str, data: InstallationReuse, user=Depends(admin)):
        """Resolve a redundant request; never record another human approval.

        The original permission remains the only executable authority. Both rows
        and the exact source/scope must agree, including the original review.
        """
        with session() as s:
            row = s.get(ToolInstallation, installation_id)
            if not row: raise HTTPException(404, '安装申请不存在')
            run = run_access(s, row.run_id, user)
            s.execute(update(Run).where(Run.id == run.id).values(updated_at=Run.updated_at))
            s.refresh(row)
            if row.digest != data.digest: raise HTTPException(409, '申请摘要已变化，请刷新')
            if row.status == 'superseded' and row.result.get('reused_installation_id') == data.installation_id:
                return output(row)
            original = s.get(ToolInstallation, data.installation_id)
            if (row.status != 'pending' or not original or original.id == row.id
                    or original.run_id != row.run_id or original.task_key != row.task_key
                    or original.agent_id != row.agent_id or original.manifest != row.manifest
                    or original.status not in ('approved', 'installed') or not original.reviewed_by):
                raise HTTPException(409, '只能复用本运行、任务、成员已有的同一来源与版本的有效批准；不能扩大权限或重试不确定安装')
            try: authorized_task(s, row.run_id, row.task_key)
            except ValueError as exc: raise HTTPException(409, str(exc))
            if offline_tool_packages.get_package(row.manifest['id'], row.manifest['version']) != row.manifest:
                raise HTTPException(409, '固定来源已变化，不能复用旧批准')
            approved = s.get(DecisionRequest, original.decision_id)
            decision = s.get(DecisionRequest, row.decision_id)
            if (not decision or decision.status != 'pending' or not approved or approved.status != 'answered'
                    or approved.answer.get('choice_id') != 'approve' or approved.answer.get('reject')):
                raise HTTPException(409, '原始批准或待处理问题状态不符，不能复用')
            row.status, row.updated_at = 'superseded', now()
            row.result = {'reused_installation_id': original.id}
            # Not answered/approved on behalf of the user: a redundant question
            # is closed, and the workflow resumes with the original grant only.
            decision.status = 'superseded'
            payload = {'request_id': decision.id, 'digest': decision.digest, 'rejected': False,
                       'reused_installation_id': original.id}
            s.add(Outbox(run_id=run.id, action='respond', payload=payload))
            audit = {'installation_id': row.id, 'reused_installation_id': original.id,
                     'request_id': decision.id, 'original_request_id': approved.id,
                     'task_key': row.task_key, 'agent_id': row.agent_id, 'actor': user.id,
                     'digest': row.digest, 'source_sha256': row.manifest['sha256']}
            emit(s, run.id, 'tool.installation.reused', audit)
            emit(s, run.id, 'human.decision.superseded', audit)
            return output(row)
