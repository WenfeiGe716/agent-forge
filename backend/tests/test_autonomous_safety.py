"""Autonomy safety at the real SQLite/tool/workspace boundary.

All state uses conftest's temporary database and project. Model replies are
fixtures; no provider, production workspace, native UI or shell tool is called.
"""
import hashlib
import json

import pytest
from sqlalchemy import select

from forge import autonomy, engine, providers, tools, workspace as ws
from forge.collaboration import mark_included, pending_messages, send_message
from forge.db import AgentMessage, DecisionRequest, Meeting, Run, Task, session
from test_autonomous_execution import finish, initial, new_run


def member_ids(configured):
    return tuple(next(a['id'] for a in configured['agents'] if a['role'] == role)
                 for role in ('planner', 'developer', 'tester'))


def rows(rid):
    with session() as s:
        return list(s.scalars(select(Task).where(Task.run_id == rid)))


def assign(rid, key, recipient, operation='delegate'):
    return autonomy.assign_work(rid, key, operation, {
        'recipient_id': recipient, 'title': '完成团队分配的工作',
        'instruction': '核对真实资料并提交实际成果，不扩大权限。',
        'acceptance': '交付文件存在且说明依据。',
    })['task_key']


def tick(rid, index):
    return autonomy.autonomy_tick({'run_id': rid, 'tick': index})


def communication(rid, aid, recipients):
    with session() as s:
        run = s.get(Run, rid)
        snapshot = json.loads(json.dumps(run.snapshot))
        snapshot['agents'][aid]['communication'] = {'scope': 'custom', 'recipients': recipients}
        run.snapshot = snapshot


async def read_result(rid, key, source_key, path):
    """Go through real tool authorization and dispatch, not a replacement reader."""
    with session() as s:
        run, task = s.get(Run, rid), s.get(Task, rid + ':' + key)
        agent = run.snapshot['agents'][task.agent_id]
        allowed = autonomy.available_tools(run, agent)
    return await tools.invoke('read_work_result', {'task_key': source_key, 'path': path},
        allowed, ws.task_root(rid, key), rid, key, 'safety-read-' + source_key,
        agent['runtime'], run.snapshot['project']['sandbox_image'])


def result_fixture(client, configured):
    """A real integrated member file and a coordinator's fresh activation."""
    _, worker, _ = member_ids(configured)
    rid, _ = new_run(client, configured)
    key = initial(rid)
    source = assign(rid, key, worker)
    finish(rid, key, 'waiting')
    tick(rid, 1)
    ws.write_workspace_file(ws.task_root(rid, source), 'docs/shared.md', b'# Shared result version 1\n', 10000)
    finish(rid, source, deliverables=['docs/shared.md'])
    receiver = tick(rid, 2)['runnable'][0]['key']
    return rid, receiver, source


def revise_integrated_result(rid, receiver, configured, content=b'# Shared result version 2\n'):
    """A concurrent teammate integrates a later version while receiver is active."""
    _, _, reviser = member_ids(configured)
    revision = assign(rid, receiver, reviser, 'revise-source')
    # This fixture represents work already independent of the receiver; it
    # must not wait for that receiver's current activation to yield its files.
    with session() as s:
        task = s.get(Task, rid + ':' + revision)
        spec = {k: v for k, v in task.spec.items() if k != 'source_task_key'}
        task.spec = spec
    engine.create_task(rid, revision, 'autonomous', reviser, '修订共享成果', spec)
    ws.write_workspace_file(ws.task_root(rid, revision), 'docs/shared.md', content, 10000)
    finish(rid, revision, deliverables=['docs/shared.md'])
    return revision


