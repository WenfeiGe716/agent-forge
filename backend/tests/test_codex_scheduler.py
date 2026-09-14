from forge.config import settings
from types import SimpleNamespace
from copy import deepcopy
from forge.db import Outbox, Run, Task, session
from forge.runtime_bridge import RuntimeEvent
import pytest


def _set_codex_scheduler(value: bool):
    # Settings is intentionally frozen; tests temporarily model the process
    # feature flag without mutating environment or user Codex configuration.
    previous = settings.codex_default_scheduler_enabled
    object.__setattr__(settings, 'codex_default_scheduler_enabled', value)
    return previous


def test_new_run_pins_codex_engine_when_feature_flag_enabled(client, configured):
    previous = _set_codex_scheduler(True)
    try:
        response = client.post('/api/runs', json={
            'project_id': configured['project']['id'],
            'title': 'Codex scheduler flag',
            'request': '仅验证新运行快照选择 Codex 入口',
            'agent_ids': [a['id'] for a in configured['agents']],
            'execution_strategy': 'autonomous',
            'entry_agent_id': next(a['id'] for a in configured['agents'] if a['role'] == 'planner'),
        })
        assert response.status_code == 202, response.text
        assert response.json()['execution_engine'] == 'codex'
    finally:
        object.__setattr__(settings, 'codex_default_scheduler_enabled', previous)


def test_feature_flag_off_keeps_legacy_engine(client, configured):
    previous = _set_codex_scheduler(False)
    try:
        response = client.post('/api/runs', json={
            'project_id': configured['project']['id'],
            'title': 'Legacy scheduler flag',
            'request': '仅验证关闭开关时保留旧运行入口',
            'agent_ids': [a['id'] for a in configured['agents']],
            'execution_strategy': 'autonomous',
            'entry_agent_id': next(a['id'] for a in configured['agents'] if a['role'] == 'planner'),
        })
        assert response.status_code == 202, response.text
        assert response.json()['execution_engine'] == 'legacy'
    finally:
        object.__setattr__(settings, 'codex_default_scheduler_enabled', previous)


def test_codex_recovery_requeues_same_visible_task_and_thread(client, configured):
    previous = _set_codex_scheduler(True)
    try:
        response = client.post('/api/runs', json={
            'project_id': configured['project']['id'], 'title': 'Codex same thread recovery',
            'request': '验证负责人在原 Thread 中继续',
            'agent_ids': [a['id'] for a in configured['agents']],
            'execution_strategy': 'autonomous',
            'entry_agent_id': next(a['id'] for a in configured['agents'] if a['role'] == 'planner'),
        })
        assert response.status_code == 202, response.text
        run_id = response.json()['id']
        with session() as db:
            run = db.get(Run, run_id)
            run.status = 'needs_attention'
            task = Task(id=f'{run_id}:codex', run_id=run_id, key='codex', kind='autonomous',
                        agent_id=run.snapshot['entry_agent_id'], title=run.title,
                        status='needs_attention', spec={'engine':'codex', 'thread_id':'thread-frozen'}, result={})
            db.add(task)
        resumed = client.post(f'/api/runs/{run_id}/control', json={
            'action':'retry', 'task_key':'codex', 'reason':'先检查失败证据后继续'})
        assert resumed.status_code == 202, resumed.text
        body = resumed.json()
        assert body['same_run'] is True and body['same_thread'] is True
        with session() as db:
            run = db.get(Run, run_id)
            assert run.status == 'queued'
            assert db.get(Task, f'{run_id}:codex').status == 'queued'
            assert db.query(Task).filter(Task.run_id == run_id).count() == 1
    finally:
        object.__setattr__(settings, 'codex_default_scheduler_enabled', previous)


def test_codex_steer_is_queued_for_the_active_turn_without_new_task(client, configured):
    previous = _set_codex_scheduler(True)
    try:
        response = client.post('/api/runs', json={
            'project_id': configured['project']['id'], 'title': 'Codex steer',
            'request': '验证追加指令边界',
            'agent_ids': [a['id'] for a in configured['agents']],
            'execution_strategy': 'autonomous',
            'entry_agent_id': next(a['id'] for a in configured['agents'] if a['role'] == 'planner'),
        })
        assert response.status_code == 202, response.text
        run_id = response.json()['id']
        with session() as db:
            run = db.get(Run, run_id)
            run.status = 'running'
            task = Task(id=f'{run_id}:codex', run_id=run_id, key='codex', kind='autonomous',
                        agent_id=run.snapshot['entry_agent_id'], title=run.title,
                        status='running', spec={'engine':'codex', 'thread_id':'thread-live'}, result={})
            db.add(task)
        steered = client.post(f'/api/runs/{run_id}/control', json={
            'action':'steer', 'prompt':'请继续检查实际测试输出'})
        assert steered.status_code == 202, steered.text
        body = steered.json()
        assert '新任务' in body['effect']
        with session() as db:
            assert db.query(Task).filter(Task.run_id == run_id).count() == 1
            command = db.query(Outbox).filter_by(run_id=run_id).order_by(Outbox.created_at.desc()).first()
            assert command.payload['action'] == 'steer'
    finally:
        object.__setattr__(settings, 'codex_default_scheduler_enabled', previous)


