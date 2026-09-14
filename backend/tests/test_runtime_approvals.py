import asyncio
from copy import deepcopy

import pytest

from forge.db import Outbox, Run, RuntimeApproval, Task, session, now
from forge.runtime_bridge import RuntimeEvent


def codex_run(client, configured):
    response = client.post('/api/runs', json={
        'project_id': configured['project']['id'], 'title': 'Runtime approval contract',
        'request': '执行需要用户批准的命令并继续同一个回合',
        'agent_ids': [a['id'] for a in configured['agents']],
        'execution_strategy': 'autonomous',
        'entry_agent_id': next(a['id'] for a in configured['agents'] if a['role'] == 'planner'),
    })
    assert response.status_code == 202, response.text
    run_id = response.json()['id']
    with session() as db:
        run = db.get(Run, run_id)
        snapshot = deepcopy(run.snapshot)
        snapshot['execution_engine'] = 'codex'
        for agent in snapshot['agents'].values():
            agent['models'][0]['connection']['protocol'] = 'openai_responses'
        run.snapshot = snapshot
    return run_id


@pytest.mark.asyncio
@pytest.mark.parametrize('approved', [True, False])
async def test_command_approval_http_decision_reaches_same_live_turn(client, configured, monkeypatch, approved):
    run_id = codex_run(client, configured)
    replies = []

    class FakeCodex:
        def __init__(self, **kwargs):
            self.thread_id, self.turn_id = 'thread-live', 'turn-live'
            self.client = self

        async def respond(self, request_id, result):
            replies.append((request_id, result))

        def has_server_request(self, request_id):
            return request_id == 17 and not replies

        async def stream(self, prompt, *, event_sink=None, thread_id=None):
            event = RuntimeEvent('item/commandExecution/requestApproval', None,
                self.thread_id, self.turn_id, 'command-1', 1,
                {'threadId': self.thread_id, 'turnId': self.turn_id, 'itemId': 'command-1',
                 'command': 'python -m unittest', 'cwd': '/fixture/project', 'reason': '测试权限'}, 17)
            event_sink(event)
            yield event
            listed = client.get(f'/api/runs/{run_id}/runtime-approvals')
            assert listed.status_code == 200, listed.text
            row = listed.json()['items'][0]
            assert row['status'] == 'pending' and row['task_key'] == 'codex'
            decision = {'digest': row['digest'], 'approved': approved, 'comment': '核对本次命令'}
            response = client.post(f'/api/runs/{run_id}/runtime-approvals/{row["id"]}/decision', json=decision)
            assert response.status_code == 202, response.text
            assert not replies, 'HTTP 接受决定不能冒充已经送达运行时'
            await executor.deliver_approval(row['id'])
            assert replies == [(17, {'decision': 'accept' if approved else 'decline'})]
            assert client.get(f'/api/runs/{run_id}/runtime-approvals').json()['items'][0]['status'] == 'delivered'
            assert client.get(f'/api/runs/{run_id}').json()['status'] == 'running'
            with session() as db:
                assert db.query(Outbox).filter_by(run_id=run_id, action='runtime_approval').count() == 1
            done = RuntimeEvent('turn/completed', None, self.thread_id, self.turn_id, None, 2,
                                {'turn': {'status': 'completed'}})
            event_sink(done)
            yield done

    monkeypatch.setattr('forge.codex_scheduler.CodexSingleTask', FakeCodex)
    from forge.codex_scheduler import CodexRunExecutor
    executor = CodexRunExecutor(run_id)
    assert await executor.run() == {'status': 'completed'}


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['cancelled', 'canceled', 'error', 'unknown'])
async def test_unknown_or_cancelled_runtime_terminal_never_means_success(client, configured, monkeypatch, terminal):
    run_id = codex_run(client, configured)

    class FakeCodex:
        def __init__(self, **kwargs): pass
        async def stream(self, prompt, *, event_sink=None, thread_id=None):
            event = RuntimeEvent('turn/completed', None, 'thread', 'turn', None, 1,
                                 {'turn': {'status': terminal}})
            event_sink(event)
            yield event

    monkeypatch.setattr('forge.codex_scheduler.CodexSingleTask', FakeCodex)
    from forge.codex_scheduler import CodexRunExecutor
    result = await CodexRunExecutor(run_id).run()
    assert result['status'] != 'completed'
    with session() as db:
        assert db.get(Task, f'{run_id}:codex').status != 'done'


def test_expired_callback_pauses_original_task_for_explicit_continue(client, configured):
    run_id = codex_run(client, configured)
    with session() as db:
        run = db.get(Run, run_id)
        task = Task(id=f'{run_id}:codex', run_id=run_id, key='codex', kind='leader',
                    agent_id=run.snapshot['entry_agent_id'], title='负责人', spec={},
                    status='pending', messages=[], result={}, base_commit='', started_at='')
        db.add(task)
        run.status = task.status = 'awaiting_approval'
        db.add(RuntimeApproval(
            id='approval-expire', run_id=run_id, task_key='codex',
            agent_id=task.agent_id, connection_id='dead-connection',
            request_id={'value': 1}, thread_id='thread-dead', turn_id='turn-dead',
            item_id='command-dead', kind='item/commandExecution/requestApproval',
            payload={'command': 'true'}, digest='a' * 64, status='pending',
            decision={}, error='', created_at=now(), updated_at=now()))
    from forge.runtime_approvals import expire_requests
    expire_requests(connection_id='dead-connection')
    with session() as db:
        assert db.get(Run, run_id).status == 'paused'
        assert db.get(Task, f'{run_id}:codex').status == 'paused'
        assert db.get(RuntimeApproval, 'approval-expire').status == 'expired'
