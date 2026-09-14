"""Recovery controls stay inside the failed autonomous Run."""

from sqlalchemy import select

from conftest import run_body
from forge.db import Event, Run, Task, session
from forge import engine, autonomy


def _failed_run(client, configured):
    body = run_body(configured)
    response = client.post('/api/runs', json=body)
    assert response.status_code == 202, response.text
    run_id = response.json()['id']
    engine.prepare_run({'run_id': run_id})
    engine.set_stage({'run_id': run_id, 'status': 'running'})
    key = autonomy.autonomy_tick({'run_id': run_id, 'tick': 0})['runnable'][0]['key']
    with session() as s:
        task = s.get(Task, f'{run_id}:{key}')
        task.status = 'failed'
        task.result = {'outcome': 'blocked', 'summary': '模型调用失败', 'error': 'provider timeout'}
        run = s.get(Run, run_id)
        run.status, run.error = 'needs_attention', '模型调用失败'
    return run_id, key


def test_retry_queues_new_activation_without_new_run_and_keeps_failure(client, configured):
    run_id, source_key = _failed_run(client, configured)
    response = client.post(f'/api/runs/{run_id}/control', json={
        'action': 'retry', 'task_key': source_key, 'reason': '负责人核对后重试'})
    assert response.status_code == 202, response.text
    detail = client.get(f'/api/runs/{run_id}').json()
    assert detail['id'] == run_id and detail['status'] == 'queued'
    with session() as s:
        source = s.get(Task, f'{run_id}:{source_key}')
        assert source.status == 'failed'
        recovery = [t for t in s.scalars(select(Task).where(Task.run_id == run_id))
                    if t.spec.get('recovery_of') == source_key]
        assert len(recovery) == 1
        assert recovery[0].agent_id == source.agent_id
        assert recovery[0].spec['recovery_action'] == 'retry'
        assert s.scalar(select(Event).where(Event.run_id == run_id,
                                            Event.kind == 'team.recovery.requested'))


def test_reassign_requires_team_member_and_targets_same_run(client, configured):
    run_id, source_key = _failed_run(client, configured)
    target = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    response = client.post(f'/api/runs/{run_id}/control', json={
        'action': 'reassign', 'task_key': source_key, 'agent_id': target})
    assert response.status_code == 202, response.text
    with session() as s:
        recovery = [t for t in s.scalars(select(Task).where(Task.run_id == run_id))
                    if t.spec.get('recovery_of') == source_key]
        assert recovery and recovery[0].agent_id == target


def test_recovery_activation_is_runnable_without_replaying_failed_source(client, configured):
    run_id, source_key = _failed_run(client, configured)
    response = client.post(f'/api/runs/{run_id}/control', json={
        'action': 'continue', 'task_key': source_key})
    assert response.status_code == 202, response.text
    recovery_key = response.json()['recovery_task_key']
    state = autonomy.autonomy_tick({'run_id': run_id, 'tick': 100})
    assert state['runnable'] == [{'key': recovery_key,
                                  'agent_id': next(a['id'] for a in configured['agents']
                                                   if a['role'] == 'planner')}]
    with session() as s:
        assert s.get(Task, f'{run_id}:{source_key}').status == 'failed'


def test_recovery_controls_reject_completed_run_and_unknown_task(client, configured):
    run_id, source_key = _failed_run(client, configured)
    missing = client.post(f'/api/runs/{run_id}/control', json={
        'action': 'retry', 'task_key': 'missing'})
    assert missing.status_code == 409
    with session() as s:
        s.get(Run, run_id).status = 'completed'
    completed = client.post(f'/api/runs/{run_id}/control', json={
        'action': 'retry', 'task_key': source_key})
    assert completed.status_code == 409


def test_integrated_blocker_can_be_continued_in_place(client, configured):
    run_id, source_key = _failed_run(client, configured)
    with session() as s:
        source = s.get(Task, f'{run_id}:{source_key}')
        source.status = 'integrated'
        source.result = {'outcome': 'blocked', 'summary': '集成后发现阻塞'}
    response = client.post(f'/api/runs/{run_id}/control', json={
        'action': 'continue', 'task_key': source_key})
    assert response.status_code == 202, response.text