@pytest.mark.asyncio
async def test_peer_help_returns_to_original_worker_then_coordinator(client, configured, monkeypatch):
    """A delegates B; B asks C; C's result resumes B with B's original remit."""
    lead, worker, peer = member_ids(configured)
    rid, _ = new_run(client, configured)
    lead_key = initial(rid)
    worker_key = assign(rid, lead_key, worker)
    finish(rid, lead_key, 'waiting')
    assert tick(rid, 1)['runnable'] == [{'key': worker_key, 'agent_id': worker}]
    calls = []

    async def model(connection, model, messages, definitions, secret, timeout):
        context = json.loads(messages[1]['content'])
        spec = context['task']
        names = [c['name'] for m in messages if m['role'] == 'assistant' for c in m.get('calls', [])]
        if spec['assignment_type'] == 'delegation':
            if not names:
                name, args = 'send_message', {'recipient_id': peer, 'body': '请创建 docs/source.md，说明资料依据。', 'request_reply': True}
            else:
                name, args = 'finish', {'outcome': 'waiting', 'summary': '等待同事返回资料，随后继续完成原始工作。'}
        elif spec.get('resumes_task') == worker_key:
            if not names:
                name, args = 'read_file', {'path': 'docs/source.md'}
            elif 'write_file' not in names:
                assert any(m['role'] == 'tool' and 'Verified source material' in m.get('content', '') for m in messages)
                name, args = 'write_file', {'path': 'docs/result.md', 'content': '# Result\nBased on the verified source material.\n'}
            else:
                name, args = 'finish', {'outcome': 'done', 'summary': '核对同事资料后已形成最终文档。', 'deliverables': ['docs/result.md']}
        else:
            assert spec['assignment_type'] == 'message' and spec['report_to'] == worker
            if not names:
                name, args = 'write_file', {'path': 'docs/source.md', 'content': '# Evidence\nVerified source material.\n'}
            else:
                name, args = 'finish', {'outcome': 'done', 'summary': '资料文件已实际形成。', 'deliverables': ['docs/source.md']}
        assert name in {definition['name'] for definition in definitions}
        calls.append(name)
        return providers.Reply('', [{'id': 'safety-call-' + str(len(calls)), 'name': name, 'arguments': args}], 1, 1)

    monkeypatch.setattr(providers, 'complete', model)

    async def activation(key, turns):
        for index in range(turns):
            result = await engine.turn({'run_id': rid, 'key': key, 'index': index})
            assert result['done'] is (index == turns - 1)
        engine.integrate_task({'run_id': rid, 'key': key})

    await activation(worker_key, 2)
    peer_activation = tick(rid, 2)['runnable']
    assert len(peer_activation) == 1 and peer_activation[0]['agent_id'] == peer
    await activation(peer_activation[0]['key'], 2)
    next_work = tick(rid, 3)['runnable']
    continuation = next(item['key'] for item in next_work if item['agent_id'] == worker)
    leader_continuation = next(item['key'] for item in next_work if item['agent_id'] == lead)
    assert continuation != worker_key
    with session() as s:
        resumed = s.get(Task, rid + ':' + continuation)
        original = s.get(Task, rid + ':' + worker_key)
        assert resumed.spec['report_to'] == lead
        assert resumed.spec['source_task_key'] == lead_key
        assert resumed.spec['description'] == original.spec['description']
        assert original.spec['resumed_by'] == continuation
    await activation(continuation, 3)
    assert tick(rid, 4)['runnable'] == [{'key': leader_continuation, 'agent_id': lead}]
    with session() as s:
        run, leader_task = s.get(Run, rid), s.get(Task, rid + ':' + leader_continuation)
        messages = autonomy.inbox(run, leader_task)
        assert any(m['from_agent_id'] == worker and 'docs/result.md' in m['body'] for m in messages)
        assert all(t.status == 'integrated' for t in rows(rid) if t.key != leader_continuation)
    assert ws.read_workspace_file(ws.run_root(rid) / 'integration', 'docs/result.md', 10000).startswith(b'# Result')
    # The coordinator's old copy stays untouched. The explicit result reader
    # exposes the newly integrated file and records exactly what was inspected.
    with pytest.raises(ValueError):
        ws.read_workspace_file(ws.task_root(rid, leader_continuation), 'docs/result.md', 10000)
    output = await read_result(rid, leader_continuation, continuation, 'docs/result.md')
    assert output['content'].startswith('# Result')
    assert output['sha256'] == hashlib.sha256(output['content'].encode()).hexdigest()
    assert output['integration_commit'] == ws.git(ws.run_root(rid) / 'integration', 'rev-parse', 'HEAD').stdout.decode().strip()
    assert output['scope'] and 'integration' in output['scope']
    with session() as s:
        receipt = s.get(Task, rid + ':' + leader_continuation).spec['peer_file_reads']['docs/result.md']
        assert receipt['sha256'] == output['sha256'] and receipt['task_key'] == continuation
    assert engine.load(rid).status == 'running'  # A worker's done never completes the whole run.
    assert calls == ['send_message', 'finish', 'write_file', 'finish', 'read_file', 'write_file', 'finish']


