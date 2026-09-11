"""New autonomous authorization pools remain explicit, seated, and snapshot-bound."""
from copy import deepcopy

import pytest
from sqlalchemy import func, select

from conftest import run_body, seat_agents
from forge.db import Agent, Event, ModelProfile, Outbox, Run, session
from test_execution_readiness import ready_environment
from test_workstation_readiness import remove_member_seat


def autonomous_body(configured, ids=None, **changes):
    member = next(a for a in configured['agents'] if a['role'] == 'developer')
    return {'project_id': configured['project']['id'], 'title': '整理项目资料',
            'request': '阅读现有资料，形成带证据的项目需求说明。',
            'agent_ids': ids or [member['id']], **changes}


def counts():
    with session() as s:
        return tuple(s.scalar(select(func.count()).select_from(model)) for model in (Run, Event, Outbox))


@pytest.mark.parametrize('role', ['planner', 'developer', 'tester', 'reviewer'])
def test_default_autonomous_single_member_accepts_any_execution_role(client, configured, ready_environment, role):
    member = next(a for a in configured['agents'] if a['role'] == role)
    body = autonomous_body(configured, [member['id']])
    before = counts()
    preview = client.post('/api/runs/preview-config', json=body)
    assert preview.status_code == 200, preview.text
    snap = preview.json()
    assert snap['execution_strategy'] == 'autonomous' and snap['run_project_checks'] is False
    assert snap['collaboration_flow'] is False and snap['orchestration'] is None
    assert snap['entry_agent_id'] == snap['planner_id'] == snap['reviewer_id'] == member['id']
    assert list(snap['agents']) == [member['id']]
    assert list(snap['design']['workstations']) == [member['id']]
    preflight = client.post('/api/execution/preflight', json=body)
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()['ready'] and preflight.json()['snapshot_valid']
    assert not preflight.json()['inference_tested'] and not preflight.json()['run_created']
    assert counts() == before
    created = client.post('/api/runs', json=body)
    assert created.status_code == 202, created.text
    row = created.json()
    assert row['execution_strategy'] == row['mode'] == 'autonomous'
    assert row['entry_agent_id'] == member['id'] and row['run_project_checks'] is False
    assert set(row).isdisjoint({'snapshot', 'models', 'credential_revision', 'connection'})


def test_autonomous_leader_is_selected_inside_pool_without_fixed_profession_mix(client, configured):
    members = [a for a in configured['agents'] if a['role'] in ('developer', 'tester')]
    ids = [a['id'] for a in members]
    with session() as s:
        for agent, title in zip(members, ['财务分析师', '市场研究员']):
            row = s.get(Agent, agent['id'])
            row.config = {**row.config, 'job_title': title}
    response = client.post('/api/runs/preview-config', json=autonomous_body(configured, ids, entry_agent_id=ids[1]))
    assert response.status_code == 200, response.text
    snap = response.json()
    assert snap['entry_agent_id'] == ids[1] and set(snap['agents']) == set(ids)
    assert {a['job_title'] for a in snap['agents'].values()} == {'财务分析师', '市场研究员'}
    assert {a['role'] for a in snap['agents'].values()} == {'developer', 'tester'}


