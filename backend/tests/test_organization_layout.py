from copy import deepcopy

import pytest

from conftest import run_body
from forge.db import Agent, session
from forge.design_schema import OfficeConfig


def layout(agents):
    return {'description': '默认一层一个部门', 'floors': [
        {'floor': 1, 'name': '产品楼层', 'department': '产品部', 'zones': [
            {'id': 'product_work', 'name': '产品工作区', 'kind': 'work', 'x': 0, 'z': -2, 'width': 22, 'depth': 10, 'color': '#7296ab'},
            {'id': 'product_meeting', 'name': '会议区', 'kind': 'meeting', 'x': 5.75, 'z': 5.5, 'width': 10.5, 'depth': 3},
            {'id': 'product_common', 'name': '公共区', 'kind': 'common', 'x': -5.75, 'z': 5.5, 'width': 10.5, 'depth': 3}]},
        {'floor': 2, 'name': '设计楼层', 'department': '设计部', 'zones': [
            {'id': 'design_work', 'name': '设计工作区', 'kind': 'work', 'x': 0, 'z': -2, 'width': 22, 'depth': 10}]}
    ], 'positions': {agent['id']: {'x': -8 + i * 4, 'z': -3, 'floor': i % 2 + 1,
                                    'zone': 'product_work' if i % 2 == 0 else 'design_work'}
                      for i, agent in enumerate(agents)}}


def create(client, config):
    response = client.post('/api/design/assets', json={'kind': 'office', 'name': '组织布局', 'config': config})
    assert response.status_code == 201, response.text
    return response.json()


def confirm(client, asset, action='publish', **extra):
    return client.post(f'/api/design/assets/{asset["id"]}/{action}', json={
        'version': asset['version'], 'digest': asset['digest'], 'comment': '核对部门与工位，不变更权限', **extra})


def test_empty_current_organization_is_read_only_and_does_not_migrate_seed_agents(client):
    before = client.get('/api/agents').json()
    current = client.get('/api/organization')
    assert current.status_code == 200
    assert current.json() == {'ref': None, 'config': {'positions': {}, 'floors': [], 'description': ''}, 'applied_at': None}
    assert client.get('/api/agents').json() == before


def test_organization_publish_apply_and_runtime_freeze_are_separate(client, configured):
    before = client.get('/api/agents').json()
    before_organization = client.get('/api/organization').json()
    config = layout(before)
    # New runs require actual desks/team ownership, not legacy position hints.
    config['teams'] = [{'id': f'team_{floor["floor"]}', 'name': floor['department'] + '团队',
        'floor': floor['floor'], 'zone_ids': [z['id'] for z in floor['zones'] if z['kind'] == 'work'],
        'headcount': sum(p['floor'] == floor['floor'] for p in config['positions'].values()),
        'member_ids': [aid for aid, p in config['positions'].items() if p['floor'] == floor['floor']]}
        for floor in config['floors']]
    config['desks'] = [{'id': f'desk_{index}', 'name': f'测试桌 {index}', **position,
        'team_id': f'team_{position["floor"]}', 'agent_id': aid} for index, (aid, position) in enumerate(config['positions'].items())]
    asset = create(client, config)
    assert confirm(client, asset, 'apply-office').status_code == 422
    assert confirm(client, asset).status_code == 200
    assert client.get('/api/agents').json() == before
    assert client.get('/api/organization').json() == before_organization
    assert confirm(client, asset, 'apply-office', digest='0' * 64).status_code == 409
    applied = confirm(client, asset, 'apply-office')
    assert applied.status_code == 200, applied.text
    after = client.get('/api/agents').json()
    current = client.get('/api/organization').json()
    assert current['config'] == config
    assert current['ref'] == {key: asset[key] for key in ('id', 'version', 'digest')}
    assert 'actor' not in current
    for a, b in zip(before, after):
        assert b['department'] == ('产品部' if config['positions'][a['id']]['floor'] == 1 else '设计部')
        assert b['office_position'] == config['positions'][a['id']]
        assert b['version'] == a['version'] + 1
        assert {k: v for k, v in a.items() if k not in ('version', 'department', 'office_position')} == {
            k: v for k, v in b.items() if k not in ('version', 'department', 'office_position')}
    response = client.post('/api/runs', json=run_body(configured))
    assert response.status_code == 202, response.text
    run_id = response.json()['id']
    frozen = client.get('/api/runs/' + run_id).json()['snapshot']
    assert frozen['design']['organization'] == current
    changed = deepcopy(config)
    changed['floors'][0]['department'] = '战略产品部'
    second = client.put('/api/design/assets/' + asset['id'], json={'name': '组织布局二版', 'config': changed, 'expected_version': 1}).json()
    assert client.get('/api/organization').json() == current
    assert confirm(client, second).status_code == 200
    assert confirm(client, second, 'apply-office').status_code == 200
    assert client.get('/api/organization').json()['config'] == changed
    assert client.get('/api/runs/' + run_id).json()['snapshot'] == frozen
    preview = client.post('/api/runs/preview-config', json=run_body(configured))
    assert preview.status_code == 200, preview.text
    assert preview.json()['design']['organization']['ref']['version'] == 2