def test_blocked_member_reports_to_coordinator_without_global_stop(client, configured):
    lead, worker, _ = member_ids(configured)
    rid, _ = new_run(client, configured)
    lead_key = initial(rid)
    worker_key = assign(rid, lead_key, worker)
    finish(rid, lead_key, 'waiting')
    tick(rid, 1)
    finish(rid, worker_key, 'blocked', summary='缺少输入数据，负责人需要决定替代方案。')
    state = tick(rid, 2)
    assert 'stop' not in state and state['runnable'][0]['agent_id'] == lead
    assert engine.load(rid).status == 'running'
    with session() as s:
        inbox = autonomy.inbox(s.get(Run, rid), s.get(Task, rid + ':' + state['runnable'][0]['key']))
        assert any(m['from_agent_id'] == worker and '缺少输入数据' in m['body'] for m in inbox)


def test_member_cannot_submit_entire_team_ready(client, configured):
    _, worker, _ = member_ids(configured)
    rid, _ = new_run(client, configured)
    lead_key = initial(rid)
    worker_key = assign(rid, lead_key, worker)
    finish(rid, lead_key, 'waiting')
    tick(rid, 1)
    with pytest.raises(ValueError, match='整次交付候选由负责人'):
        finish(rid, worker_key, 'ready', notes='这是有足够长度的成果说明，但成员仍无权代替负责人提交整次交付。')
    with session() as s:
        assert s.get(Task, rid + ':' + worker_key).status == 'pending'


def test_ready_rejects_pending_member_work(client, configured):
    _, worker, _ = member_ids(configured)
    rid, _ = new_run(client, configured)
    key = initial(rid)
    assign(rid, key, worker)
    with pytest.raises(ValueError, match='未完成的成员工作'):
        finish(rid, key, 'ready', notes='负责人不能仅凭声明跳过已委派但尚未执行的工作。')


def test_ready_rejects_active_meeting_after_members_return(client, configured):
    lead, worker, _ = member_ids(configured)
    rid, _ = new_run(client, configured)
    key = initial(rid)
    meeting = autonomy.start_discussion(rid, key, 'review-issue', {'participants': [lead, worker], 'agenda': '核对资料缺失的处理方案'})
    finish(rid, key, 'waiting')
    worker_key = tick(rid, 1)['runnable'][0]['key']
    finish(rid, worker_key, notes='本次讨论的事实与建议已给出。')
    resumed_key = tick(rid, 2)['runnable'][0]['key']
    with pytest.raises(ValueError, match='先结束当前讨论'):
        finish(rid, resumed_key, 'ready', notes='即使所有成员都返回结果，负责人也必须记录会议结论才能提交。')
    with session() as s:
        assert s.get(Meeting, meeting['meeting_id']).status == 'active'


def test_ready_rejects_pending_human_decision(client, configured):
    rid, lead = new_run(client, configured)
    key = initial(rid)
    with session() as s:
        s.add(DecisionRequest(id='isolated-pending-decision', run_id=rid, task_key=key,
            agent_id=lead, digest='a' * 64, question={'question': '请选择交付语言'}, status='pending'))
    with pytest.raises(ValueError, match='未决审批或用户问题'):
        finish(rid, key, 'ready', notes='有一个用户问题未决，负责人不得自己假定用户已经批准或回答。')


def test_ready_rejects_waiting_member_without_continuation(client, configured):
    lead, worker, _ = member_ids(configured)
    rid, _ = new_run(client, configured)
    lead_key = initial(rid)
    worker_key = assign(rid, lead_key, worker)
    finish(rid, lead_key, 'waiting')
    tick(rid, 1)
    finish(rid, worker_key, 'waiting')
    # An independent leader activation cannot silently treat an unresolved wait
    # as finished merely because the member's file changes were integrated.
    engine.create_task(rid, 'safety_leader_continuation', 'autonomous', lead, '核对进展', {'coordinator': True})
    with pytest.raises(ValueError, match='未解决的成员等待'):
        finish(rid, 'safety_leader_continuation', 'ready', notes='文件集成不等于工作已经完成，必须解决成员仍在等待的问题。')