def test_codex_executor_has_one_visible_task_row():
    from forge.codex_scheduler import _ensure_codex_task

    run = SimpleNamespace(id='a' * 32, title='可见 Codex 任务', request='执行一次真实回合')
    _ensure_codex_task(run, 'leader-1')
    with session() as db:
        task = db.get(Task, run.id + ':codex')
        assert task is not None
        assert (task.kind, task.agent_id, task.spec['engine']) == ('autonomous', 'leader-1', 'codex')


def test_team_mcp_enabled_adds_leader_contract_without_rewriting_user_goal():
    from forge.codex_scheduler import _leader_prompt
    run = SimpleNamespace()
    prompt = _leader_prompt(run, '实现订单导出并运行测试', team_mcp=True)
    assert '实现订单导出并运行测试' in prompt
    assert 'forge_team MCP' in prompt
    assert '不要虚构成员执行' in prompt


def test_team_mcp_disabled_keeps_prompt_exact():
    from forge.codex_scheduler import _leader_prompt
    run = SimpleNamespace()
    assert _leader_prompt(run, '原始任务', team_mcp=False) == '原始任务'


@pytest.mark.asyncio
async def test_codex_executor_persists_terminal_state_for_a_new_run(client, configured, monkeypatch):
    # Build a real frozen run first, then make only the fixture's protocol
    # compatible with the fake app-server below. No provider request occurs.
    response = client.post('/api/runs', json={
        'project_id': configured['project']['id'], 'title': 'Codex executor fixture',
        'request': '验证 Codex 运行状态落库',
        'agent_ids': [a['id'] for a in configured['agents']],
        'execution_strategy': 'autonomous',
        'entry_agent_id': next(a['id'] for a in configured['agents'] if a['role'] == 'planner'),
    })
    assert response.status_code == 202, response.text
    run_id = response.json()['id']
    with session() as db:
        run = db.get(Run, run_id)
        snapshot = deepcopy(run.snapshot)
        leader = snapshot['agents'][snapshot['entry_agent_id']]
        leader['models'][0]['connection']['protocol'] = 'openai_responses'
        run.snapshot = snapshot

    class FakeCodex:
        def __init__(self, **kwargs): self.kwargs = kwargs
        async def stream(self, prompt, *, event_sink=None, thread_id=None):
            event = RuntimeEvent('turn/completed', None, 'thread-fixture', 'turn-fixture', None, 1,
                                  {'turn': {'status': 'completed'}})
            if event_sink: event_sink(event)
            yield event

    monkeypatch.setattr('forge.codex_scheduler.CodexSingleTask', FakeCodex)
    from forge.codex_scheduler import CodexRunExecutor
    result = await CodexRunExecutor(run_id).run()
    assert result == {'status': 'completed'}
    with session() as db:
        run = db.get(Run, run_id)
        task = db.get(Task, run_id + ':codex')
        assert run.status == 'completed'
        assert task.status == 'done'


@pytest.mark.asyncio
async def test_codex_team_starts_member_thread_in_isolated_workspace(client, configured, monkeypatch):
    previous_engine = _set_codex_scheduler(True)
    previous_team = settings.codex_team_mcp_enabled
    object.__setattr__(settings, 'codex_team_mcp_enabled', True)
    try:
        response = client.post('/api/runs', json={
            'project_id': configured['project']['id'], 'title': 'Codex team fixture',
            'request': '验证负责人委派后成员独立运行',
            'agent_ids': [a['id'] for a in configured['agents']],
            'execution_strategy': 'autonomous',
            'entry_agent_id': next(a['id'] for a in configured['agents'] if a['role'] == 'planner'),
        })
        assert response.status_code == 202, response.text
        run_id = response.json()['id']
        with session() as db:
            run = db.get(Run, run_id)
            snapshot = deepcopy(run.snapshot)
            for agent in snapshot['agents'].values():
                agent['models'][0]['connection']['protocol'] = 'openai_responses'
            run.snapshot = snapshot
            member_id = next(aid for aid in snapshot['agents'] if aid != snapshot['entry_agent_id'])
            db.add(Task(id=f'{run_id}:auto-member', run_id=run_id, key='auto-member',
                        kind='autonomous', agent_id=member_id, title='成员独立工作',
                        status='pending', spec={'description': '执行成员工作'}))

        class FakeCodex:
            instances = []
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.thread_id = None
                self.turn_id = None
                FakeCodex.instances.append(self)
            async def stream(self, prompt, *, event_sink=None, thread_id=None):
                event = RuntimeEvent('turn/completed', None,
                    f'thread-{len(FakeCodex.instances)}', f'turn-{len(FakeCodex.instances)}', None, 1,
                    {'turn': {'status': 'completed'}})
                if event_sink:
                    event_sink(event)
                yield event

        monkeypatch.setattr('forge.codex_scheduler.CodexSingleTask', FakeCodex)
        from forge.codex_scheduler import CodexRunExecutor
        result = await CodexRunExecutor(run_id).run()
        assert result == {'status': 'needs_attention'}
        assert len(FakeCodex.instances) == 2
        assert FakeCodex.instances[0].kwargs['cwd'] != FakeCodex.instances[1].kwargs['cwd']
        with session() as db:
            member = db.get(Task, f'{run_id}:auto-member')
            assert member.status == 'needs_attention'
            assert db.get(Run, run_id).status == 'needs_attention'
    finally:
        object.__setattr__(settings, 'codex_default_scheduler_enabled', previous_engine)
        object.__setattr__(settings, 'codex_team_mcp_enabled', previous_team)


