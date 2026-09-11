from copy import deepcopy

import pytest
from sqlalchemy import select

from conftest import run_body
from forge import api, organization_presets as presets
from forge.db import Agent, PlatformConfig, session
from forge.design_center import DesignAsset, DesignAudit, DesignRevision
from forge.design_schema import OfficeConfig
from test_organization_layout import create, confirm


def rich_layout():
    return presets.default_layout([])


def test_default_floor_has_varied_required_facilities_and_twelve_real_desks():
    layout = rich_layout()
    assert layout['layout_version'] == 2
    assert len(layout['desks']) == 12
    assert layout['teams'][0]['headcount'] == 4 and layout['teams'][0]['member_ids'] == []
    assert all(desk['agent_id'] is None for desk in layout['desks'])
    zones = {zone['id']: zone for zone in layout['floors'][0]['zones']}
    assert {zone['kind'] for zone in zones.values()} == {'work', 'meeting', 'lounge', 'restroom', 'pantry'}
    assert {zone['name'] for zone in zones.values()} == {
        '代码规划与架构', '开发工位', '测试质量', '需求分析', '卫生间', '饮水间', '休息区', '协作会议室'}
    assert zones['development_atlas']['width'] == 8
    assert zones['development_nova']['depth'] == 6
    assert zones['development_meeting']['width'] == 11.5
    assert zones['development_restroom']['width'] == 3.5
    desks = {desk['id']: desk for desk in layout['desks']}
    assert desks['desk_atlas']['x'] == -9.9 and desks['desk_atlas']['z'] == -6.3
    assert desks['desk_sage']['zone'] == 'development_echo'
    assert sum(desk['zone'] == 'development_nova' for desk in layout['desks']) == 4
    assert sum(desk['zone'] == 'development_sage' for desk in layout['desks']) == 3
    OfficeConfig.model_validate(layout)


@pytest.mark.parametrize('kind', ['meeting', 'restroom', 'pantry'])
@pytest.mark.parametrize('operation', ['remove', 'retype'])
def test_new_floors_cannot_remove_or_retype_last_required_facility(kind, operation):
    config = rich_layout()
    if operation == 'remove':
        config['floors'][0]['zones'] = [zone for zone in config['floors'][0]['zones'] if zone['kind'] != kind]
    else:
        next(zone for zone in config['floors'][0]['zones'] if zone['kind'] == kind)['kind'] = 'common'
    with pytest.raises(ValueError, match='每层必须保留'):
        OfficeConfig.model_validate(config)


@pytest.mark.parametrize('kind,width,depth', [
    ('meeting', 3.9, 3), ('meeting', 4, 2.9), ('lounge', 2.9, 3),
    ('lounge', 3, 2.9), ('restroom', 2.9, 3), ('restroom', 3, 2.9),
    ('pantry', 2.9, 3), ('pantry', 3, 2.9),
])
def test_v2_facility_minimum_size_fits_furniture(kind, width, depth):
    config = rich_layout()
    next(zone for zone in config['floors'][0]['zones'] if zone['kind'] == kind).update(width=width, depth=depth)
    with pytest.raises(ValueError, match='尺寸不足'):
        OfficeConfig.model_validate(config)


@pytest.mark.parametrize('kind,width,depth', [('meeting', 4, 3), ('lounge', 3, 3), ('restroom', 3, 3), ('pantry', 3, 3)])
def test_v2_facility_exact_minimum_is_valid(kind, width, depth):
    config = rich_layout()
    next(zone for zone in config['floors'][0]['zones'] if zone['kind'] == kind).update(width=width, depth=depth)
    OfficeConfig.model_validate(config)


def test_facility_requirement_is_per_floor_not_satisfied_by_a_different_department():
    config = rich_layout()
    second = deepcopy(config['floors'][0])
    second.update(floor=2, name='运营楼层', department='运营部')
    for zone in second['zones']: zone['id'] = 'operations_' + zone['id']
    second['zones'] = [zone for zone in second['zones'] if zone['kind'] != 'pantry']
    config['floors'].append(second)
    with pytest.raises(ValueError, match='每层必须保留'):
        OfficeConfig.model_validate(config)


def test_legacy_version_without_new_facilities_remains_valid_and_cleared_v2_stays_valid():
    legacy = presets._legacy_default_layout([])
    assert 'layout_version' not in legacy
    assert OfficeConfig.model_validate(legacy).layout_version == 1
    assert OfficeConfig.model_validate({'layout_version': 2, 'positions': {}, 'desks': [], 'floors': []}).desks == []
    legacy['floors'][0]['zones'][4].update(width=2, depth=2)
    OfficeConfig.model_validate(legacy)  # v1 meeting sizes are not retroactively tightened.
    with pytest.raises(ValueError):
        OfficeConfig.model_validate({'layout_version': 2, 'positions': {'old': {'x': 0, 'z': 0}}})


@pytest.mark.parametrize('version', [True, False, 1.0, 2.0, '2', 0, 3, None])
def test_layout_version_is_an_explicit_supported_integer(version):
    config = rich_layout(); config['layout_version'] = version
    with pytest.raises(ValueError): OfficeConfig.model_validate(config)