def test_messages_and_delivery_receipts_never_cross_run(client, configured):
    _, worker, _ = member_ids(configured)
    first, _ = new_run(client, configured)
    second, _ = new_run(client, configured)
    first_key, second_key = initial(first), initial(second)
    first_msg = send_message(first, first_key, 'same-operation', worker, '只属于第一个任务的数据', False)
    second_msg = send_message(second, second_key, 'same-operation', worker, '只属于第二个任务的数据', False)
    assert first_msg['message_id'] != second_msg['message_id']
    first_inbox, second_inbox = pending_messages(first, worker), pending_messages(second, worker)
    assert [m['body'] for m in first_inbox] == ['只属于第一个任务的数据']
    assert [m['body'] for m in second_inbox] == ['只属于第二个任务的数据']
    mark_included(first, worker, first_key, second_inbox)
    assert len(pending_messages(second, worker)) == 1
    mark_included(first, worker, first_key, first_inbox)
    assert not pending_messages(first, worker)
    assert len(pending_messages(second, worker)) == 1
    # Autonomous inbox also checks the run, not just a reused member ID.
    for rid, expected in [(first, []), (second, ['只属于第二个任务的数据'])]:
        engine.create_task(rid, 'safety_reader', 'autonomous', worker, '读取自己任务内通知', {})
        with session() as s:
            own = autonomy.inbox(s.get(Run, rid), s.get(Task, rid + ':safety_reader'))
            assert [m['body'] for m in own] == expected


@pytest.mark.parametrize('direction', ['sender_denied', 'reply_denied', 'leader_report_denied'])
def test_reply_request_requires_all_actual_communication_edges(client, configured, direction):
    lead, worker, peer = member_ids(configured)
    rid, _ = new_run(client, configured)
    key = initial(rid)
    worker_key = assign(rid, key, worker)
    finish(rid, key, 'waiting')
    tick(rid, 1)
    if direction == 'sender_denied':
        communication(rid, worker, [lead])
    elif direction == 'reply_denied':
        communication(rid, peer, [lead])
    else:
        communication(rid, peer, [worker])
    before = len(rows(rid))
    with pytest.raises(ValueError, match='通信范围'):
        send_message(rid, worker_key, 'denied-help', peer, '请在现有权限内检查资料', True)
    assert len(rows(rid)) == before
    with session() as s:
        assert not list(s.scalars(select(AgentMessage).where(AgentMessage.run_id == rid)))
        assert not s.get(Task, rid + ':' + worker_key).spec.get('message_wake_modes')


def test_one_way_information_does_not_require_reply_or_wake_idle_member(client, configured):
    lead, worker, _ = member_ids(configured)
    rid, _ = new_run(client, configured)
    key = initial(rid)
    communication(rid, worker, [])
    response = send_message(rid, key, 'one-way-fyi', worker, '仅通知状态，不需要回复。', False)
    assert response['wake_task_key'] is None
    assert len(rows(rid)) == 1
    for index in range(1, 4):
        assert tick(rid, index)['runnable'] == [{'key': key, 'agent_id': lead}]
        assert len(rows(rid)) == 1
    assert [m['body'] for m in pending_messages(rid, worker)] == ['仅通知状态，不需要回复。']


def test_cross_team_message_rejected_before_enqueue(client, configured):
    lead, worker, peer = member_ids(configured)
    selected = [a for a in configured['agents'] if a['id'] in {lead, worker}]
    rid, _ = new_run(client, configured, selected)
    key = initial(rid)
    with pytest.raises(ValueError, match='参与团队'):
        send_message(rid, key, 'outside-frozen-team', peer, '虽然在组织中，但不属于本次授权团队。', True)
    assert len(rows(rid)) == 1
    with session() as s:
        assert not list(s.scalars(select(AgentMessage).where(AgentMessage.run_id == rid)))


@pytest.mark.asyncio
async def test_result_reader_uses_current_integration_and_preserves_personal_edits(client, configured):
    rid, receiver, source = result_fixture(client, configured)
    private = b'# Coordinator private draft\n'
    ws.write_workspace_file(ws.task_root(rid, receiver), 'docs/shared.md', private, 10000)
    ws.write_workspace_file(ws.task_root(rid, receiver), 'notes.md', b'Uncommitted local notes\n', 10000)
    before = ws.git(ws.task_root(rid, receiver), 'diff', 'HEAD').stdout
    output = await read_result(rid, receiver, source, 'docs/shared.md')
    assert output['content'] == '# Shared result version 1\n'
    assert output['sha256'] == hashlib.sha256(output['content'].encode()).hexdigest()
    assert output['integration_commit'] == ws.git(ws.run_root(rid) / 'integration', 'rev-parse', 'HEAD').stdout.decode().strip()
    assert ws.read_workspace_file(ws.task_root(rid, receiver), 'docs/shared.md', 10000) == private
    assert ws.read_workspace_file(ws.task_root(rid, receiver), 'notes.md', 10000) == b'Uncommitted local notes\n'
    assert ws.git(ws.task_root(rid, receiver), 'diff', 'HEAD').stdout == before


