"""Office-team task selection is explicit, revision-bound, and frozen once."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent, get_ident
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from conftest import run_body
from forge import execution_readiness
from forge.api import app
from forge.db import Agent, Event, Outbox, Run, engine, session
from forge.organization_presets import default_layout
from test_organization_layout import create, confirm


@pytest.fixture
def office(client, configured, monkeypatch):
    with session() as s:
        config = default_layout(list(s.scalars(select(Agent))))
    asset = create(client, config)
    assert confirm(client, asset).status_code == 200
    assert confirm(client, asset, 'apply-office').status_code == 200
    async def ready():
        return {'state': 'ready', 'ready': True, 'detail': 'local fixture', 'images': []}
    monkeypatch.setattr(execution_readiness, 'sandbox_readiness', ready)
    async def no_inference(*args, **kwargs):
        raise AssertionError('Team selection/preview must not call inference')
    monkeypatch.setattr(execution_readiness.providers, 'complete', no_inference)
    app.state.dispatch_status = 'connected (fixture)'
    return {'asset': asset, 'config': config, 'ref': {k: asset[k] for k in ('id', 'version', 'digest')}}


def payload(configured, office, **changes):
    return run_body(configured, office_team_id='development_team', office_layout_ref=office['ref'], **changes)


def counts():
    with session() as s:
        return tuple(len(list(s.scalars(select(table.id)))) for table in (Run, Event, Outbox))


def selected_source(office, ids):
    team, floor = office['config']['teams'][0], office['config']['floors'][0]
    return {'id': team['id'], 'name': team['name'], 'floor': team['floor'], 'floor_name': floor['name'],
            'department': floor['department'], 'layout_ref': office['ref'],
            'member_ids': team['member_ids'], 'selected_agent_ids': ids}


def test_office_team_preflight_preview_and_create_share_selection_and_freeze_source(client, configured, office):
    # A team may contain four members while this request explicitly selects three.
    ids = [a['id'] for a in configured['agents'] if a['role'] != 'tester']
    body = payload(configured, office, agent_ids=ids)
    before_agents = client.get('/api/agents').json()
    before_layout = client.get('/api/organization').json()
    before = counts()
    preview = client.post('/api/runs/preview-config', json=body)
    assert preview.status_code == 200, preview.text
    snap = preview.json()
    assert set(snap['agents']) == set(ids)
    assert snap['design']['organization_team'] == selected_source(office, ids)
    assert snap['design']['organization'] == before_layout
    preflight = client.post('/api/execution/preflight', json=body)
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()['snapshot_valid'] and preflight.json()['run_created'] is False
    assert {a['id'] for a in preflight.json()['agents']} == set(ids)
    assert counts() == before
    response = client.post('/api/runs', json=body)
    assert response.status_code == 202, response.text
    stored = client.get('/api/runs/' + response.json()['id']).json()['snapshot']
    assert stored['design']['organization_team'] == snap['design']['organization_team']
    assert set(stored['agents']) == set(ids)
    assert client.get('/api/agents').json() == before_agents
    assert client.get('/api/organization').json() == before_layout


@pytest.mark.parametrize('endpoint', ['/api/runs', '/api/runs/preview-config', '/api/execution/preflight'])
@pytest.mark.parametrize('invalid', ['missing-team', 'outsider', 'stale-id', 'stale-version', 'stale-digest', 'no-applied-layout'])
def test_invalid_office_team_selection_never_creates_or_silently_replaces_members(client, configured, office, endpoint, invalid):
    body = payload(configured, office)
    if invalid == 'missing-team': body['office_team_id'] = 'missing_team'
    elif invalid == 'outsider':
        original = configured['agents'][0]
        outsider = client.post('/api/agents', json={**{k: v for k, v in original.items() if k not in ('id', 'version')}, 'name': 'Unassigned outsider'})
        assert outsider.status_code == 201, outsider.text
        body['agent_ids'][0] = outsider.json()['id']
    elif invalid.startswith('stale-'):
        field = invalid.removeprefix('stale-')
        body['office_layout_ref'] = {**office['ref'], field: 2 if field == 'version' else '0' * (64 if field == 'digest' else 32)}
    else:
        from forge.db import PlatformConfig
        with session() as s: s.delete(s.get(PlatformConfig, 'organization_layout'))
    before, before_agents = counts(), client.get('/api/agents').json()
    response = client.post(endpoint, json=body)
    if endpoint.endswith('preflight'):
        assert response.status_code == 200, response.text
        assert not response.json()['snapshot_valid'] and not response.json()['ready']
        assert any(c['code'] == 'run.snapshot' and c['status'] == 'error' for c in response.json()['checks'])
    else:
        assert response.status_code in (409, 422), response.text
    assert counts() == before
    assert client.get('/api/agents').json() == before_agents


@pytest.mark.parametrize('mutation', ['missing-ref', 'ref-only', 'team-template', 'empty-members', 'missing-members'])
def test_team_input_contract_rejects_ambiguous_or_empty_selection(client, configured, office, mutation):
    body = payload(configured, office)
    if mutation == 'missing-ref': body.pop('office_layout_ref')
    elif mutation == 'ref-only': body.pop('office_team_id')
    elif mutation == 'team-template': body['team_ref'] = {'id': office['ref']['id'], 'version': 1}
    elif mutation == 'empty-members': body['agent_ids'] = []
    else: body.pop('agent_ids')
    before = counts()
    for endpoint in ['/api/runs', '/api/runs/preview-config', '/api/execution/preflight']:
        assert client.post(endpoint, json=body).status_code == 422
    assert counts() == before


def test_empty_applied_team_is_not_expanded_to_the_organization(client, configured, office):
    changed = deepcopy(office['config'])
    changed['teams'][0]['member_ids'] = []
    changed['positions'] = {}
    for desk in changed['desks']: desk['agent_id'] = None
    asset = create(client, changed)
    assert confirm(client, asset).status_code == 200
    assert confirm(client, asset, 'apply-office').status_code == 200
    body = payload(configured, office)
    body['office_layout_ref'] = {k: asset[k] for k in ('id', 'version', 'digest')}
    before = counts()
    response = client.post('/api/runs/preview-config', json=body)
    assert response.status_code == 422 and '成员' in response.json()['detail']
    assert counts() == before


def test_office_selection_never_bypasses_disabled_or_missing_model_checks(client, configured, office):
    with session() as s:
        row = s.get(Agent, configured['agents'][0]['id'])
        row.config = {**row.config, 'model_profile_id': None}
    response = client.post('/api/execution/preflight', json=payload(configured, office))
    assert response.status_code == 200, response.text
    assert not response.json()['ready'] and not response.json()['snapshot_valid']
    assert any(c['code'] == 'model.bound' and c['status'] == 'error' for c in response.json()['checks'])


def test_later_layout_changes_do_not_rewrite_frozen_team_or_follow_unapplied_drafts(client, configured, office):
    response = client.post('/api/runs', json=payload(configured, office))
    assert response.status_code == 202, response.text
    run_id = response.json()['id']
    frozen = client.get('/api/runs/' + run_id).json()['snapshot']
    changed = deepcopy(office['config'])
    changed['teams'][0]['name'] = 'Human edited team'
    changed['floors'][0]['department'] = 'Human edited department'
    asset = client.put('/api/design/assets/' + office['ref']['id'], json={
        'name': 'Updated office', 'config': changed, 'expected_version': 1}).json()
    assert confirm(client, asset).status_code == 200
    preview = client.post('/api/runs/preview-config', json=payload(configured, office))
    assert preview.status_code == 200, preview.text
    assert preview.json()['design']['organization_team'] == frozen['design']['organization_team']
    assert confirm(client, asset, 'apply-office').status_code == 200
    assert client.post('/api/runs/preview-config', json=payload(configured, office)).status_code == 409
    assert client.get('/api/runs/' + run_id).json()['snapshot'] == frozen


def test_no_office_selection_still_requires_explicit_agents_without_team_provenance(client, configured, office):
    body = run_body(configured)
    preview = client.post('/api/runs/preview-config', json=body)
    assert preview.status_code == 200
    assert 'organization_team' not in preview.json()['design']
    body['agent_ids'] = []
    assert client.post('/api/runs/preview-config', json=body).status_code == 422


def test_pending_layout_application_cannot_race_past_team_confirmation(client, configured, office, monkeypatch):
    from forge import api, design_center, organization
    from forge.design_schema import AssetConfirmation
    from forge.schema import RunInput
    changed = deepcopy(office['config'])
    changed['teams'][0]['name'] = 'Concurrently applied team'
    asset = create(client, changed)
    assert confirm(client, asset).status_code == 200
    held, release, attempted = ThreadEvent(), ThreadEvent(), ThreadEvent()
    owner = []
    original = design_center.resolve_published

    def hold_application(s, ref):
        owner.append(get_ident())
        held.set()
        assert release.wait(5)
        return original(s, ref)

    def observe_creation_lock(connection, cursor, statement, parameters, context, many):
        if held.is_set() and owner != [get_ident()] and statement.lower().startswith('update platform_config '):
            attempted.set()

    def apply_next():
        with session() as s:
            return organization.apply_layout(s, asset['id'], AssetConfirmation(
                version=asset['version'], digest=asset['digest'], comment='Concurrency fixture'), 'fixture')

    monkeypatch.setattr(design_center, 'resolve_published', hold_application)
    event.listen(engine, 'before_cursor_execute', observe_creation_lock)
    before = counts()[0]
    try:
        with ThreadPoolExecutor() as pool:
            applying = pool.submit(apply_next)
            assert held.wait(5)
            creating = pool.submit(api.create_run, RunInput(**payload(configured, office)), SimpleNamespace(id='fixture', role='admin'), None)
            try:
                assert attempted.wait(5), 'Run creation must use the organization application lock'
                assert not creating.done()
            finally:
                release.set()
            assert applying.result(5)['applied']
            with pytest.raises(HTTPException) as rejected:
                creating.result(5)
            assert rejected.value.status_code == 409
    finally:
        release.set()
        event.remove(engine, 'before_cursor_execute', observe_creation_lock)
    assert counts()[0] == before