def seed_old_builtin(client, monkeypatch):
    with monkeypatch.context() as historical:
        historical.delenv('FORGE_BOOTSTRAP_DEFAULT_ORGANIZATION')
        historical.setattr(presets, 'PRESET_ID', presets.LEGACY_PRESET_ID)
        historical.setattr(presets, 'default_layout', presets._legacy_default_layout)
        api.bootstrap()
    organization = client.get('/api/organization').json()
    assert len(organization['config']['desks']) == 4
    return organization


def test_untouched_builtin_v1_upgrades_as_new_revision_without_mutating_old_runs(client, configured_unseated, monkeypatch):
    configured = configured_unseated
    original = seed_old_builtin(client, monkeypatch)
    old_ref = original['ref']
    agents_before = client.get('/api/agents').json()
    response = client.post('/api/runs', json=run_body(configured))
    assert response.status_code == 202, response.text
    run_id = response.json()['id']
    snapshot = client.get('/api/runs/' + run_id).json()['snapshot']
    monkeypatch.delenv('FORGE_BOOTSTRAP_DEFAULT_ORGANIZATION')
    api.bootstrap()
    upgraded = client.get('/api/organization').json()
    assert upgraded['ref']['id'] == old_ref['id'] and upgraded['ref']['version'] == 2
    assert upgraded['ref']['digest'] != old_ref['digest']
    assert upgraded['config']['layout_version'] == 2 and len(upgraded['config']['desks']) == 12
    assert len(upgraded['config']['positions']) == 4
    assert sum(desk['agent_id'] is None for desk in upgraded['config']['desks']) == 8
    for before, after in zip(agents_before, client.get('/api/agents').json()):
        assert {key: value for key, value in before.items() if key not in {'department', 'office_position', 'version'}} == {
            key: value for key, value in after.items() if key not in {'department', 'office_position', 'version'}}
    with session() as s:
        old = s.get(DesignRevision, f'{old_ref["id"]}:1')
        assert old.config == original['config'] and old.digest == old_ref['digest'] and old.status == 'published'
        latest = s.get(DesignRevision, f'{old_ref["id"]}:2')
        assert latest.generation['upgraded_from'] == old_ref and latest.published_by == presets.ACTOR
        assert list(s.scalars(select(DesignAudit.action).where(DesignAudit.asset_id == old_ref['id'], DesignAudit.version == 2))) == ['bootstrap_upgrade', 'applied']
        assert s.get(PlatformConfig, presets.MARKER).value['ref'] == upgraded['ref']
    assert client.get('/api/runs/' + run_id).json()['snapshot'] == snapshot
    api.bootstrap()
    assert client.get('/api/organization').json() == upgraded


@pytest.mark.parametrize('change', ['manual_revision', 'other_asset', 'human_application', 'agent_position', 'agent_department',
                                      'agent_disabled', 'marker_digest', 'current_digest', 'missing_marker', 'revoked', 'changed_content'])
def test_builtin_upgrade_never_overwrites_human_changes_or_inconsistent_history(client, monkeypatch, change):
    original = seed_old_builtin(client, monkeypatch)
    ref = original['ref']
    if change == 'manual_revision':
        response = client.put('/api/design/assets/' + ref['id'], json={'name': '人工保存草稿', 'config': original['config'], 'expected_version': 1})
        assert response.status_code == 200
    elif change == 'other_asset':
        create(client, {'positions': {}})
    elif change == 'human_application':
        assert confirm(client, ref, 'apply-office').status_code == 200
    else:
        with session() as s:
            current, marker = s.get(PlatformConfig, 'organization_layout'), s.get(PlatformConfig, presets.MARKER)
            agent = s.get(Agent, next(iter(original['config']['positions'])))
            if change == 'agent_position': agent.config = {**agent.config, 'office_position': {**agent.config['office_position'], 'x': 0}}
            elif change == 'agent_department': agent.config = {**agent.config, 'department': '人工调整部门'}
            elif change == 'agent_disabled': agent.config = {**agent.config, 'enabled': False}
            elif change == 'marker_digest': marker.value = {**marker.value, 'ref': {**ref, 'digest': '0' * 64}}
            elif change == 'current_digest': current.value = {**current.value, 'ref': {**ref, 'digest': '0' * 64}}
            elif change == 'missing_marker': s.delete(marker)
            elif change == 'revoked':
                s.get(DesignRevision, f'{ref["id"]}:1').status = 'revoked'
                s.get(DesignAsset, ref['id']).published_version = None
            else:
                revision = s.get(DesignRevision, f'{ref["id"]}:1')
                revision.config = {**revision.config, 'description': 'manually changed persisted content'}
    before, people = client.get('/api/organization').json(), client.get('/api/agents').json()
    monkeypatch.delenv('FORGE_BOOTSTRAP_DEFAULT_ORGANIZATION')
    api.bootstrap()
    assert client.get('/api/organization').json() == before
    assert client.get('/api/agents').json() == people
    with session() as s:
        assert s.get(DesignAsset, ref['id']).latest_version == (2 if change == 'manual_revision' else 1)


def test_explicit_test_opt_out_also_prevents_builtin_upgrade(client, monkeypatch):
    original = seed_old_builtin(client, monkeypatch)
    api.bootstrap()
    assert client.get('/api/organization').json() == original