@pytest.mark.asyncio
async def test_result_reader_requires_frozen_read_file_permission_at_both_boundaries(client, configured):
    rid, receiver, source = result_fixture(client, configured)
    with session() as s:
        run, task = s.get(Run, rid), s.get(Task, rid + ':' + receiver)
        frozen = json.loads(json.dumps(run.snapshot))
        frozen['agents'][task.agent_id]['tools'].remove('read_file')
        run.snapshot = frozen
    run = engine.load(rid)
    assert 'read_work_result' not in autonomy.available_tools(run, run.snapshot['agents'][task.agent_id])
    with pytest.raises(ValueError):
        await read_result(rid, receiver, source, 'docs/shared.md')
    with pytest.raises(ValueError):
        autonomy.read_work_result(rid, receiver, {'task_key': source, 'path': 'docs/shared.md'})
    with session() as s:
        assert not s.get(Task, rid + ':' + receiver).spec.get('peer_file_reads')


@pytest.mark.asyncio
async def test_result_reader_honors_actor_file_limit_not_larger_run_limit(client, configured):
    rid, receiver, source = result_fixture(client, configured)
    revise_integrated_result(rid, receiver, configured, content=b'x' * 2048)
    with session() as s:
        run, task = s.get(Run, rid), s.get(Task, rid + ':' + receiver)
        frozen = json.loads(json.dumps(run.snapshot))
        assert frozen['runtime']['max_file_bytes'] > 2048
        frozen['agents'][task.agent_id]['runtime']['max_file_bytes'] = 1024
        run.snapshot = frozen
    # The shared result exists and fits the run budget, but not this reader's
    # tighter frozen per-Agent budget. The new route cannot relax that limit.
    with pytest.raises(ValueError):
        await read_result(rid, receiver, source, 'docs/shared.md')
    with session() as s:
        assert not s.get(Task, rid + ':' + receiver).spec.get('peer_file_reads')


@pytest.mark.asyncio
async def test_result_reader_cannot_cross_run_even_when_agent_and_project_match(client, configured):
    first, _, source = result_fixture(client, configured)
    second, receiver, _ = result_fixture(client, configured)
    assert first != second
    with pytest.raises(ValueError):
        await read_result(second, receiver, source, 'docs/shared.md')
    with session() as s:
        assert not s.get(Task, second + ':' + receiver).spec.get('peer_file_reads')


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['pending', 'running', 'done', 'waiting_decision', 'failed'])
async def test_result_reader_rejects_sources_not_integrated(client, configured, status):
    rid, receiver, source = result_fixture(client, configured)
    # Keep an existing real file so the gate, not a missing file, rejects it.
    with session() as s:
        s.get(Task, rid + ':' + source).status = status
    with pytest.raises(ValueError):
        await read_result(rid, receiver, source, 'docs/shared.md')
    with session() as s:
        assert not s.get(Task, rid + ':' + receiver).spec.get('peer_file_reads')


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['calc.py', 'docs/not-declared.md', '../outside.md', '/tmp/outside.md', '.git/config'])
async def test_result_reader_rejects_undeclared_or_unsafe_paths(client, configured, path):
    rid, receiver, source = result_fixture(client, configured)
    with pytest.raises(ValueError):
        await read_result(rid, receiver, source, path)
    with session() as s:
        assert not s.get(Task, rid + ':' + receiver).spec.get('peer_file_reads')


@pytest.mark.asyncio
async def test_result_reader_checks_safe_path_even_if_a_bad_record_declares_it(client, configured):
    rid, receiver, source = result_fixture(client, configured)
    with session() as s:
        task = s.get(Task, rid + ':' + source)
        task.result = {**task.result, 'deliverables': ['../outside.md']}
    with pytest.raises(ValueError):
        await read_result(rid, receiver, source, '../outside.md')


