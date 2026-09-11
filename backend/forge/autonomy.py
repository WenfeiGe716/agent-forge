"""Durable team autonomy behind the existing permission-checked agent turn.

Messages, assignments and meetings are run-scoped records, not new authority.
Only the coordinator may nominate a delivery candidate; human acceptance is
still bound to its artifact digest. No model or shell command runs in this module.
"""
import hashlib
import json
import zipfile
from sqlalchemy import select, update
from .db import session, Run, Task, Step, AgentMessage, Meeting, DecisionRequest, HumanInstruction, InstructionReceipt, emit, now
from .schema import TeamAssignment, TeamDiscussion
from . import workspace as ws

TERMINAL_TASKS = {'done', 'integrated', 'unverified', 'failed', 'conflicted', 'superseded'}


def _id(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _authority(s, run_id, key):
    s.execute(update(Run).where(Run.id == run_id).values(updated_at=Run.updated_at))
    run, task = s.get(Run, run_id), s.get(Task, run_id + ':' + key)
    if not run or not task or run.snapshot.get('execution_strategy') != 'autonomous':
        raise ValueError('此操作仅用于本次团队自治任务')
    if run.status not in {'queued', 'running', 'intake', 'planning', 'discussing'}:
        raise ValueError('任务当前不可发起新的团队工作')
    if task.agent_id not in run.snapshot['agents']:
        raise ValueError('成员不属于本次冻结团队')
    return run, task


def _leader(run):
    return run.snapshot['entry_agent_id']


def _require_peer(run, sender, recipient):
    from .engagement import require_communication
    if recipient not in run.snapshot['agents']:
        raise ValueError('只能联系本次已授权团队中的成员')
    require_communication(run.snapshot['agents'], sender, recipient)


def _tasks(s, run):
    return list(s.scalars(select(Task).where(Task.run_id == run.id).order_by(Task.started_at, Task.key)))


def available_tools(run, agent):
    """The directory and execution expose the same frozen capability grants."""
    names = [*agent['tools'], 'inspect_team', 'request_decision']
    if 'read_file' in agent['tools']:
        names += ['read_work_result', 'read_work_issue']
        if agent['id'] == _leader(run): names.append('resolve_work_issue')
    if agent['id'] == _leader(run): names += ['assign_work', 'start_discussion', 'end_discussion']
    if agent.get('skills'):
        names += ['activate_skill', 'read_skill_resource']
        if 'run_command' in agent['tools'] and any(f.get('allow_scripts') for f in agent['skills']):
            names.append('run_skill_script')
    if 'run_command' in agent['tools']:
        names += ['inspect_environment', 'request_tool_installation', 'install_approved_tool', 'run_installed_tool']
        if run.snapshot['project'].get('execution_mode', 'container') == 'local':
            names += ['request_execution_permission', 'run_approved_command']
    return list(dict.fromkeys(names))


def _queue(s, run, identity, recipient, title, spec):
    key = 'auto_' + _id(identity)[:24]
    tid = run.id + ':' + key
    existing = s.get(Task, tid)
    payload = {**spec, 'coordinator': recipient == _leader(run)}
    if existing:
        if existing.agent_id != recipient or existing.title != title or existing.spec.get('request_digest') != payload.get('request_digest'):
            raise ValueError('同一委派操作不能更换成员或工作内容')
        return existing
    row = Task(id=tid, run_id=run.id, key=key, kind='autonomous', agent_id=recipient,
               title=title[:180], spec=payload, status='pending')
    s.add(row)
    s.flush()
    emit(s, run.id, 'team.work.queued', {'task_key': key, 'agent_id': recipient,
         'assignment_type': spec.get('assignment_type'), 'summary': title[:180]})
    return row


def initialize(s, run):
    return _queue(s, run, run.id + ':initial', _leader(run), '理解目标并组织团队工作', {
        'assignment_type': 'coordination', 'description': run.request,
        'acceptance': '核对原始目标与实际成果，必要时委派或求助；不要强制召开会议。',
        'request_digest': _id(run.request), 'report_to': None})


def inspect_team(run_id, key, arguments):
    if set(arguments) - {'member_id'}: raise ValueError('能力目录参数不匹配')
    member_id = arguments.get('member_id')
    if member_id is not None and (not isinstance(member_id, str) or not member_id):
        raise ValueError('member_id 必须为本次团队成员ID字符串')
    with session() as s:
        run, task = _authority(s, run_id, key)
        if member_id is not None and member_id not in run.snapshot['agents']:
            raise ValueError('只能查看本次团队成员的能力')
        rows = _tasks(s, run)
        members = []
        for aid, agent in run.snapshot['agents'].items():
            if member_id is not None and aid != member_id: continue
            instructions = agent.get('instructions', '')
            members.append({'id': aid, 'name': agent['name'], 'job_title': agent.get('job_title', ''),
                'responsibilities': instructions if member_id else instructions[:600],
                'description_abbreviated': not member_id and len(instructions) > 600,
                'is_coordinator': aid == _leader(run), 'tools': available_tools(run, agent),
                'skills': [{'name': f['name'], 'version': f.get('version'),
                            'description': f.get('description', '')} for f in agent.get('skills', [])],
                'models': [{'provider': m['provider_name'], 'model': m['model']['model_id']} for m in agent['models']],
                'assignments': [{'key': t.key, 'title': t.title, 'status': t.status} for t in rows if t.agent_id == aid]})
        return {'members': members, 'scope': 'this_frozen_run',
                'note': '能力与工具属于各成员自身，不转移权限。省略的职责可指定 member_id 查看全文。'}


def read_work_result(run_id, key, arguments):
    """Read a submitted path from the latest integration without changing a worktree."""
    if set(arguments) != {'task_key', 'path'} or any(not isinstance(v, str) or not v.strip() for v in arguments.values()):
        raise ValueError('请提供本次已集成工作 task_key 和成果 path')
    with session() as s:
        run, task = _authority(s, run_id, key)
        if 'read_file' not in run.snapshot['agents'][task.agent_id]['tools']:
            raise ValueError('读取团队成果需要该成员已有的文件读取权限')
        source = s.get(Task, run.id + ':' + arguments['task_key'])
        if not source or source.kind != 'autonomous' or source.status != 'integrated':
            raise ValueError('只能读取本次运行已集成工作的成果')
        path = arguments['path']
        if path not in source.result.get('deliverables', []):
            raise ValueError('该路径不在此成员提交的成果中')
        root = ws.run_root(run.id) / 'integration'
        commit = ws.git(root, 'rev-parse', 'HEAD').stdout.decode().strip()
        limit = run.snapshot['agents'][task.agent_id]['runtime']['max_file_bytes']
        raw = ws.read_workspace_file(root, path, limit)
        digest = hashlib.sha256(raw).hexdigest()
        content = raw.decode('utf-8')
        receipt = {'task_key': source.key, 'integration_commit': commit, 'sha256': digest}
        task.spec = {**task.spec, 'peer_file_reads': {**task.spec.get('peer_file_reads', {}), path: receipt}}
        return {**receipt, 'path': path, 'content': content, 'scope': 'current_team_integration',
                'note': '这是团队当前已集成版本；未覆盖你的个人工作副本。后续集成可能改变文件，请按摘要核验。'}


def assign_work(run_id, key, operation, arguments):
    request = TeamAssignment.model_validate(arguments, strict=True).model_dump()
    with session() as s:
        run, task = _authority(s, run_id, key)
        if task.agent_id != _leader(run): raise ValueError('正式分工由本次任务负责人决定；成员可直接发送求助消息')
        recipient = request['recipient_id']
        if recipient == task.agent_id:
            raise ValueError('自己的工作请在当前激活中直接完成，无需自委派；需要协助时选择其他成员')
        _require_peer(run, task.agent_id, recipient)
        _require_peer(run, recipient, task.agent_id)
        identity = run.id + ':assign:' + operation
        existing = s.get(Task, run.id + ':auto_' + _id(identity)[:24])
        if not existing and sum(t.spec.get('assignment_type') == 'delegation' for t in _tasks(s, run)) >= run.snapshot['runtime']['max_tasks']:
            raise ValueError('达到本次任务的工作分配数量上限，未扩大预算')
        row = _queue(s, run, identity, recipient, request['title'], {
            'assignment_type': 'delegation', 'description': request['instruction'],
            'acceptance': request['acceptance'], 'report_to': task.agent_id,
            'source_task_key': key, 'request_digest': _id(json.dumps(request, sort_keys=True, ensure_ascii=False))})
        if not existing:
            emit(s, run.id, 'task.delegated', {'from_agent_id': task.agent_id, 'to_agent_id': recipient,
                 'task_key': row.key, 'summary': request['title'], 'body': request['instruction']})
        return {'task_key': row.key, 'recipient_id': recipient, 'status': row.status,
                'note': '调度器将按成员占用和并发预算唤醒；无需等待固定流程。'}


def queue_message(s, run, sender_task, message, request_reply):
    """Called inside the same transaction that records the real message."""
    if run.snapshot.get('execution_strategy') != 'autonomous' or not request_reply:
        return None
    _require_peer(run, message.recipient_id, sender_task.agent_id)
    _require_peer(run, message.recipient_id, _leader(run))
    row = _queue(s, run, run.id + ':message:' + message.id, message.recipient_id,
        '回应 ' + run.snapshot['agents'][sender_task.agent_id]['name'] + ' 的协作请求', {
            'assignment_type': 'message', 'description': message.body,
            'acceptance': '处理这条具体请求，给出真实结果或明确限制；无需礼节性往返。',
            'report_to': sender_task.agent_id, 'source_task_key': sender_task.key,
            'message_ids': [message.id], 'request_digest': _id(message.body)})
    return row.key


def start_discussion(run_id, key, operation, arguments):
    request = TeamDiscussion.model_validate(arguments, strict=True).model_dump()
    with session() as s:
        run, task = _authority(s, run_id, key)
        if task.agent_id != _leader(run): raise ValueError('多人讨论由任务负责人召集；成员可以直接定向求助')
        ids = request['participants']
        if task.agent_id not in ids: raise ValueError('讨论需包含负责人；单人任务无需会议')
        from .engagement import validate_meeting
        validate_meeting(run.snapshot['agents'], ids)
        mid = _id(run.id + ':meeting:' + operation)
        old = s.get(Meeting, mid)
        if old:
            if old.participants != ids or old.agenda != request['agenda']: raise ValueError('同一会议操作不能改变议题或成员')
            return {'meeting_id': mid, 'status': old.status}
        if s.scalar(select(Meeting).where(Meeting.run_id == run.id, Meeting.status == 'active')):
            raise ValueError('请先结束当前讨论，再按需要召集下一次讨论')
        s.add(Meeting(id=mid, run_id=run.id, host_id=task.agent_id, participants=ids,
            subject=run.title, agenda=request['agenda'], status='active'))
        task.spec = {**task.spec, 'meeting_id': mid}
        for aid in ids:
            if aid == task.agent_id: continue
            _queue(s, run, mid + ':' + aid, aid, '参与定向讨论', {
                'assignment_type': 'discussion', 'description': request['agenda'],
                'acceptance': '只回答当前议题，给出依据、分歧和建议；不按轮数判断完成。',
                'meeting_id': mid, 'report_to': task.agent_id, 'source_task_key': key,
                'request_digest': _id(request['agenda'])})
        emit(s, run.id, 'meeting.opened', {'meeting_id': mid, 'host_id': task.agent_id,
             'participants': ids, 'subject': run.title, 'summary': request['agenda'][:600]})
        return {'meeting_id': mid, 'status': 'active', 'note': '仅召集所选成员；负责人按结果决定继续询问或结束，不强制轮流发言。'}


def end_discussion(run_id, key, operation, arguments):
    if set(arguments) != {'meeting_id', 'summary'} or any(not isinstance(v, str) or not v.strip() for v in arguments.values()) or len(arguments.get('summary', '')) > 8000:
        raise ValueError('请提供明确会议ID与讨论结论')
    with session() as s:
        run, task = _authority(s, run_id, key)
        meeting = s.get(Meeting, arguments['meeting_id'])
        if task.agent_id != _leader(run) or not meeting or meeting.run_id != run.id:
            raise ValueError('只能由本次负责人结束本次运行的会议')
        if meeting.status != 'active': return {'meeting_id': meeting.id, 'status': meeting.status}
        if any(t.spec.get('meeting_id') == meeting.id and t.key != key and t.status not in TERMINAL_TASKS for t in _tasks(s, run)):
            raise ValueError('仍有成员正在处理议题，请先等待其结果')
        meeting.status, meeting.ended_at = 'concluded', now()
        meeting.outcome = {'summary': arguments['summary'][:8000], 'source': 'coordinator'}
        emit(s, run.id, 'meeting.closed', {'meeting_id': meeting.id, 'summary': arguments['summary'][:1000]})
        return {'meeting_id': meeting.id, 'status': 'concluded'}


def task_context(run, task):
    with session() as s:
        from .work_issues import summaries
        rows = _tasks(s, run)
        return {'mode': 'autonomous', 'coordinator_id': _leader(run),
            'work_issues': summaries(s, run),
            'original_goal': run.request, 'this_assignment': task.spec,
            'team_capabilities': [{'id': a['id'], 'name': a['name'], 'job_title': a.get('job_title', ''),
                'responsibilities': a.get('instructions', '')[:500], 'tools': available_tools(run, a),
                'skills': [f['name'] for f in a.get('skills', [])]} for a in run.snapshot['agents'].values()],
            'work': [{'key': t.key, 'agent_id': t.agent_id, 'title': t.title, 'status': t.status,
                'outcome': t.result.get('outcome'), 'summary': t.result.get('summary', '')[:800],
                'deliverables': t.result.get('deliverables', [])} for t in rows[-60:]],
            'meetings': [{'id': m.id, 'agenda': m.agenda, 'participants': m.participants} for m in
                s.scalars(select(Meeting).where(Meeting.run_id == run.id, Meeting.status == 'active'))]}


def inbox(run, task):
    with session() as s:
        # Requests belong to their own activation. Informational messages may be
        # read by any next turn of the intended recipient, never another run.
        routed = {mid for t in _tasks(s, run) for mid in t.spec.get('message_ids', [])}
        own = set(task.spec.get('message_ids', []))
        messages = list(s.scalars(select(AgentMessage).where(AgentMessage.run_id == run.id,
            AgentMessage.recipient_id == task.agent_id, AgentMessage.delivered_at == '').order_by(AgentMessage.created_at, AgentMessage.id)))
        return [{'message_id': m.id, 'from_agent_id': m.sender_id,
            'from_name': run.snapshot['agents'][m.sender_id]['name'], 'task_key': m.task_key, 'body': m.body}
            for m in messages if m.id in own or m.id not in routed][:30]


def validate_submission(run, task, result):
    if result['outcome'] == 'ready' and task.agent_id != _leader(run):
        raise ValueError('成员只能提交自己的工作，整次交付候选由负责人核对后提交')
    root = ws.task_root(run.id, task.key)
    for path in result['deliverables']:
        ws.read_workspace_file(root, path, run.snapshot['runtime']['max_file_bytes'])
    with session() as s:
        evidence_paths = set()
        for key in result['evidence_task_keys']:
            evidence = s.get(Task, run.id + ':' + key)
            if not evidence or evidence.status != 'integrated' or evidence.result.get('outcome') not in {'done', 'ready'}:
                raise ValueError('引用的工作证据尚未完成并集成，或不属于本次运行')
            evidence_paths.update(evidence.result.get('deliverables', []))
        if result['outcome'] == 'ready':
            if any(t.spec.get('work_issue', {}).get('status') in {'open', 'escalated'} for t in _tasks(s, run)):
                raise ValueError('仍有未解决的工作问题，请负责人读取证据、协调修复并核验关闭后再交付')
            for path, receipt in task.spec.get('peer_file_reads', {}).items():
                if path in evidence_paths or path in result['deliverables']:
                    raw = ws.read_workspace_file(ws.run_root(run.id) / 'integration', path, run.snapshot['runtime']['max_file_bytes'])
                    if hashlib.sha256(raw).hexdigest() != receipt['sha256']:
                        raise ValueError('已核验的团队成果已变化，请重新读取后再提交：' + path)
            if any(t.key != task.key and t.kind == 'autonomous' and t.status != 'integrated'
                   and not (t.status == 'superseded' and t.spec.get('work_issue', {}).get('status') == 'resolved')
                   for t in _tasks(s, run)):
                raise ValueError('还有未完成的成员工作，先等待结果并核对，再提交交付候选')
            if any(t.key != task.key and t.kind == 'autonomous' and t.result.get('outcome') == 'waiting'
                   and not t.spec.get('resumed_by')
                   and not (t.status == 'superseded' and t.spec.get('work_issue', {}).get('status') == 'resolved')
                   for t in _tasks(s, run)):
                raise ValueError('仍有未解决的成员等待，请先核对其协作结果')
            if s.scalar(select(Meeting).where(Meeting.run_id == run.id, Meeting.status == 'active')):
                raise ValueError('请先结束当前讨论并记录结论')
            if s.scalar(select(DecisionRequest).where(DecisionRequest.run_id == run.id, DecisionRequest.status == 'pending')):
                raise ValueError('尚有未决审批或用户问题，不能提交交付候选')
            if not result['deliverables'] and not result['evidence_task_keys'] and len(result['notes'].strip()) < 20:
                raise ValueError('交付需要实际文件、已完成工作证据或完整文字成果，不能只有完成声明')
    return result


def _resume_coordinator_waits(s, run, recipient, resumed_key):
    if recipient != _leader(run): return
    # A result may return through a repaired member's downstream request, not
    # through the coordinator's immediate source_task_key. The real successor
    # still resumes the coordinator's run-wide wait; member waits stay separate.
    for previous in _tasks(s, run):
        if (previous.agent_id == recipient and previous.status == 'integrated'
            and previous.result.get('outcome') == 'waiting' and not previous.spec.get('resumed_by')):
            previous.spec = {**previous.spec, 'resumed_by': resumed_key}


def _deliver_result(s, run, source, recipient):
    if recipient == source.agent_id: return
    _require_peer(run, source.agent_id, recipient)
    mid = _id(run.id + ':result:' + source.key + ':' + recipient)
    body = json.dumps({'task_key': source.key, 'title': source.title, 'result': source.result}, ensure_ascii=False)
    # Full evidence remains in task_context; messages are bounded notifications.
    body = body[:2800]
    message = s.get(AgentMessage, mid)
    if not message:
        message = AgentMessage(id=mid, run_id=run.id, task_key=source.key, sender_id=source.agent_id,
            recipient_id=recipient, body=body)
        s.add(message)
        emit(s, run.id, 'agent.message', {'message_id': mid, 'task_key': source.key,
            'from_agent_id': source.agent_id, 'to_agent_id': recipient, 'body': body, 'summary': source.result.get('summary', '')[:300]})
    active = next((t for t in _tasks(s, run) if t.agent_id == recipient and t.status not in TERMINAL_TASKS), None)
    original = s.get(Task, run.id + ':' + source.spec.get('source_task_key', ''))
    if active:
        if original and original.agent_id == recipient and original.key != active.key:
            original.spec = {**original.spec, 'resumed_by': active.key}
        _resume_coordinator_waits(s, run, recipient, active.key)
        return
    spec = {'assignment_type': 'coordination' if recipient == _leader(run) else 'message',
        'description': run.request if recipient == _leader(run) else (original.spec.get('description') if original and original.agent_id == recipient else '核对成员返回的结果并完成自己的任务'),
        'acceptance': '依据原始目标与已返回结果继续工作；仍需协助就定向联系，有成果再提交。',
        'report_to': original.spec.get('report_to') if original and original.agent_id == recipient else None,
        'source_task_key': original.spec.get('source_task_key', '') if original and original.agent_id == recipient else '',
        'resumes_task': original.key if original and original.agent_id == recipient else '',
        'message_ids': [mid], 'request_digest': _id(body)}
    resumed = _queue(s, run, run.id + ':wake:' + mid, recipient, '处理成员返回的工作结果', spec)
    if original and original.agent_id == recipient:
        original.spec = {**original.spec, 'resumed_by': resumed.key}
    _resume_coordinator_waits(s, run, recipient, resumed.key)


def _wake_human_guidance(s, run):
    """Supplementary user guidance reaches an idle authorized member as well."""
    for instruction in s.scalars(select(HumanInstruction).where(HumanInstruction.run_id == run.id).order_by(HumanInstruction.created_at, HumanInstruction.id)):
        for recipient in instruction.recipients:
            if recipient not in run.snapshot['agents']: continue
            own = [t for t in _tasks(s, run) if t.agent_id == recipient]
            if s.scalar(select(InstructionReceipt).where(InstructionReceipt.instruction_id == instruction.id,
                          InstructionReceipt.task_id.in_([t.id for t in own]))): continue
            if any(t.status not in TERMINAL_TASKS for t in own): continue
            original = own[-1] if own else None
            row = _queue(s, run, run.id + ':human:' + instruction.id + ':' + recipient,
                recipient, '处理用户补充说明', {'assignment_type': 'coordination' if recipient == _leader(run) else 'message',
                'description': run.request, 'human_instruction_id': instruction.id,
                'report_to': _leader(run) if recipient != _leader(run) else None,
                'acceptance': '在原有团队与权限内处理用户补充说明，不改变授权或隐瞒未完成工作。',
                'request_digest': _id(instruction.body)})
            if original and original.result.get('outcome') == 'waiting':
                original.spec = {**original.spec, 'resumed_by': row.key}


def _publish(run, candidate, checks):
    from .config import settings
    integration = ws.run_root(run.id) / 'integration'
    paths = set(candidate.result.get('deliverables', []))
    with session() as s:
        records = _tasks(s, run)
        for key in candidate.result.get('evidence_task_keys', []):
            item = next(t for t in records if t.key == key)
            paths.update(item.result.get('deliverables', []))
        report_tasks = [{'key': t.key, 'agent_id': t.agent_id, 'title': t.title, 'status': t.status,
                         'result': t.result} for t in records]
    files = []
    for path in sorted(paths):
        raw = ws.read_workspace_file(integration, path, run.snapshot['runtime']['max_file_bytes'])
        files.append({'path': path, 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)})
    report = {'mode': 'autonomous', 'execution_strategy': 'autonomous', 'run_id': run.id, 'title': run.title,
        'request': run.request, 'summary': candidate.result['summary'], 'notes': candidate.result.get('notes', ''),
        'coordinator_id': _leader(run), 'deliverables': files, 'tasks': report_tasks,
        'checks': checks, 'review': {'verdict': 'not_requested', 'summary': '由负责人核对，等待用户验收；未伪造独立审查。'},
        'base_commit': run.snapshot['base_commit'], 'input_tokens': run.input_tokens,
        'output_tokens': run.output_tokens, 'model_calls': run.calls,
        'limitations': ['成员能力描述不等于执行证据。', '未执行的检查不视为通过。', '不会自动回写原始导入目录或部署。']}
    with session() as s:
        from .engagement import overview
        report['collaboration'] = overview(s, s.get(Run, run.id))
    patch = ws.git(integration, 'diff', '--binary', '--no-ext-diff', '--no-textconv', run.snapshot['base_commit'], 'HEAD').stdout
    folder = ws.run_root(run.id) / 'artifacts'
    folder.mkdir(exist_ok=True)
    report_bytes = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2).encode()
    (folder / 'report.json').write_bytes(report_bytes)
    (folder / 'changes.patch').write_bytes(patch)
    ws.assert_safe_tree(integration)
    with zipfile.ZipFile(folder / 'source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        listed = ws.list_files(integration, limit=30001)
        if len(listed) > 30000: raise ValueError('成果文件数超过安全导出上限')
        remaining = settings.max_project_mb * 1024 * 1024
        for rel in listed:
            raw = ws.read_workspace_file(integration, rel, max(1, remaining))
            remaining -= len(raw)
            if remaining < 0: raise ValueError('成果总大小超过安全导出上限')
            archive.writestr(rel, raw)
    checksum = hashlib.sha256(patch + report_bytes).hexdigest()
    with session() as s:
        row = s.get(Run, run.id)
        row.report, row.artifact_digest = report, checksum
        emit(s, run.id, 'artifacts.ready', {'digest': checksum, 'files': ['changes.patch', 'report.json', 'source.zip'], 'mode': 'autonomous'})
    return {'digest': checksum}


def autonomy_tick(data):
    """One journaled observation/transition; the caller supplies replay-stable tick."""
    from .engine import create_task, load
    run = load(data['run_id'])
    if run.snapshot.get('execution_strategy') != 'autonomous': raise ValueError('非自治运行')
    answered = set(data.get('answered', []))
    with session() as s:
        run = s.get(Run, run.id)
        _wake_human_guidance(s, run)
        rows = _tasks(s, run)
        done = [t.key for t in rows if t.kind == 'autonomous' and t.status == 'done']
        if done: return {'integrate': done, 'runnable': [], 'blocked': []}
        from .work_issues import reconcile
        recovery_stop = reconcile(s, run)
        if recovery_stop: return {'stop': {'reason': recovery_stop}, 'runnable': [], 'blocked': []}
        rows = _tasks(s, run)
        for source in rows:
            if source.kind != 'autonomous' or source.status != 'integrated' or source.spec.get('result_announced'): continue
            outcome = source.result.get('outcome')
            if outcome in {'done', 'blocked'}:
                for recipient in dict.fromkeys([source.spec.get('report_to'), _leader(run)]):
                    if recipient: _deliver_result(s, run, source, recipient)
            source.spec = {**source.spec, 'result_announced': True}
        s.flush()
        rows = _tasks(s, run)
        # A member owns its prepared activation until that work is integrated,
        # including while waiting for an approval. A newly queued request has
        # no started_at and must not jump ahead when the old approval arrives.
        # Keep this ownership local to each member: globally sorting all active
        # work first would starve unprepared work belonging to other members.
        active = [t for t in rows if t.kind == 'autonomous' and t.status not in TERMINAL_TASKS]
        owners = {}
        for task in sorted(active, key=lambda t: (not bool(t.started_at), t.started_at, t.key)):
            if task.base_commit or task.started_at:
                owners.setdefault(task.agent_id, task.key)
        runnable, blocked, occupied = [], [], set()
        for task in rows:
            if task.kind != 'autonomous' or task.status in TERMINAL_TASKS: continue
            if task.agent_id in owners and owners[task.agent_id] != task.key: continue
            source_key = task.spec.get('source_task_key')
            source = next((t for t in rows if t.key == source_key), None)
            source_ready = source and (source.status == 'integrated' or
                (source.status == 'superseded' and source.spec.get('work_issue', {}).get('status') == 'resolved'))
            if not task.base_commit and source and not source_ready:
                # New work sees the sender's integrated files. The sender yields
                # with finish(waiting); this never blocks a already-started peer.
                continue
            if task.status == 'waiting_decision':
                steps = list(s.scalars(select(Step).where(Step.id.like(task.id + ':turn:%'), Step.state == 'completed')))
                pending = next((step.result.get('blocked_on') for step in sorted(steps, key=lambda x: int(x.id.rsplit(':', 1)[1]), reverse=True) if step.result.get('blocked_on')), None)
                if pending not in answered:
                    blocked.append({'key': task.key, 'request_id': pending})
                    occupied.add(task.agent_id)
                    continue
                task.status = 'pending'
            if task.agent_id not in occupied:
                runnable.append({'key': task.key, 'agent_id': task.agent_id})
                occupied.add(task.agent_id)
        # Never schedule another activation of an agent blocked on explicit approval.
        blocked_agents = {t.agent_id for t in rows if t.key in {b['key'] for b in blocked}}
        runnable = [r for r in runnable if r['agent_id'] not in blocked_agents]
        # Do not freeze a worktree for a task that will not receive a turn in
        # this batch. The workflow's defensive limit alone is too late.
        runnable = runnable[:run.snapshot['runtime']['max_parallel']]
        if runnable:
            specs = [(r, next(t for t in rows if t.key == r['key']).spec) for r in runnable]
        else: specs = []
        candidates = [t for t in rows if t.kind == 'autonomous' and t.agent_id == _leader(run)
                      and t.status == 'integrated' and t.result.get('outcome') == 'ready']
        if not runnable and not blocked and candidates:
            candidate = candidates[-1]
            checks = {'verification': 'not_requested', 'commands': []}
            if run.snapshot.get('run_project_checks'):
                check_index = candidate.spec.get('check_round')
                if check_index is None:
                    check_index = sum(t.kind == 'check' for t in rows)
                    candidate.spec = {**candidate.spec, 'check_round': check_index}
                check_task = s.get(Task, run.id + ':checks_' + str(check_index))
                if not check_task: return {'checks': {'round': check_index}, 'runnable': [], 'blocked': []}
                checks = check_task.result
                if checks.get('verification') == 'failed':
                    candidate.result = {**candidate.result, 'outcome': 'waiting'}
                    resumed = _queue(s, run, candidate.id + ':check-feedback', _leader(run), '处理验收检查反馈', {
                        'assignment_type': 'coordination', 'description': run.request,
                        'feedback': checks, 'report_to': None, 'request_digest': _id(json.dumps(checks, sort_keys=True))})
                    candidate.spec = {**candidate.spec, 'resumed_by': resumed.key}
                    return {'runnable': [], 'blocked': [], 'again': True}
        elif not runnable and not blocked:
            leader_tasks = [t for t in rows if t.agent_id == _leader(run) and t.kind == 'autonomous']
            last_leader = leader_tasks[-1] if leader_tasks else None
            reminder = last_leader.spec.get('delivery_reminder_count', 0) if last_leader else 0
            if last_leader and reminder < 3 and last_leader.result.get('outcome') == 'done':
                _queue(s, run, run.id + ':delivery-review:' + str(len(leader_tasks)), _leader(run), '核对目标并提交交付候选', {
                    'assignment_type': 'coordination', 'description': run.request, 'report_to': None,
                    'delivery_reminder_count': reminder + 1,
                    'acceptance': '若目标达到，请列出真实成果并用 ready 提交；否则分配后续工作或说明阻塞。', 'request_digest': _id(run.request)})
                return {'runnable': [], 'blocked': [], 'again': True}
            return {'stop': {'reason': '团队尚未提交可验收成果，且没有可继续的工作或待回复申请；已保留现场。'}, 'runnable': [], 'blocked': []}
        else: candidate = None
    if specs:
        for item, spec in specs:
            create_task(run.id, item['key'], 'autonomous', item['agent_id'],
                next(t.title for t in rows if t.key == item['key']), spec)
        return {'runnable': runnable, 'blocked': blocked, 'integrate': []}
    if blocked: return {'runnable': [], 'blocked': blocked}
    return {'candidate': _publish(run, candidate, checks), 'runnable': [], 'blocked': []}
