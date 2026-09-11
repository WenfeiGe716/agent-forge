"""A failed autonomous activation wakes the leader for evidence-bound recovery."""
import pytest
from forge.autonomous_flow import _advance_batch
from conftest import run_body
from forge import autonomy, engine
from forge.db import session, Run, Task, Event


def _run(client, configured):
    lead = next(a for a in configured['agents'] if a['role'] == 'planner')
    body = run_body(configured, execution_strategy='autonomous', entry_agent_id=lead['id'],
                    agent_ids=[a['id'] for a in configured['agents']])
    response = client.post('/api/runs', json=body)
    assert response.status_code == 202, response.text
    rid = response.json()['id']
    engine.prepare_run({'run_id': rid})
    engine.set_stage({'run_id': rid, 'status': 'running'})
    return rid


def test_failed_member_activation_is_recorded_and_leader_is_woken(client, configured):
    rid = _run(client, configured)
    first = autonomy.autonomy_tick({'run_id': rid, 'tick': 0})['runnable'][0]
    with session() as s:
        s.add(Event(run_id=rid, kind='model.started', data={'task_key': first['key'],
            'agent_id': first['agent_id'], 'provider': 'fixture-provider',
            'model': 'fixture-model', 'attempt': 2}))
    recorded = engine.record_autonomous_failure({'run_id': rid,
        'failures': [{'key': first['key'], 'error': '模型响应超时；结果不确定，不自动重放'}]})
    assert recorded['failed'] == [first['key']]

    state = autonomy.autonomy_tick({'run_id': rid, 'tick': 1})
    assert state['runnable'] and state['runnable'][0]['agent_id'] == first['agent_id']
    assert state['runnable'][0]['key'] != first['key']
    with session() as s:
        task = s.get(Task, rid + ':' + first['key'])
        assert task.status == 'failed'
        issue = task.spec['work_issue']
        assert issue['kind'] == 'member_failed' and issue['uncertain'] is True
        assert issue['provider'] == 'fixture-provider' and issue['model'] == 'fixture-model'
        assert s.scalar(__import__('sqlalchemy').select(Event).where(Event.run_id == rid, Event.kind == 'team.issue.opened'))


@pytest.mark.asyncio
async def test_batch_failure_records_only_failed_member_without_replaying_peers():
    """A rejected turn becomes evidence for the coordinator; successful peers
    keep their durable progress and are never replayed by the failure path."""
    class Runtime:
        autonomous_recovery = True

    class Flow:
        runtime = Runtime()
        settings = {'agents': {
            'leader': {'runtime': {'task_timeout_s': 30, 'call_timeout_s': 1, 'command_timeout_s': 1}},
            'worker': {'runtime': {'task_timeout_s': 30, 'call_timeout_s': 1, 'command_timeout_s': 1}},
        }}
        turns, spent = {}, {}

        def __init__(self): self.recorded = None

        async def call(self, name, args, timeout=300):
            if name == 'turn' and args['key'] == 'failed':
                raise RuntimeError('provider timeout; result unknown')
            if name == 'turn': return {'elapsed_s': 0.25}
            if name == 'record_autonomous_failure':
                self.recorded = args['failures']
                return {'failed': ['failed']}
            raise AssertionError(name)

    flow = Flow()
    await _advance_batch(flow, [
        {'key': 'failed', 'agent_id': 'leader'},
        {'key': 'ok', 'agent_id': 'worker'},
    ])
    assert flow.recorded == [{'key': 'failed', 'error': 'provider timeout; result unknown'}]
    assert flow.turns == {'ok': 1} and flow.spent == {'ok': 0.25}