@pytest.mark.asyncio
async def test_ready_rejects_stale_peer_read_until_current_result_is_reread(client, configured):
    rid, receiver, source = result_fixture(client, configured)
    first_read = await read_result(rid, receiver, source, 'docs/shared.md')
    revision = revise_integrated_result(rid, receiver, configured)
    with pytest.raises(ValueError, match='已变化.*重新读取'):
        finish(rid, receiver, 'ready', evidence_task_keys=[source, revision], notes='负责人必须重新核验自己已读但后来发生变更的成果。')
    output = await read_result(rid, receiver, source, 'docs/shared.md')
    assert output['content'] == '# Shared result version 2\n'
    assert output['sha256'] != first_read['sha256']
    assert output['integration_commit'] != first_read['integration_commit']
    # Source worker's historical copy is still version 1: the new tool really
    # reads current integration, not a stale source-activation workspace.
    assert ws.read_workspace_file(ws.task_root(rid, source), 'docs/shared.md', 10000) == b'# Shared result version 1\n'
    finish(rid, receiver, 'ready', evidence_task_keys=[source, revision])
    assert engine.load(rid).status == 'running'


@pytest.mark.asyncio
@pytest.mark.parametrize('include_evidence_keys', [True, False])
async def test_unchanged_personal_copy_cannot_bypass_stale_read_by_listing_deliverable(client, configured, include_evidence_keys):
    rid, receiver, source = result_fixture(client, configured)
    await read_result(rid, receiver, source, 'docs/shared.md')
    revision = revise_integrated_result(rid, receiver, configured)
    # The private copy is still v1 and has no actual edits. Renaming an old
    # peer result as "my deliverable" must not disable evidence freshness.
    assert ws.read_workspace_file(ws.task_root(rid, receiver), 'docs/shared.md', 10000) == b'# Shared result version 1\n'
    assert not ws.git(ws.task_root(rid, receiver), 'diff', 'HEAD').stdout
    with pytest.raises(ValueError, match='已变化.*重新读取'):
        finish(rid, receiver, 'ready', deliverables=['docs/shared.md'],
            evidence_task_keys=[source, revision] if include_evidence_keys else [])


@pytest.mark.asyncio
async def test_personal_edits_keep_valid_receipt_when_integration_has_not_changed(client, configured):
    rid, receiver, source = result_fixture(client, configured)
    await read_result(rid, receiver, source, 'docs/shared.md')
    revised = b'# Coordinator explicitly revised and verified this deliverable\n'
    ws.write_workspace_file(ws.task_root(rid, receiver), 'docs/shared.md', revised, 10000)
    finish(rid, receiver, 'ready', deliverables=['docs/shared.md'], evidence_task_keys=[source])
    assert ws.read_workspace_file(ws.run_root(rid) / 'integration', 'docs/shared.md', 10000) == revised


@pytest.mark.asyncio
async def test_personal_edits_do_not_exempt_stale_evidence_and_reread_never_overwrites_them(client, configured):
    rid, receiver, source = result_fixture(client, configured)
    await read_result(rid, receiver, source, 'docs/shared.md')
    revision = revise_integrated_result(rid, receiver, configured)
    private = b'# Coordinator draft requiring an explicit later merge\n'
    ws.write_workspace_file(ws.task_root(rid, receiver), 'docs/shared.md', private, 10000)
    with pytest.raises(ValueError, match='已变化.*重新读取'):
        finish(rid, receiver, 'ready', deliverables=['docs/shared.md'], evidence_task_keys=[source, revision])
    latest = await read_result(rid, receiver, source, 'docs/shared.md')
    assert latest['content'] == '# Shared result version 2\n'
    assert ws.read_workspace_file(ws.task_root(rid, receiver), 'docs/shared.md', 10000) == private
    # Freshness can now validate. Deliberately do not claim that overlapping
    # edits have merged: the separate integration conflict gate still applies.
    with session() as s:
        run, task = s.get(Run, rid), s.get(Task, rid + ':' + receiver)
        result = engine.finish_result(run, task, run.snapshot['agents'][task.agent_id],
            {'summary': '已核对新的共享成果，个人变更仍待安全集成。', 'outcome': 'ready',
             'deliverables': ['docs/shared.md'], 'evidence_task_keys': [source, revision]})
        assert result['outcome'] == 'ready'
    assert ws.read_workspace_file(ws.run_root(rid) / 'integration', 'docs/shared.md', 10000) == b'# Shared result version 2\n'
