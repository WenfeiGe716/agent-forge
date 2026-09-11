from copy import deepcopy

import pytest
from sqlalchemy import select

from conftest import run_body
from forge import api
from forge.db import Agent, PlatformConfig, session
from forge.design_center import DesignAsset, DesignAudit, DesignRevision
from forge.design_schema import OfficeConfig
from forge.organization_presets import ACTOR, MARKER
from test_organization_layout import confirm, create


def sync_positions(config):
    config['positions'] = {desk['agent_id']: {key: desk[key] for key in ('x', 'z', 'floor', 'zone')}
                           for desk in config['desks'] if desk.get('agent_id') is not None}
    return config


def team_layout(member_ids=('alice', 'bob')):
    config = {'positions': {}, 'floors': [
        {'floor': 1, 'name': '业务楼层', 'department': '业务部', 'zones': [
            {'id': 'west', 'name': '产品区', 'kind': 'work', 'x': -6, 'z': -2, 'width': 10, 'depth': 10},
            {'id': 'east', 'name': '设计区', 'kind': 'work', 'x': 6, 'z': -2, 'width': 10, 'depth': 10},
            {'id': 'meet', 'name': '会议区', 'kind': 'meeting', 'x': -6, 'z': 5.5, 'width': 10, 'depth': 3},
            {'id': 'common', 'name': '公共区', 'kind': 'common', 'x': 6, 'z': 5.5, 'width': 10, 'depth': 3}]}],
        'teams': [
            {'id': 'product', 'name': '产品团队', 'floor': 1, 'zone_ids': ['west'], 'headcount': 2, 'member_ids': [member_ids[0]]},
            {'id': 'design', 'name': '设计团队', 'floor': 1, 'zone_ids': ['east'], 'headcount': 1, 'member_ids': [member_ids[1]]}],
        'desks': [
            {'id': 'p1', 'name': '产品桌1', 'floor': 1, 'zone': 'west', 'x': -8, 'z': -3, 'team_id': 'product', 'agent_id': member_ids[0]},
            {'id': 'p2', 'name': '产品桌2', 'floor': 1, 'zone': 'west', 'x': -4, 'z': -3, 'team_id': 'product', 'agent_id': None},
            {'id': 'd1', 'name': '设计桌1', 'floor': 1, 'zone': 'east', 'x': 4, 'z': -3, 'team_id': 'design', 'agent_id': member_ids[1]},
            {'id': 'd2', 'name': '空桌', 'floor': 1, 'zone': 'east', 'x': 8, 'z': -3, 'team_id': None, 'agent_id': None}],
    }
    return sync_positions(config)


def test_two_teams_share_floor_but_not_work_zones_and_empty_desks_are_real():
    parsed = OfficeConfig.model_validate(team_layout())
    assert len(parsed.desks) == 4 and len(parsed.positions) == 2
    assert [team.headcount for team in parsed.teams] == [2, 1]
    assert parsed.desks[-1].team_id is None and parsed.desks[-1].agent_id is None
    assert {team.floor for team in parsed.teams} == {1}


@pytest.mark.parametrize('mutation', [
    lambda c: c['desks'][1].update(id='p1'),
    lambda c: c['desks'][1].update(name=' '),
    lambda c: c['desks'][1].update(x=-8),  # vacant desk still collides
    lambda c: c['desks'][1].update(x=-10),  # full chair/desk outside zone
    lambda c: c['desks'][1].update(z=2),
    lambda c: c['desks'][1].update(floor=2),
    lambda c: c['desks'][1].update(floor=True),
    lambda c: c['desks'][1].update(zone='meet', z=5.5),
    lambda c: c['desks'][1].update(team_id='missing'),
    lambda c: c['desks'][1].update(team_id='design'),
    lambda c: c['desks'][1].update(agent_id='alice'),
    lambda c: c['desks'][3].update(agent_id='third'),
    lambda c: c['teams'][0].update(id='design'),
    lambda c: c['teams'][0].update(name=' '),
    lambda c: c['teams'][0].update(floor=2),
    lambda c: c['teams'][0].update(floor=True),
    lambda c: c['teams'][0].update(zone_ids=['meet']),
    lambda c: c['teams'][0].update(zone_ids=['west', 'west']),
    lambda c: c['teams'][0].update(zone_ids=['west', 'east']),
    lambda c: c['teams'][0].update(headcount=True),
    lambda c: c['teams'][0].update(headcount=0),
    lambda c: c['teams'][0].update(headcount=3),
    lambda c: c['teams'][0].update(member_ids=['alice', 'alice']),
    lambda c: c['teams'][0].update(member_ids=['alice', 'bob']),
    lambda c: c['teams'][0].update(member_ids=['alice', 'third']),
    lambda c: c['teams'][0].update(member_ids=[]),
])
def test_invalid_desks_or_team_contract_is_rejected(mutation):
    config = team_layout()
    mutation(config)
    sync_positions(config)
    with pytest.raises(ValueError):
        OfficeConfig.model_validate(config)


