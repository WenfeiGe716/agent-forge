"""Real task files, SQLite journal and tool dispatch; only model responses are fixtures."""
import asyncio
import json
import hashlib
import zipfile
import pytest
from sqlalchemy import select
from forge import engine, providers, tools, autonomy, workspace as ws
from forge.db import session, Run, Task, Event, AgentMessage
from forge.local_runtime import LocalRuntime
from forge.workflow_core import DevelopmentFlow
from conftest import run_body


def new_run(client, configured, members=None, **overrides):
    members = members or configured['agents']
    lead = next((a for a in members if a['role'] == 'planner'), members[0])
    body = run_body(configured, execution_strategy='autonomous', entry_agent_id=lead['id'],
                    agent_ids=[a['id'] for a in members], **overrides)
    response = client.post('/api/runs', json=body)
    assert response.status_code == 202, response.text
    return response.json()['id'], lead['id']


def initial(rid):
    engine.prepare_run({'run_id': rid})
    engine.set_stage({'run_id': rid, 'status': 'running'})
    state = autonomy.autonomy_tick({'run_id': rid, 'tick': 0})
    return state['runnable'][0]['key']


def finish(rid, key, outcome='done', **extra):
    with session() as s:
        task, run = s.get(Task, rid + ':' + key), s.get(Run, rid)
        result = engine.finish_result(run, task, run.snapshot['agents'][task.agent_id],
            {'summary': '实际工作结果', 'notes': '', 'outcome': outcome, **extra})
        task.result, task.status = result, 'done'
    engine.integrate_task({'run_id': rid, 'key': key})


def test_directory_is_frozen_and_scoped_without_credentials(client, configured):
    members = configured['agents'][:2]
    rid, lead = new_run(client, configured, members)
    key = initial(rid)
    directory = autonomy.inspect_team(rid, key, {})
    assert {m['id'] for m in directory['members']} == {m['id'] for m in members}
    assert all('tools' in m and 'responsibilities' in m and 'skills' in m for m in directory['members'])
    encoded = json.dumps(directory)
    assert 'secret-key' not in encoded and 'encrypted_key' not in encoded and 'credential_revision' not in encoded
    for unknown in [[], {}, 'outside']:
        with pytest.raises(ValueError): autonomy.inspect_team(rid, key, {'member_id': unknown})


def test_delegate_requires_coordinator_and_bidirectional_permission(client, configured):
    rid, lead = new_run(client, configured)
    key = initial(rid)
    recipient = next(a['id'] for a in configured['agents'] if a['id'] != lead)
    request = {'recipient_id': recipient, 'title': '编写说明', 'instruction': '编写实际需求文档', 'acceptance': '内容覆盖用户目标'}
    with session() as s:
        run = s.get(Run, rid)
        snapshot = json.loads(json.dumps(run.snapshot))
        snapshot['agents'][recipient]['communication'] = {'scope': 'custom', 'recipients': []}
        run.snapshot = snapshot
    with pytest.raises(ValueError, match='通信范围'): autonomy.assign_work(rid, key, 'work-1', request)
    with session() as s: assert len(list(s.scalars(select(Task).where(Task.run_id == rid)))) == 1


def test_peer_request_wakes_once_after_sender_yields_and_preserves_files(client, configured):
    rid, lead = new_run(client, configured)
    key = initial(rid)
    recipient = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    from forge.collaboration import send_message
    first = send_message(rid, key, 'request-1', recipient, '请核对 docs/input.md', True)
    assert send_message(rid, key, 'request-1', recipient, '请核对 docs/input.md', True)['wake_task_key'] == first['wake_task_key']
    with pytest.raises(ValueError, match='是否请求回复'):
        send_message(rid, key, 'request-1', recipient, '请核对 docs/input.md', False)
    waiting = autonomy.autonomy_tick({'run_id': rid, 'tick': 1})
    assert [r['key'] for r in waiting['runnable']] == [key]
    ws.write_workspace_file(ws.task_root(rid, key), 'docs/input.md', b'actual input', 10000)
    finish(rid, key, 'waiting')
    state = autonomy.autonomy_tick({'run_id': rid, 'tick': 2})
    assert state['runnable'] == [{'key': first['wake_task_key'], 'agent_id': recipient}]
    assert ws.read_workspace_file(ws.task_root(rid, first['wake_task_key']), 'docs/input.md', 10000) == b'actual input'
    with session() as s:
        run, task = s.get(Run, rid), s.get(Task, rid + ':' + first['wake_task_key'])
        assert len(autonomy.inbox(run, task)) == 1
    finish(rid, first['wake_task_key'], notes='已实际核对输入文档，内容完整。')
    next_state = autonomy.autonomy_tick({'run_id': rid, 'tick': 3})
    assert next_state['runnable'][0]['agent_id'] == lead
    assert next_state['runnable'][0]['key'] != key


def test_information_only_does_not_wake_or_loop(client, configured):
    rid, lead = new_run(client, configured)
    key = initial(rid)
    recipient = next(a['id'] for a in configured['agents'] if a['id'] != lead)
    from forge.collaboration import send_message
    assert send_message(rid, key, 'fyi', recipient, '仅告知，无需回复', False)['wake_task_key'] is None
    with session() as s: assert len(list(s.scalars(select(Task).where(Task.run_id == rid)))) == 1


