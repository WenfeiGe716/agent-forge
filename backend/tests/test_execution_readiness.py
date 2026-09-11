import copy

import httpx
import pytest
from sqlalchemy import select, func

from forge import execution_readiness as readiness
from forge.api import app
from forge.db import Agent, ModelProfile, Project, Run, Event, session
from conftest import run_body


@pytest.fixture
def ready_environment(monkeypatch):
    async def probe():
        return {'state': 'ready', 'ready': True, 'detail': 'verified fixture', 'runtime': 'runsc',
                'network': 'none', 'images': [{'name': 'agent-forge/sandbox:local', 'available': True}]}
    monkeypatch.setattr(readiness, 'sandbox_readiness', probe)
    app.state.dispatch_status = 'connected (local journal)'
    return probe


def test_inventory_reports_unbound_members_and_missing_project_without_inference(client, monkeypatch, ready_environment):
    async def forbidden(*args, **kwargs): raise AssertionError('Readiness must not call a model')
    monkeypatch.setattr(readiness.providers, 'complete', forbidden)
    r = client.get('/api/execution/readiness')
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body['ready'] and body['inference_tested'] is False
    assert len(body['agents']) == 4
    assert len([c for c in body['checks'] if c['code'] == 'model.bound' and c['status'] == 'error']) == 4
    assert any(c['code'] == 'project.selected' and c['status'] == 'error' for c in body['checks'])


def test_preflight_validates_snapshot_but_does_not_create_runs_events_or_invoke(client, configured, monkeypatch, ready_environment):
    async def forbidden(*args, **kwargs): raise AssertionError('No inference during preflight')
    monkeypatch.setattr(readiness.providers, 'complete', forbidden)
    with session() as s: before = s.scalar(select(func.count()).select_from(Event))
    r = client.post('/api/execution/preflight', json=run_body(configured))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body['ready'] and body['snapshot_valid'] and body['run_created'] is False
    assert body['requires_sandbox'] and body['inference_tested'] is False
    assert any(c['code'] == 'model.live_probe' and c['status'] == 'warning' for c in body['checks'])
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Run)) == 0
        assert s.scalar(select(func.count()).select_from(Event)) == before


def test_preflight_accumulates_binding_model_parameter_and_skill_issues(client, configured, ready_environment):
    with session() as s:
        a = s.get(Agent, configured['agents'][0]['id']); c = copy.deepcopy(a.config); c['model_profile_id'] = None; a.config = c
        m = s.get(ModelProfile, configured['models'][1]); c = copy.deepcopy(m.config); c.update(enabled=False, supports_tools=False, parameters={'forged_parameter': 1}); m.config = c
    body = client.post('/api/execution/preflight', json=run_body(configured)).json()
    errors = {c['code'] for c in body['checks'] if c['status'] == 'error'}
    assert not body['ready'] and not body['snapshot_valid']
    assert {'model.bound', 'model.enabled', 'model.tools', 'model.parameters', 'run.snapshot'} <= errors


def test_autonomous_member_without_command_permission_does_not_require_command_environment(client, configured, ready_environment, monkeypatch):
    async def missing(): return {'state': 'unavailable', 'ready': False, 'detail': '404 fixture'}
    monkeypatch.setattr(readiness, 'sandbox_readiness', missing)
    member = next(a for a in configured['agents'] if a['role'] == 'planner')
    r = client.post('/api/execution/preflight', json=run_body(configured, agent_ids=[member['id']]))
    assert r.status_code == 200, r.text
    assert r.json()['ready'] and not r.json()['requires_sandbox']
    assert not r.json()['requires_command_environment']
    assert 'run_command' not in r.json()['agents'][0]['tools']


def test_wrong_project_image_blocks_command_run(client, configured, ready_environment):
    with session() as s:
        project = s.get(Project, configured['project']['id']); config = copy.deepcopy(project.config)
        config['sandbox_image'] = 'unknown/not-prepared:1'; project.config = config
    r = client.post('/api/execution/preflight', json=run_body(configured)).json()
    assert not r['ready']
    assert any(c['code'] == 'sandbox.ready' and c['status'] == 'error' for c in r['checks'])


def test_runner_unavailable_blocks_and_does_not_queue(client, configured, ready_environment):
    app.state.dispatch_status = 'unavailable: another runner owns lock'
    body = client.post('/api/execution/preflight', json=run_body(configured)).json()
    assert not body['ready'] and body['snapshot_valid']
    assert not body['runner']['ready']


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body,ready', [
    (404, {'detail': 'unrelated application'}, False),
    (401, {}, False),
    (200, {'ready': True, 'service': 'another-app'}, False),
    (200, {'ready': True, 'service': 'sandbox-broker', 'runtime': 'runc', 'network': 'none', 'images': [{'name': 'x', 'available': True}]}, False),
    (200, {'ready': True, 'service': 'sandbox-broker', 'runtime': 'runsc', 'network': 'bridge', 'images': [{'name': 'x', 'available': True}]}, False),
    (200, {'ready': True, 'service': 'sandbox-broker', 'runtime': 'runsc', 'network': 'none', 'images': []}, False),
    (200, {'ready': True, 'service': 'sandbox-broker', 'runtime': 'runsc', 'network': 'none', 'images': [{'name': 'x', 'available': True}]}, True),
])
async def test_sandbox_readiness_requires_authentic_runsc_network_none_and_image(monkeypatch, status, body, ready):
    real = httpx.AsyncClient
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(status, json=body)
    monkeypatch.setattr(readiness.httpx, 'AsyncClient', lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(respond)))
    result = await readiness.sandbox_readiness()
    assert result['ready'] is ready
    assert len(seen) == 1 and seen[0].method == 'GET'
    assert seen[0].url.path == '/readiness'


def test_readiness_unknown_project_is_not_exposed(client, ready_environment):
    assert client.get('/api/execution/readiness?project_id=not-accessible').status_code == 404
