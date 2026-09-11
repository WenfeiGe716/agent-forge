"""New assignments require real applied desks, without a global busy mutex."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent, get_ident
from types import SimpleNamespace

import pytest
from sqlalchemy import event, func, select

from conftest import run_body, seat_agents
from forge import engine
from forge.db import Agent, Event, Outbox, PlatformConfig, Run, session
from test_execution_readiness import ready_environment


def remove_member_seat(agent_id):
    with session() as s:
        row = s.get(PlatformConfig, 'organization_layout')
        layout = deepcopy(row.value)
        layout['config']['positions'].pop(agent_id, None)
        for desk in layout['config']['desks']:
            if desk['agent_id'] == agent_id:
                desk['agent_id'] = None
        for team in layout['config']['teams']:
            team['member_ids'] = [aid for aid in team['member_ids'] if aid != agent_id]
        row.value = layout


def counts():
    with session() as s:
        return tuple(s.scalar(select(func.count()).select_from(table)) for table in (Run, Event, Outbox))


@pytest.mark.parametrize('endpoint', ['/api/runs', '/api/runs/preview-config', '/api/execution/preflight'])
def test_new_run_requires_applied_desk_even_when_agent_has_a_position_hint(client, configured, ready_environment, endpoint):
    agent = configured['agents'][0]
    remove_member_seat(agent['id'])
    # The removed member retains its old position hint; that cannot create a desk.
    assert client.get('/api/agents').json()[0]['office_position']
    before, config = counts(), client.get('/api/organization').json()
    response = client.post(endpoint, json=run_body(configured))
    if endpoint.endswith('preflight'):
        assert response.status_code == 200, response.text
        value = response.json()
        assert not value['ready'] and not value['snapshot_valid']
        failed = next(item for item in value['checks'] if item['code'] == 'agent.workstation' and item['agent_id'] == agent['id'])
        assert failed['status'] == 'error' and failed['target'] == 'organization'
        assert agent['name'] in failed['message'] and '楼层与部门' in failed['message']
        assert next(item for item in value['agents'] if item['id'] == agent['id'])['ready'] is False
    else:
        assert response.status_code == 422, response.text
        assert agent['name'] in response.json()['detail'] and '工位' in response.json()['detail']
    assert counts() == before
    assert client.get('/api/organization').json() == config


@pytest.mark.parametrize('invalid', ['missing-layout', 'positions-only', 'wrong-department', 'wrong-team', 'wrong-floor', 'wrong-zone', 'duplicate-desk'])
def test_incomplete_or_inconsistent_organization_cannot_start_new_work(client, configured, ready_environment, invalid):
    aid = configured['agents'][0]['id']
    with session() as s:
        row = s.get(PlatformConfig, 'organization_layout')
        layout = deepcopy(row.value)
        if invalid == 'missing-layout':
            s.delete(row)
        elif invalid == 'wrong-department':
            agent = s.get(Agent, aid)
            agent.config = {**agent.config, 'department': '另一个部门'}
        else:
            if invalid == 'positions-only':
                layout['config'].pop('desks')
                layout['config']['teams'] = []
            else:
                desk = next(d for d in layout['config']['desks'] if d['agent_id'] == aid)
                if invalid == 'wrong-team': desk['team_id'] = 'nonexistent'
                elif invalid == 'wrong-floor': desk['floor'] = 9
                elif invalid == 'wrong-zone': desk['zone'] = 'fixture_meeting_1'
                else: layout['config']['desks'].append({**desk, 'id': 'duplicate_occupied'})
            row.value = layout
    before = counts()
    assert client.post('/api/runs', json=run_body(configured)).status_code == 422
    result = client.post('/api/execution/preflight', json=run_body(configured)).json()
    assert not result['ready']
    assert any(item['code'] == 'agent.workstation' and item['status'] == 'error' for item in result['checks'])
    assert counts() == before


def test_published_but_unapplied_layout_is_not_authority(client, configured):
    layout = client.get('/api/organization').json()
    with session() as s:
        s.delete(s.get(PlatformConfig, 'organization_layout'))
    assert client.get('/api/design/assets/' + layout['ref']['id']).json()['status'] == 'published'
    assert client.post('/api/runs', json=run_body(configured)).status_code == 422


def test_seats_are_frozen_evidence_and_not_a_parallel_run_lock(client, configured, ready_environment):
    body = run_body(configured)
    first = client.post('/api/runs', json=body)
    assert first.status_code == 202, first.text
    with session() as s:
        s.get(Run, first.json()['id']).status = 'running'
    second = client.post('/api/runs', json={**body, 'title': 'Second independent task'})
    assert first.status_code == second.status_code == 202
    first_id, second_id = first.json()['id'], second.json()['id']
    assert first_id != second_id
    frozen = client.get('/api/runs/' + first_id).json()['snapshot']
    assert set(frozen['design']['workstations']) == set(body['agent_ids'])
    for aid, seat in frozen['design']['workstations'].items():
        assert seat['desk_id'] and seat['team_id'] and seat['department']
        assert seat['layout_ref'] == frozen['design']['organization']['ref']
    before = deepcopy(frozen)
    remove_member_seat(body['agent_ids'][0])
    assert client.get('/api/runs/' + first_id).json()['snapshot'] == before
    # Runtime preparation/resume has no live-seat gate and preserves run/task roots.
    engine.prepare_run({'run_id': first_id})
    engine.prepare_run({'run_id': second_id})
    from forge.autonomy import autonomy_tick
    first_activation = autonomy_tick({'run_id': first_id})['runnable'][0]['key']
    second_activation = autonomy_tick({'run_id': second_id})['runnable'][0]['key']
    from forge.workspace import task_root
    first_root, second_root = task_root(first_id, first_activation), task_root(second_id, second_activation)
    assert first_root != second_root and first_root.is_dir() and second_root.is_dir()
    (first_root / 'run-specific-note.txt').write_text('Only the first assignment owns this file.')
    assert not (second_root / 'run-specific-note.txt').exists()
    assert client.post('/api/runs/' + first_id + '/control', json={'action': 'pause'}).status_code == 202
    assert client.post('/api/runs', json=body).status_code == 422


def test_retrying_same_creation_idempotency_key_preserves_existing_run_after_layout_changes(client, configured):
    body = run_body(configured)
    headers = {'Idempotency-Key': 'same-confirmed-work'}
    first = client.post('/api/runs', json=body, headers=headers)
    assert first.status_code == 202
    remove_member_seat(body['agent_ids'][0])
    second = client.post('/api/runs', json=body, headers=headers)
    assert second.status_code == 202 and second.json()['id'] == first.json()['id']


def test_legacy_frozen_run_without_workstation_metadata_can_still_prepare_and_resume(client, configured):
    created = client.post('/api/runs', json=run_body(configured)).json()
    with session() as s:
        run = s.get(Run, created['id'])
        legacy = deepcopy(run.snapshot)
        legacy.pop('design', None)
        for member in legacy['agents'].values():
            member.pop('office_position', None)
            member.pop('department', None)
        run.snapshot = legacy
        s.delete(s.get(PlatformConfig, 'organization_layout'))
    engine.prepare_run({'run_id': created['id']})
    assert client.post('/api/runs/' + created['id'] + '/control', json={'action': 'resume'}).status_code == 202
    detail = client.get('/api/runs/' + created['id']).json()
    assert detail['snapshot'] == legacy and detail['calls'] == 0


def test_agent_position_hint_does_not_replace_the_applied_desk_evidence(client, configured):
    aid = configured['agents'][0]['id']
    with session() as s:
        agent = s.get(Agent, aid)
        agent.config = {**agent.config, 'office_position': {'x': 11, 'z': 7, 'floor': 9, 'zone': 'unapplied_hint'}}
    result = client.post('/api/runs/preview-config', json=run_body(configured))
    assert result.status_code == 200, result.text
    desk = result.json()['design']['workstations'][aid]
    assert desk['floor'] == 1 and desk['position']['zone'] == 'fixture_work_1'
    assert desk['position'] != result.json()['agents'][aid]['office_position']


def test_reapplying_explicit_seat_configuration_restores_readiness_without_model_calls(client, configured, ready_environment, monkeypatch):
    async def forbidden(*args, **kwargs): raise AssertionError('Desk preflight must not call inference')
    monkeypatch.setattr(engine.providers, 'complete', forbidden)
    remove_member_seat(configured['agents'][0]['id'])
    assert not client.post('/api/execution/preflight', json=run_body(configured)).json()['ready']
    seat_agents(client)
    result = client.post('/api/execution/preflight', json=run_body(configured)).json()
    assert result['ready'] and result['snapshot_valid']
    assert all(item['workstation'] for item in result['agents'])


def test_run_list_returns_only_safe_frozen_membership_metadata_and_keeps_acl(client, configured):
    body = run_body(configured)
    first = client.post('/api/runs', json=body).json()
    selected = body['agent_ids'][:2]
    second = client.post('/api/runs', json={**body, 'agent_ids': selected, 'entry_agent_id': selected[0]}).json()
    rows = {row['id']: row for row in client.get('/api/runs').json()}
    assert rows[first['id']]['agent_ids'] == body['agent_ids'] and rows[first['id']]['mode'] == 'autonomous'
    assert rows[second['id']]['agent_ids'] == selected and rows[second['id']]['mode'] == 'autonomous'
    for rid, row in rows.items():
        detail = client.get('/api/runs/' + rid).json()
        for key in ('agent_ids', 'mode', 'execution_strategy', 'run_project_checks', 'entry_agent_id', 'planner_id', 'reviewer_id'):
            assert row[key] == detail[key]
        assert {'snapshot', 'models', 'credential_revision', 'config', 'connection'}.isdisjoint(row)
    assert client.post('/api/users', json={'username': 'seat-observer', 'password': 'test-seat-observer-password', 'role': 'viewer'}).status_code == 201
    client.post('/api/auth/logout')
    client.post('/api/auth/login', json={'username': 'seat-observer', 'password': 'test-seat-observer-password'})
    assert client.get('/api/runs').json() == []
    assert client.get('/api/runs/' + first['id']).status_code in (403, 404)


def test_member_only_run_creation_waits_for_pending_layout_application(client, configured, monkeypatch):
    from forge import api, design_center, organization
    from forge.db import engine as db_engine
    from forge.design_schema import AssetConfirmation
    from forge.schema import RunInput
    from test_organization_layout import create, confirm
    config = deepcopy(client.get('/api/organization').json()['config'])
    aid = configured['agents'][0]['id']
    config['positions'].pop(aid)
    for desk in config['desks']:
        if desk['agent_id'] == aid: desk['agent_id'] = None
    for team in config['teams']:
        team['member_ids'] = [member for member in team['member_ids'] if member != aid]
    asset = create(client, config)
    assert confirm(client, asset).status_code == 200
    held, release, attempted = ThreadEvent(), ThreadEvent(), ThreadEvent()
    owner = []
    original = design_center.resolve_published

    def hold_application(s, ref):
        owner.append(get_ident())
        held.set()
        assert release.wait(5)
        return original(s, ref)

    def observe(connection, cursor, statement, parameters, context, many):
        if held.is_set() and owner != [get_ident()] and statement.lower().startswith('update platform_config '):
            attempted.set()

    def apply_next():
        with session() as s:
            return organization.apply_layout(s, asset['id'], AssetConfirmation(
                version=asset['version'], digest=asset['digest'], comment='Concurrency fixture'), 'fixture')

    monkeypatch.setattr(design_center, 'resolve_published', hold_application)
    event.listen(db_engine, 'before_cursor_execute', observe)
    before = counts()[0]
    try:
        with ThreadPoolExecutor() as pool:
            applying = pool.submit(apply_next)
            assert held.wait(5)
            # No office_team_id: explicit members must use the same seat lock.
            creating = pool.submit(api.create_run, RunInput(**run_body(configured)), SimpleNamespace(id='fixture', role='admin'), None)
            try:
                assert attempted.wait(5)
                assert not creating.done()
            finally:
                release.set()
            assert applying.result(5)['applied']
            with pytest.raises(ValueError, match='没有唯一的正式工位'):
                creating.result(5)
    finally:
        release.set()
        event.remove(db_engine, 'before_cursor_execute', observe)
    assert counts()[0] == before