def test_ready_requires_real_evidence_and_no_outstanding_work(client, configured):
    member = next(a for a in configured['agents'] if a['role'] == 'developer')
    rid, lead = new_run(client, configured, [member])
    key = initial(rid)
    with pytest.raises(ValueError, match='完成声明'): finish(rid, key, 'ready')
    with pytest.raises(ValueError): finish(rid, key, 'ready', deliverables=['missing.md'])
    with pytest.raises(ValueError): finish(rid, key, 'ready', deliverables=['../outside'])
    ws.write_workspace_file(ws.task_root(rid, key), 'result.md', b'# Real deliverable\n', 10000)
    finish(rid, key, 'ready', deliverables=['result.md'])
    state = autonomy.autonomy_tick({'run_id': rid, 'tick': 1})
    assert len(state['candidate']['digest']) == 64
    run = engine.load(rid)
    assert run.status != 'completed'
    assert run.report['checks']['verification'] == 'not_requested'
    assert run.report['deliverables'][0]['sha256'] == hashlib.sha256(b'# Real deliverable\n').hexdigest()
    with zipfile.ZipFile(ws.run_root(rid) / 'artifacts/source.zip') as archive:
        assert archive.read('result.md') == b'# Real deliverable\n'


@pytest.mark.asyncio
async def test_model_tools_team_handoff_real_files_and_human_acceptance(client, configured, monkeypatch):
    lead = next(a for a in configured['agents'] if a['role'] == 'planner')
    dev = next(a for a in configured['agents'] if a['role'] == 'developer')
    rid, leader_id = new_run(client, configured, [lead, dev], request='写一份实际的需求文档，交付 docs/requirements.md，不修改 calc.py。')
    seen_tools = []

    async def model(connection, model, messages, definitions, secret, timeout):
        context = json.loads(messages[1]['content'])
        spec = context['task']
        calls = [c for m in messages if m['role'] == 'assistant' for c in m.get('calls', [])]
        names = [c['name'] for c in calls]
        if spec['assignment_type'] == 'coordination' and not spec.get('message_ids'):
            if not names: name, args = 'inspect_team', {}
            elif 'assign_work' not in names:
                name, args = 'assign_work', {'recipient_id': dev['id'], 'title': '形成需求文档', 'instruction': '创建 docs/requirements.md，说明目标和验收要求，不改 calc.py。', 'acceptance': '文档实际存在并包含目标和验收要求'}
            else: name, args = 'finish', {'outcome': 'waiting', 'summary': '已分配文档工作，等待真实成果。'}
        elif spec['assignment_type'] == 'delegation':
            if not names: name, args = 'write_file', {'path': 'docs/requirements.md', 'content': '# 需求\n\n目标：形成需求文档。\n验收：文档包含目标与验收要求。\n'}
            else: name, args = 'finish', {'outcome': 'done', 'summary': '需求文档已写入。', 'deliverables': ['docs/requirements.md']}
        else:
            if not names: name, args = 'read_file', {'path': 'docs/requirements.md'}
            else: name, args = 'finish', {'outcome': 'ready', 'summary': '已核对文档，内容覆盖目标与验收要求。', 'deliverables': ['docs/requirements.md']}
        assert name in {d['name'] for d in definitions}
        seen_tools.append(name)
        return providers.Reply('', [{'id': 'fixture-call-' + str(len(seen_tools)), 'name': name, 'arguments': args}], 10, 10)

    monkeypatch.setattr(providers, 'complete', model)
    async def no_fixed_checks(*args, **kwargs): raise AssertionError('Document task must not run inherited project tests')
    monkeypatch.setattr(engine, 'check_project', no_fixed_checks)
    runtime = LocalRuntime(rid, asyncio.Semaphore(3))
    flow = DevelopmentFlow(runtime)

    async def human_accepts(predicate):
        run = engine.load(rid)
        assert run.status == 'awaiting_acceptance', run.error
        assert run.artifact_digest
        response = client.post('/api/runs/' + rid + '/decision/acceptance', json={'digest': run.artifact_digest, 'approved': True, 'note': ''})
        assert response.status_code == 202, response.text
        flow.decide({'command_id': 'test-human-acceptance', 'phase': 'acceptance', 'digest': run.artifact_digest, 'approved': True})
        assert predicate()

    runtime.wait_condition = human_accepts
    result = await flow.run(rid)
    run = engine.load(rid)
    assert result == {'status': 'completed'}, run.error
    assert seen_tools == ['inspect_team', 'assign_work', 'finish', 'write_file', 'finish', 'read_file', 'finish']
    assert ws.read_workspace_file(ws.run_root(rid) / 'integration', 'calc.py', 10000) == b'def add(a, b):\n    return a - b\n'
    with session() as s:
        tasks = list(s.scalars(select(Task).where(Task.run_id == rid)))
        assert len(tasks) == 3 and all(t.kind == 'autonomous' for t in tasks)
        assert s.scalar(select(Event).where(Event.run_id == rid, Event.kind == 'agent.message.delivered'))
        assert not s.scalar(select(Event).where(Event.run_id == rid, Event.kind == 'meeting.opened'))
        assert not s.scalar(select(Event).where(Event.run_id == rid, Event.kind == 'plan.ready'))