@pytest.mark.parametrize('mutation', [
    lambda c: c['floors'][1].update(floor=1),
    lambda c: c['floors'][1].update(department='产品部'),
    lambda c: c['floors'][1].update(department='  '),
    lambda c: c['floors'][1].update(department=' 设计部'),
    lambda c: c['floors'][0].update(floor=11),
    lambda c: c['floors'][0].update(floor=True),
    lambda c: c['floors'][0]['zones'][0].update(x=11),
    lambda c: c['floors'][0]['zones'][0].update(width=0),
    lambda c: c['floors'][0]['zones'][0].update(color='red'),
    lambda c: c['floors'][0]['zones'][1].update(z=0),
    lambda c: c['floors'][1]['zones'][0].update(id='product_work'),
    lambda c: c['floors'][1]['zones'][0].update(kind='common'),
    lambda c: next(iter(c['positions'].values())).update(floor=3),
    lambda c: next(iter(c['positions'].values())).update(zone='design_work'),
    lambda c: next(iter(c['positions'].values())).update(zone='product_meeting', x=5, z=5),
    lambda c: next(iter(c['positions'].values())).update(x=12),
    lambda c: next(iter(c['positions'].values())).update(x=10),
    lambda c: next(iter(c['positions'].values())).update(z=2),
    lambda c: c['positions'][list(c['positions'])[2]].update(x=-8, z=-3),
])
def test_invalid_organization_geometry_can_save_draft_but_never_publish_or_apply(client, mutation):
    before = client.get('/api/agents').json()
    config = layout(before)
    mutation(config)
    asset = create(client, config)
    assert confirm(client, asset).status_code == 422
    assert confirm(client, asset, 'apply-office').status_code == 422
    assert client.get('/api/organization').json()['ref'] is None
    assert client.get('/api/agents').json() == before


def test_same_coordinates_on_different_floors_are_valid_and_regions_may_touch():
    config = layout([{'id': 'a'}, {'id': 'b'}])
    config['positions']['b'].update(x=-8, z=-3)
    config['floors'][0]['zones'][1].update(z=4.5)
    assert OfficeConfig.model_validate(config).positions['b'].floor == 2


def test_empty_department_floor_can_publish_without_agents(client):
    config = layout([])
    asset = create(client, config)
    assert confirm(client, asset).status_code == 200
    before = client.get('/api/agents').json()
    assert confirm(client, asset, 'apply-office').status_code == 200
    assert client.get('/api/agents').json() == before


def test_legacy_position_only_layout_keeps_departments_and_is_idempotent(client):
    before = client.get('/api/agents').json()
    agent_id = before[0]['id']
    config = {'positions': {agent_id: {'x': 1, 'z': 2, 'floor': 3, 'zone': '旧版自由区域'}}}
    asset = create(client, config)
    assert confirm(client, asset).status_code == 200
    assert confirm(client, asset, 'apply-office').status_code == 200
    after = client.get('/api/agents').json()
    assert after[0]['department'] == before[0]['department']
    assert after[1:] == before[1:]
    assert confirm(client, asset, 'apply-office').json()['changed_agent_ids'] == []
    assert client.get('/api/agents').json() == after
    assert client.get('/api/organization').json()['config'] == config


@pytest.mark.parametrize('deleted', [True, False])
def test_apply_rechecks_all_members_before_any_mutation(client, deleted):
    before = client.get('/api/agents').json()
    asset = create(client, layout(before))
    assert confirm(client, asset).status_code == 200
    with session() as s:
        agent = s.get(Agent, before[-1]['id'])
        if deleted:
            s.delete(agent)
        else:
            agent.config = {**agent.config, 'enabled': False}
    latest = client.get('/api/agents').json()
    response = confirm(client, asset, 'apply-office')
    assert response.status_code == 422, response.text
    assert '不存在或停用' in response.text
    assert client.get('/api/agents').json() == latest
    assert client.get('/api/organization').json()['ref'] is None


def test_organization_reads_require_login_and_application_requires_admin(client):
    asset = create(client, layout(client.get('/api/agents').json()))
    assert confirm(client, asset).status_code == 200
    assert client.post('/api/users', json={'username': 'observer', 'password': 'test-observer-password-123', 'role': 'viewer'}).status_code == 201
    client.post('/api/auth/logout')
    assert client.get('/api/organization').status_code == 401
    assert client.post('/api/auth/login', json={'username': 'observer', 'password': 'test-observer-password-123'}).status_code == 200
    assert client.get('/api/organization').status_code == 200
    assert confirm(client, asset, 'apply-office').status_code == 403