def test_autonomous_uses_only_a_configured_unique_pool_reporting_leader(client, configured):
    members = [a for a in configured['agents'] if a['role'] in ('developer', 'tester')]
    ids = [a['id'] for a in members]
    body = autonomous_body(configured, ids)
    assert client.post('/api/runs/preview-config', json=body).status_code == 422
    with session() as s:
        subordinate = s.get(Agent, ids[1])
        subordinate.config = {**subordinate.config, 'reports_to': ids[0]}
    response = client.post('/api/runs/preview-config', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['entry_agent_id'] == ids[0]
    # The unrelated planner elsewhere in the organization is never imported.
    assert set(response.json()['agents']) == set(ids)


@pytest.mark.parametrize('explicit_pool_leader', [False, True])
def test_outside_reporting_ancestor_cannot_replace_autonomous_leader(client, configured, ready_environment, explicit_pool_leader):
    """Organization ancestors remain evidence, never task-pool authorities."""
    member = next(a for a in configured['agents'] if a['role'] == 'developer')
    peer = next(a for a in configured['agents'] if a['role'] == 'tester')
    outsider = next(a for a in configured['agents'] if a['role'] == 'planner')
    ids = [member['id'], peer['id']] if explicit_pool_leader else [member['id']]
    with session() as s:
        row = s.get(Agent, member['id'])
        row.config = {**row.config, 'reports_to': outsider['id']}
    body = autonomous_body(configured, ids, **({'entry_agent_id': member['id']} if explicit_pool_leader else {}))
    before = counts()
    preview = client.post('/api/runs/preview-config', json=body)
    assert preview.status_code == 200, preview.text
    snapshot = preview.json()
    assert snapshot['entry_agent_id'] == snapshot['planner_id'] == snapshot['reviewer_id'] == member['id']
    assert set(snapshot['agents']) == set(ids)
    assert snapshot['agents'][member['id']]['reporting_chain'] == [outsider['id']]
    assert outsider['id'] not in snapshot['agents']
    preflight = client.post('/api/execution/preflight', json=body)
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()['ready'] and preflight.json()['snapshot_valid']
    assert counts() == before
    created = client.post('/api/runs', json=body)
    assert created.status_code == 202, created.text
    assert created.json()['entry_agent_id'] == member['id']
    with session() as s:
        frozen = s.get(Run, created.json()['id']).snapshot
        assert frozen['entry_agent_id'] == member['id']
        assert set(frozen['agents']) == set(ids)


def test_autonomous_pool_leader_and_directed_recipients_cannot_escape_selection(client, configured):
    chosen = next(a for a in configured['agents'] if a['role'] == 'developer')
    outsider = next(a for a in configured['agents'] if a['role'] == 'planner')
    for change in [{'entry_agent_id': outsider['id']}, {'request': '@' + outsider['id'] + ' 请完成这份文档'}]:
        response = client.post('/api/runs/preview-config', json=autonomous_body(configured, **change))
        assert response.status_code == 422, response.text
    assert counts()[0] == 0


@pytest.mark.parametrize('invalid', ['disabled', 'unseated', 'unbound', 'model-disabled', 'tools-unconfirmed', 'invalid-permission'])
def test_autonomous_pool_never_bypasses_member_readiness(client, configured, ready_environment, invalid):
    body = autonomous_body(configured)
    aid = body['agent_ids'][0]
    if invalid == 'unseated':
        remove_member_seat(aid)
    else:
        with session() as s:
            agent = s.get(Agent, aid)
            if invalid == 'disabled': agent.config = {**agent.config, 'enabled': False}
            elif invalid == 'unbound': agent.config = {**agent.config, 'model_profile_id': None}
            elif invalid == 'invalid-permission': agent.config = {**agent.config, 'tool_permissions': ['finish', 'unapproved_global_shell']}
            else:
                model = s.get(ModelProfile, agent.config['model_profile_id'])
                model.config = {**model.config, 'enabled': invalid != 'model-disabled', 'supports_tools': invalid != 'tools-unconfirmed'}
    before = counts()
    assert client.post('/api/runs/preview-config', json=body).status_code == 422
    result = client.post('/api/execution/preflight', json=body)
    assert result.status_code == 200, result.text
    assert not result.json()['ready'] and not result.json()['snapshot_valid']
    assert client.post('/api/runs', json=body).status_code == 422
    assert counts() == before


def test_autonomous_team_selection_freezes_only_confirmed_pool_and_layout_version(client, configured):
    layout = client.get('/api/organization').json()
    team = layout['config']['teams'][0]
    body = autonomous_body(configured, office_team_id=team['id'], office_layout_ref=layout['ref'])
    preview = client.post('/api/runs/preview-config', json=body)
    assert preview.status_code == 200, preview.text
    snap = preview.json()
    assert set(snap['agents']) == set(body['agent_ids'])
    assert snap['design']['organization_team']['selected_agent_ids'] == body['agent_ids']
    stale = deepcopy(body)
    stale['office_layout_ref']['version'] += 1
    assert client.post('/api/runs', json=stale).status_code == 409


def test_published_team_is_reusable_without_injecting_its_historical_fixed_graph(client, configured):
    from test_design_center import create, confirm, team
    asset = create(client, 'team', team(configured))
    assert confirm(client, asset).status_code == 200
    response = confirm(client, asset, 'instantiate')
    assert response.status_code == 201, response.text
    instance = response.json()
    seat_agents(client, instance['agent_ids'])
    body = autonomous_body(configured, instance['agent_ids'], team_ref=instance['team_ref'])
    response = client.post('/api/runs/preview-config', json=body)
    assert response.status_code == 200, response.text
    snap = response.json()
    assert snap['orchestration'] is None and snap['collaboration_flow'] is False
    assert snap['design']['team']['config']['workflow']['mode'] == 'discussion'
    assert set(snap['agents']) == set(instance['agent_ids'])
    assert snap['entry_agent_id'] == instance['member_mapping']['atlas']
    assert snap['agents'][instance['member_mapping']['nova']]['communication']['recipients'] == [instance['member_mapping']['atlas']]


def test_autonomous_routing_stays_human_versioned_without_changing_agent_configuration(client, configured):
    from test_design_center import create, confirm, routing
    asset = create(client, 'routing', routing(configured))
    assert confirm(client, asset).status_code == 200
    before = client.get('/api/agents').json()
    body = autonomous_body(configured, routing_ref={'id': asset['id'], 'version': asset['version']},
                           routing_overrides={'developer': configured['models'][0]})
    response = client.post('/api/runs/preview-config', json=body)
    assert response.status_code == 200, response.text
    member = response.json()['agents'][body['agent_ids'][0]]
    assert member['models'][0]['profile_id'] == configured['models'][0]
    assert member['model_selection']['source'] == 'task_override'
    assert client.get('/api/agents').json() == before


@pytest.mark.parametrize('fixed', ['orchestration', 'template', 'workflow'])
def test_autonomous_rejects_explicit_fixed_workflow_instead_of_overwriting_it(client, configured, fixed):
    ids = [a['id'] for a in configured['agents']]
    changes = {'orchestration': {'mode': 'development', 'participants': ids}} if fixed == 'orchestration' else {'template_id': 'fixed', 'template_version': 1} if fixed == 'template' else {'workflow_ref': {'id': 'fixed', 'version': 1}}
    response = client.post('/api/runs/preview-config', json=autonomous_body(configured, ids, entry_agent_id=ids[0], **changes))
    assert response.status_code == 422, response.text
    assert counts()[0] == 0


def test_new_run_api_rejects_removed_fixed_workflow_strategy(client, configured):
    body = run_body(configured, execution_strategy='workflow')
    response = client.post('/api/runs/preview-config', json=body)
    assert response.status_code == 422, response.text


def test_project_checks_are_opt_in_for_autonomous_read_only_capability(client, configured, ready_environment):
    member = next(a for a in configured['agents'] if a['role'] == 'planner')
    body = autonomous_body(configured, [member['id']])
    default = client.post('/api/execution/preflight', json=body)
    assert default.status_code == 200, default.text
    assert default.json()['ready'] and not default.json()['requires_command_environment']
    opted_in = client.post('/api/execution/preflight', json={**body, 'run_project_checks': True})
    assert opted_in.status_code == 200, opted_in.text
    assert opted_in.json()['ready'] and opted_in.json()['requires_command_environment']


@pytest.mark.parametrize('verification,requested,accepted', [('not_requested', False, True), ('not_requested', True, False), ('unverified', False, False), ('passed', True, True)])
def test_autonomous_acceptance_preserves_digest_and_does_not_require_reason_for_unrequested_checks(client, configured, verification, requested, accepted):
    body = autonomous_body(configured, run_project_checks=requested)
    created = client.post('/api/runs', json=body)
    assert created.status_code == 202, created.text
    rid = created.json()['id']
    with session() as s:
        row = s.get(Run, rid)
        row.status = 'awaiting_acceptance'
        row.artifact_digest = 'a' * 64
        row.report = {'mode': 'autonomous', 'checks': {'verification': verification}}
    endpoint = f'/api/runs/{rid}/decision/acceptance'
    assert client.post(endpoint, json={'digest': 'b' * 64, 'approved': True}).status_code == 409
    response = client.post(endpoint, json={'digest': 'a' * 64, 'approved': True})
    assert response.status_code == (202 if accepted else 422), response.text