def test_positions_cannot_disagree_with_occupied_desks():
    config = team_layout()
    config['positions']['alice']['x'] = -7
    with pytest.raises(ValueError, match='positions'):
        OfficeConfig.model_validate(config)


def test_explicit_empty_desks_can_clear_layout_but_none_keeps_legacy_semantics():
    assert OfficeConfig.model_validate({'positions': {}, 'desks': []}).desks == []
    with pytest.raises(ValueError):
        OfficeConfig.model_validate({'positions': {}, 'desks': None})
    config = team_layout()
    config['desks'] = None
    with pytest.raises(ValueError, match='旧版'):
        OfficeConfig.model_validate(config)
    legacy = OfficeConfig.model_validate({'positions': {'old': {'x': 1, 'z': 2, 'floor': 3, 'zone': '旧版区域'}}})
    assert legacy.desks is None and legacy.teams == [] and legacy.positions['old'].floor == 3


def test_multi_team_apply_moves_only_existing_occupied_members_and_grants_no_permissions(client):
    before = client.get('/api/agents').json()
    config = team_layout([agent['id'] for agent in before[:2]])
    asset = create(client, config)
    assert confirm(client, asset).status_code == 200
    assert client.get('/api/agents').json() == before
    response = confirm(client, asset, 'apply-office')
    assert response.status_code == 200, response.text
    after = client.get('/api/agents').json()
    assert len(after) == len(before)  # Empty desks and headcount never make Agents.
    for original, current in zip(before, after):
        if original['id'] in config['positions']:
            assert current['department'] == '业务部'
            assert current['office_position'] == config['positions'][original['id']]
            assert {k: v for k, v in current.items() if k not in {'version', 'department', 'office_position'}} == {
                k: v for k, v in original.items() if k not in {'version', 'department', 'office_position'}}
        else:
            assert current == original
    assert client.get('/api/organization').json()['config'] == config


@pytest.mark.parametrize('disabled_after_publish', [False, True])
def test_explicit_desks_validate_members_at_publication_and_application(client, disabled_after_publish):
    agents = client.get('/api/agents').json()
    config = team_layout([agent['id'] for agent in agents[:2]])
    asset = create(client, config)
    if disabled_after_publish:
        assert confirm(client, asset).status_code == 200
    with session() as s:
        member = s.get(Agent, agents[0]['id'])
        member.config = {**member.config, 'enabled': False}
    before = client.get('/api/agents').json()
    response = confirm(client, asset, 'apply-office' if disabled_after_publish else 'publish')
    assert response.status_code == 422 and '不存在或停用' in response.text
    assert client.get('/api/agents').json() == before


def test_production_default_bootstrap_is_audited_editable_and_keeps_agent_identity(client, monkeypatch):
    before = client.get('/api/agents').json()
    monkeypatch.delenv('FORGE_BOOTSTRAP_DEFAULT_ORGANIZATION')
    api.bootstrap()
    organization = client.get('/api/organization').json()
    config, ref = organization['config'], organization['ref']
    assert len(config['desks']) == 12 and len(config['positions']) == 4
    assert config['layout_version'] == 2
    assert len(config['floors']) == 1 and config['floors'][0]['department'] == '研发部'
    assert [zone['kind'] for zone in config['floors'][0]['zones']].count('work') == 4
    assert config['teams'][0]['headcount'] == 4
    assert set(config['teams'][0]['member_ids']) == {agent['id'] for agent in before}
    after = client.get('/api/agents').json()
    for old, new in zip(before, after):
        assert {k: v for k, v in old.items() if k not in {'department', 'office_position', 'version'}} == {
            k: v for k, v in new.items() if k not in {'department', 'office_position', 'version'}}
    with session() as s:
        revision = s.get(DesignRevision, f'{ref["id"]}:1')
        assert revision.status == 'published' and revision.source == 'manual'
        assert revision.created_by == revision.published_by == ACTOR
        assert revision.generation['kind'] == 'builtin_preset'
        assert revision.generation['automatic_ai_approval'] is False
        assert revision.generation['human_review_performed'] is False
        actions = list(s.scalars(select(DesignAudit.action).where(DesignAudit.asset_id == ref['id'])))
        assert 'bootstrap' in actions and 'applied' in actions and 'published' not in actions
        assert s.get(PlatformConfig, MARKER)
    api.bootstrap()
    assert client.get('/api/organization').json() == organization
    assert client.get('/api/agents').json() == after
    edited = client.put('/api/design/assets/' + ref['id'], json={
        'name': '管理员编辑后的布局', 'config': config, 'expected_version': 1})
    assert edited.status_code == 200 and edited.json()['status'] == 'draft'
    assert client.get('/api/organization').json() == organization