@pytest.mark.asyncio
@pytest.mark.parametrize('use_gateway', [False, True])
async def test_codex_team_structured_member_result_is_integrated(client, configured, monkeypatch, use_gateway):
    previous_url = settings.opencodex_gateway_url
    previous_token = settings.opencodex_api_auth_token
    object.__setattr__(settings, 'opencodex_gateway_url', 'http://127.0.0.1:10100/v1' if use_gateway else '')
    object.__setattr__(settings, 'opencodex_api_auth_token', 'test-gateway-token')
    previous_engine = _set_codex_scheduler(True)
    previous_team = settings.codex_team_mcp_enabled
    object.__setattr__(settings, 'codex_team_mcp_enabled', True)
    try:
        response = client.post('/api/runs', json={
            'project_id': configured['project']['id'], 'title': 'Codex team result fixture',
            'request': '验证成员成果提交和集成',
            'agent_ids': [a['id'] for a in configured['agents']],
            'execution_strategy': 'autonomous',
            'entry_agent_id': next(a['id'] for a in configured['agents'] if a['role'] == 'planner'),
        })
        assert response.status_code == 202, response.text
        run_id = response.json()['id']
        with session() as db:
            run = db.get(Run, run_id)
            snapshot = deepcopy(run.snapshot)
            for agent in snapshot['agents'].values():
                agent['models'][0]['connection']['protocol'] = 'openai_responses'
                if use_gateway:
                    agent['models'][0]['connection']['base_url'] = settings.opencodex_gateway_url
            run.snapshot = snapshot
            member_id = next(aid for aid in snapshot['agents'] if aid != snapshot['entry_agent_id'])
            db.add(Task(id=f'{run_id}:auto-result', run_id=run_id, key='auto-result',
                        kind='autonomous', agent_id=member_id, title='成员提交真实成果',
                        status='pending', spec={'description': '生成 result.md'}))

        class FakeCodex:
            instances = []
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                FakeCodex.instances.append(self)
            async def stream(self, prompt, *, event_sink=None, thread_id=None):
                cwd = str(self.kwargs['cwd'])
                if '/tasks/' in cwd:
                    from forge import autonomy, workspace as ws
                    key = cwd.rsplit('/', 1)[-1]
                    ws.write_workspace_file(ws.task_root(run_id, key), 'result.md', b'member result\n', 10000)
                    autonomy.submit_result(run_id, key, {
                        'outcome': 'done', 'summary': '成员已经生成真实成果文件',
                        'deliverables': ['result.md'], 'notes': '',
                    })
                event = RuntimeEvent('turn/completed', None, 'thread-' + str(len(FakeCodex.instances)),
                    'turn-' + str(len(FakeCodex.instances)), None, 1,
                    {'turn': {'status': 'completed'}})
                if event_sink:
                    event_sink(event)
                yield event

        monkeypatch.setattr('forge.codex_scheduler.CodexSingleTask', FakeCodex)
        from forge.codex_scheduler import CodexRunExecutor
        result = await CodexRunExecutor(run_id).run()
        assert result == {'status': 'needs_attention'}
        assert len(FakeCodex.instances) >= 2
        assert any('/tasks/' in str(instance.kwargs['cwd']) for instance in FakeCodex.instances)
        assert any('/tasks/' not in str(instance.kwargs['cwd']) for instance in FakeCodex.instances)
        for instance in FakeCodex.instances:
            assert instance.kwargs['provider'].gateway_token == ('test-gateway-token' if use_gateway else None)
        with session() as db:
            member = db.get(Task, f'{run_id}:auto-result')
            assert member.status == 'integrated'
            assert db.get(Run, run_id).status == 'needs_attention'
        assert (settings.data_root / 'runs' / run_id / 'integration' / 'result.md').read_text() == 'member result\n'
    finally:
        object.__setattr__(settings, 'opencodex_gateway_url', previous_url)
        object.__setattr__(settings, 'opencodex_api_auth_token', previous_token)
        object.__setattr__(settings, 'codex_default_scheduler_enabled', previous_engine)
        object.__setattr__(settings, 'codex_team_mcp_enabled', previous_team)