def test_bootstrap_missing_seed_members_leaves_empty_desks_and_ignores_extra_agents(client, monkeypatch):
    before = client.get('/api/agents').json()
    with session() as s:
        for member in list(s.scalars(select(Agent))):
            if member.config['name'] == 'Atlas': continue
            member.config = {**member.config, 'enabled': False}
        # An unrelated verification member is never assigned to the preset.
        s.add(Agent(config={**{key: deepcopy(value) for key, value in before[0].items() if key not in {'id', 'version'}},
                            'name': 'Verification member', 'enabled': True}))
    ids = {agent['id'] for agent in client.get('/api/agents').json()}
    monkeypatch.delenv('FORGE_BOOTSTRAP_DEFAULT_ORGANIZATION')
    api.bootstrap()
    config = client.get('/api/organization').json()['config']
    assert len(config['desks']) == 12 and config['teams'][0]['headcount'] == 4
    assert len(config['positions']) == 1
    assert sum(desk['agent_id'] is None for desk in config['desks']) == 11
    assert {agent['id'] for agent in client.get('/api/agents').json()} == ids


@pytest.mark.parametrize('existing', ['draft_asset', 'empty_layout', 'bootstrap_marker'])
def test_bootstrap_never_overwrites_prior_layout_intent(client, monkeypatch, existing):
    before = client.get('/api/agents').json()
    if existing == 'draft_asset':
        create(client, {'positions': {}})  # Even an unfinished draft is user intent.
    else:
        with session() as s:
            if existing == 'empty_layout':
                s.add(PlatformConfig(id='organization_layout', value={
                    'ref': None, 'config': {'positions': {}, 'floors': [], 'desks': [], 'teams': []}, 'applied_at': None}))
            else:
                s.add(PlatformConfig(id=MARKER, value={'previously_initialized': True}))
    prior = client.get('/api/organization').json()
    monkeypatch.delenv('FORGE_BOOTSTRAP_DEFAULT_ORGANIZATION')
    api.bootstrap()
    assert client.get('/api/organization').json() == prior
    assert client.get('/api/agents').json() == before
    with session() as s:
        assert len(list(s.scalars(select(DesignAsset).where(DesignAsset.kind == 'office')))) == (1 if existing == 'draft_asset' else 0)


def test_explicit_desks_and_team_staffing_are_frozen_in_existing_runs(client, configured):
    config = team_layout([agent['id'] for agent in configured['agents'][:2]])
    # Seat all four run participants explicitly before creating their run.
    third, fourth = [agent['id'] for agent in configured['agents'][2:]]
    config['desks'][1]['agent_id'] = third
    config['desks'][3].update(agent_id=fourth, team_id='design')
    config['teams'][0]['member_ids'].append(third)
    config['teams'][1]['member_ids'].append(fourth)
    config['teams'][1]['headcount'] = 2
    sync_positions(config)
    asset = create(client, config)
    assert confirm(client, asset).status_code == 200
    assert confirm(client, asset, 'apply-office').status_code == 200
    response = client.post('/api/runs', json=run_body(configured))
    assert response.status_code == 202, response.text
    run_id = response.json()['id']
    frozen = client.get('/api/runs/' + run_id).json()['snapshot']
    assert frozen['design']['organization']['config'] == config
    changed = deepcopy(config)
    changed['desks'] = [desk for desk in changed['desks'] if desk['id'] != 'p2']
    changed['teams'][0]['headcount'] = 1
    changed['teams'][0]['member_ids'].remove(third)
    sync_positions(changed)
    revision = client.put('/api/design/assets/' + asset['id'], json={
        'name': '调整桌数和编制', 'config': changed, 'expected_version': 1}).json()
    assert confirm(client, revision).status_code == 200
    assert confirm(client, revision, 'apply-office').status_code == 200
    assert client.get('/api/organization').json()['config'] == changed
    assert client.get('/api/runs/' + run_id).json()['snapshot'] == frozen


def test_intentionally_cleared_explicit_layout_stays_empty_after_bootstrap(client, monkeypatch):
    config = {'positions': {}, 'floors': [], 'teams': [], 'desks': []}
    asset = create(client, config)
    assert confirm(client, asset).status_code == 200
    before = client.get('/api/agents').json()
    assert confirm(client, asset, 'apply-office').status_code == 200
    current = client.get('/api/organization').json()
    assert current['config'] == config
    monkeypatch.delenv('FORGE_BOOTSTRAP_DEFAULT_ORGANIZATION')
    api.bootstrap()
    assert client.get('/api/organization').json() == current
    assert client.get('/api/agents').json() == before
