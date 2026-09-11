"""Real API/DB policy tests plus actual macOS command enforcement in temp data.

Model calls are prohibited here. This verifies policy administration and native
execution, not an end-to-end production model run.
"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import shlex
import sys
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from forge.api import app
from forge import engine, local_executor as native, execution_readiness
from forge.db import Agent, Event, Project, Run, Task, session
from forge.execution_permissions import request_permission
from forge.execution_auto_review import assess_request
from forge.member_execution_policy import execution_project
from conftest import run_body, seat_agents


def get_policies(client, project_id=''):
    response = client.get('/api/execution/policies', params={'project_id': project_id})
    assert response.status_code == 200, response.text
    return response.json()


def set_policy(client, scope, target, profile, *, confirmed=False, project_id='', current=None):
    current = current or get_policies(client, project_id)
    return client.put(f'/api/execution/policies/{scope}/{target}', params={'project_id': project_id}, json={
        'permission_profile': profile, 'expected_version': current['version'],
        'expected_layout_ref': current['layout_ref'], 'full_access_confirmed': confirmed})


def local_project(client, configured, profile='ask'):
    pid = configured['project']['id']
    with session() as s: config = deepcopy(s.get(Project, pid).config)
    response = client.put('/api/projects/' + pid, json={**config, 'execution_mode': 'local',
        'permission_profile': profile, 'full_access_confirmed': profile == 'full'})
    assert response.status_code == 200, response.text
    return pid


def created_run(client, configured):
    response = client.post('/api/runs', json=run_body(configured))
    assert response.status_code == 202, response.text
    return response.json()['id']


def test_authenticated_inventory_never_invents_a_project_permission(client, configured):
    inventory = get_policies(client)
    assert inventory['version'] == 0 and inventory['project'] is None
    assert len(inventory['teams']) == 1 and len(inventory['agents']) == 4
    assert all(a['permission_profile'] is None and a['effective_profile'] is None and a['source'] == 'project' for a in inventory['agents'])
    assert set(inventory['teams'][0]['member_ids']) == {a['id'] for a in configured['agents']}
    selected = get_policies(client, configured['project']['id'])
    assert all(a['effective_profile'] == 'ask' for a in selected['agents'])
    assert client.get('/api/execution/policies?project_id=missing').status_code == 404
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/execution/policies').status_code == 401


def test_policy_writes_require_admin_csrf_current_version_and_current_membership(client, configured):
    current = get_policies(client)
    aid, team = configured['agents'][0]['id'], current['teams'][0]['id']
    body = {'permission_profile': 'auto', 'expected_version': current['version'], 'expected_layout_ref': current['layout_ref']}
    assert client.put('/api/execution/policies/agent/' + aid, json=body, headers={'X-Forge-Request': ''}).status_code == 403
    for role in ('developer', 'viewer'):
        response = client.post('/api/users', json={'username': 'policy-' + role, 'password': 'policy-test-only-12345', 'role': role})
        assert response.status_code == 201, response.text
        with TestClient(app, headers={'X-Forge-Request': '1'}) as other:
            assert other.post('/api/auth/login', json={'username': 'policy-' + role, 'password': 'policy-test-only-12345'}).status_code == 200
            assert other.get('/api/execution/policies').status_code == 200
            assert other.put('/api/execution/policies/agent/' + aid, json=body).status_code == 403
    assert set_policy(client, 'team', team, 'auto', current=current).status_code == 200
    assert set_policy(client, 'agent', aid, 'ask', current=current).status_code == 409
    stale = get_policies(client)
    stale['layout_ref']['digest'] = '0' * 64
    assert set_policy(client, 'team', team, 'full', confirmed=True, current=stale).status_code == 409
    assert set_policy(client, 'team', 'draft-or-missing-team', 'ask').status_code == 404
    assert set_policy(client, 'agent', 'missing-member', 'ask').status_code == 404
    assert set_policy(client, 'project', aid, 'ask').status_code == 404
    final = get_policies(client)
    assert final['version'] == 1 and all(a['effective_profile'] == 'auto' for a in final['agents'])


def test_full_and_restoring_full_inheritance_require_explicit_confirmation(client, configured):
    pid = local_project(client, configured)
    current = get_policies(client, pid)
    team, aid = current['teams'][0]['id'], configured['agents'][0]['id']
    assert set_policy(client, 'team', team, 'full', project_id=pid).status_code == 422
    assert set_policy(client, 'agent', aid, 'full').status_code == 422
    assert get_policies(client)['version'] == 0
    assert set_policy(client, 'team', team, 'full', confirmed=True).status_code == 200
    assert set_policy(client, 'agent', aid, 'ask').status_code == 200
    assert set_policy(client, 'agent', aid, None).status_code == 422
    response = set_policy(client, 'agent', aid, None, confirmed=True)
    assert response.status_code == 200, response.text
    row = next(a for a in response.json()['agents'] if a['id'] == aid)
    assert row['permission_profile'] is None and row['effective_profile'] == 'full' and row['source'] == 'team'
    current = response.json()
    assert client.put('/api/execution/policies/agent/' + aid, json={'permission_profile': 'full',
        'expected_version': current['version'], 'expected_layout_ref': current['layout_ref'], 'full_access_confirmed': 'true'}).status_code == 422
    assert client.put('/api/execution/policies/agent/' + aid, json={'permission_profile': 'ask',
        'expected_version': True, 'expected_layout_ref': current['layout_ref']}).status_code == 422
    # Restoring project inheritance can also be full when previewing that project.
    local_project(client, configured, 'full')
    assert set_policy(client, 'team', team, None, project_id=pid).status_code == 422
    assert set_policy(client, 'team', team, None, project_id=pid, confirmed=True).status_code == 200


def test_concurrent_first_policy_saves_cannot_both_win_the_same_global_version(client, configured):
    current = get_policies(client)
    member_ids = [a['id'] for a in configured['agents'][:2]]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(set_policy, client, 'agent', aid, 'auto', current=current) for aid in member_ids]
        responses = [future.result(timeout=10) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    result = get_policies(client)
    assert result['version'] == 1
    assert sum(a['permission_profile'] == 'auto' for a in result['agents']) == 1


def test_new_run_freezes_member_over_team_over_project_and_old_runs_do_not_change(client, configured):
    pid = local_project(client, configured)
    before_agents = client.get('/api/agents').json()
    team = get_policies(client)['teams'][0]['id']
    aid = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    assert set_policy(client, 'team', team, 'auto').status_code == 200
    assert set_policy(client, 'agent', aid, 'ask').status_code == 200
    first_id = created_run(client, configured)
    with session() as s: first = deepcopy(s.get(Run, first_id).snapshot)
    for member_id, agent in first['agents'].items():
        assert agent['execution_policy']['profile'] == ('ask' if member_id == aid else 'auto')
        assert agent['execution_policy']['source'] == ('agent' if member_id == aid else 'team')
        assert agent['execution_policy']['policy_version'] == 2
    assert set_policy(client, 'team', team, 'ask').status_code == 200
    assert set_policy(client, 'agent', aid, 'full', confirmed=True).status_code == 200
    second_id = created_run(client, configured)
    with session() as s:
        assert s.get(Run, first_id).snapshot == first
        second = s.get(Run, second_id)
        assert execution_project(second, aid)['permission_profile'] == 'full'
        assert second.snapshot['project']['permission_profile'] == 'ask'
        assert second.snapshot['agents'][aid]['execution_policy']['policy_version'] == 4
        records = list(s.scalars(select(Event).where(Event.kind == 'execution.policy.updated')))
        assert len(records) == 4
        assert all(not e.data['existing_runs_changed'] and not e.data['role_tools_changed'] and not e.data['model_authorization_changed'] for e in records)
    assert client.get('/api/agents').json() == before_agents
    assert get_policies(client, pid)['project']['permission_profile'] == 'ask'


def test_full_member_blocks_container_preflight_and_new_run_without_loosening_other_gates(client, configured, monkeypatch):
    aid = configured['agents'][0]['id']
    assert set_policy(client, 'agent', aid, 'full', confirmed=True).status_code == 200
    async def no_sandbox(): return {'ready': False, 'detail': 'isolated probe only', 'images': []}
    monkeypatch.setattr(execution_readiness, 'sandbox_readiness', no_sandbox)
    response = client.post('/api/execution/preflight', json=run_body(configured))
    assert response.status_code == 200, response.text
    result = response.json()
    assert not result['ready'] and not result['snapshot_valid']
    assert any(c['code'] == 'agent.execution_policy' and c['status'] == 'error' for c in result['checks'])
    response = client.post('/api/runs', json=run_body(configured))
    assert response.status_code == 422 and '完全访问' in response.text
    # A separate layout with reused short team IDs must not receive an old grant.
    assert set_policy(client, 'agent', aid, None).status_code == 200
    team = get_policies(client)['teams'][0]['id']
    assert set_policy(client, 'team', team, 'full', confirmed=True).status_code == 200
    previous_ref = get_policies(client)['layout_ref']
    seat_agents(client)
    current = get_policies(client)
    assert current['layout_ref']['id'] != previous_ref['id']
    assert current['teams'][0]['permission_profile'] is None
    assert all(a['effective_profile'] is None for a in current['agents'])


def test_legacy_runtime_fallback_and_bad_new_policies_fail_closed():
    snapshot = {'project': {'execution_mode': 'local', 'permission_profile': 'auto'}, 'agents': {'legacy': {}}}
    run = SimpleNamespace(snapshot=snapshot)
    assert execution_project(run, 'legacy')['permission_profile'] == 'auto'
    result = execution_project(run, 'legacy'); result['permission_profile'] = 'full'
    assert snapshot['project']['permission_profile'] == 'auto'
    with pytest.raises(ValueError, match='不属于'): execution_project(run, 'foreign')
    for invalid in ({'profile': 'everything'}, 'full', {}):
        snapshot['agents']['legacy']['execution_policy'] = invalid
        with pytest.raises(ValueError): execution_project(run, 'legacy')
    snapshot['project']['execution_mode'] = 'container'
    snapshot['agents']['legacy']['execution_policy'] = {'profile': 'full'}
    with pytest.raises(ValueError, match='本机'): execution_project(run, 'legacy')


@pytest.fixture
def native_members(client, configured, monkeypatch):
    if sys.platform != 'darwin' or not Path(native.SANDBOX).is_file(): pytest.skip('actual OS policy enforcement requires macOS')
    monkeypatch.setattr(native, 'settings', replace(native.settings, local_execution_enabled=True))
    pid = local_project(client, configured)
    dev = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    tester = next(a['id'] for a in configured['agents'] if a['role'] == 'tester')
    assert set_policy(client, 'agent', dev, 'full', confirmed=True).status_code == 200
    assert set_policy(client, 'agent', tester, 'ask').status_code == 200
    rid = created_run(client, configured)
    engine.prepare_run({'run_id': rid})
    for key, aid in [('full-worker', dev), ('ask-worker', tester)]:
        engine.create_task(rid, key, 'develop', aid, 'Temporary permission enforcement probe', {})
    return rid, dev, tester, pid


@pytest.mark.asyncio
async def test_actual_native_commands_enforce_each_members_frozen_profile(client, native_members, tmp_path):
    rid, dev, tester, _ = native_members
    outside = tmp_path / 'explicitly-designated-probe.txt'
    outside.write_text('POLICY_FIXTURE_PUBLIC_DATA')
    command = '/bin/cat ' + shlex.quote(str(outside.resolve()))
    full = await native.execute(rid, 'full-worker', 'read-probe', command, {'command_timeout_s': 5})
    limited = await native.execute(rid, 'ask-worker', 'read-probe', command, {'command_timeout_s': 5})
    assert full['exit_code'] == 0 and full['output'] == 'POLICY_FIXTURE_PUBLIC_DATA'
    assert full['permission_profile'] == 'full' and full['runtime'] == 'native-full'
    assert limited['exit_code'] != 0 and 'POLICY_FIXTURE_PUBLIC_DATA' not in limited['output']
    assert limited['permission_profile'] == 'ask' and limited['runtime'] == 'seatbelt'
    with session() as s:
        receipts = list(s.scalars(select(Event).where(Event.run_id == rid, Event.kind == 'execution.native.completed')))
        assert {e.data['result']['permission_profile'] for e in receipts} == {'full', 'ask'}
    with pytest.raises(ValueError, match='完全访问'):
        await request_permission(rid, 'full-worker', 'not-needed', {'command': 'pwd', 'reason': 'fixture check'})


@pytest.mark.asyncio
async def test_permission_requests_auto_review_and_offline_install_output_use_member_policy(client, configured, monkeypatch):
    local_project(client, configured, 'auto')
    members = {a['role']: a['id'] for a in configured['agents']}
    assert set_policy(client, 'agent', members['developer'], 'ask').status_code == 200
    assert set_policy(client, 'agent', members['tester'], 'auto').status_code == 200
    rid = created_run(client, configured)
    engine.prepare_run({'run_id': rid})
    for role in ('developer', 'tester'):
        engine.create_task(rid, role, 'develop', members[role], role, {})
    from forge import execution_auto_review, tool_installations
    calls = []
    async def review(*args):
        calls.append(args[2])
        return {'decision': 'ask_user', 'reason': 'Test only: no model request', 'digest': args[3]}
    monkeypatch.setattr(execution_auto_review, 'assess_request', review)
    for role in ('developer', 'tester'):
        request = await request_permission(rid, role, 'one-request-' + role, {'command': 'pwd', 'network': True, 'reason': 'Isolated policy wiring test'})
        assert request['permission_status'] == 'pending'
    assert calls == [members['tester']], 'a project auto profile must not override the member ask profile'
    result = await assess_request(rid, 'developer', members['developer'], 'a' * 64, {'command': 'pwd'})
    assert result['decision'] == 'ask_user' and '未启用' in result['reason']
    installation = tool_installations.request_installation(rid, 'developer', 'offline-probe',
        {'package_id': 'forge-text-tools', 'version': '1.0.0', 'reason': 'Inspect permission metadata only'})
    with session() as s:
        row = s.get(tool_installations.ToolInstallation, installation['installation_id'])
        assert row.status == 'pending', 'a profile change must not fabricate installation approval'
        assert tool_installations.output(row)['permission_profile'] == 'ask'


def test_readiness_reports_mixed_profiles_without_borrowing_full_members_environment(client, native_members):
    _, dev, tester, pid = native_members
    app.state.dispatch_status = 'connected (isolated test only)'
    response = client.get('/api/execution/readiness', params={'project_id': pid})
    assert response.status_code == 200, response.text
    execution = response.json()['execution']
    assert execution['permission_profile'] == 'mixed'
    assert set(execution['environments']) == {'ask', 'full'}
    policies = {a['agent_id']: a['profile'] for a in execution['member_policies']}
    assert policies[dev] == 'full' and policies[tester] == 'ask'


def test_actual_initial_messages_use_each_members_frozen_execution_policy_not_live_configuration(client, configured):
    local_project(client, configured, 'auto')
    members = {a['role']: a['id'] for a in configured['agents']}
    dev, tester = members['developer'], members['tester']
    assert set_policy(client, 'agent', dev, 'full', confirmed=True).status_code == 200
    assert set_policy(client, 'agent', tester, 'ask').status_code == 200
    rid = created_run(client, configured)
    engine.prepare_run({'run_id': rid})
    for key, aid in [('developer-context', dev), ('tester-context', tester)]:
        engine.create_task(rid, key, 'autonomous', aid, 'Inspect member context only', {'description': 'No model call'})
    # Changing live settings does not retroactively alter model/tool authority.
    assert set_policy(client, 'agent', dev, 'ask').status_code == 200
    assert set_policy(client, 'agent', tester, 'full', confirmed=True).status_code == 200
    with session() as s:
        run = s.get(Run, rid)
        before = deepcopy(run.snapshot)
        prompts = {}
        for key, aid in [('developer-context', dev), ('tester-context', tester)]:
            agent = run.snapshot['agents'][aid]
            task = s.get(Task, engine.task_id(rid, key))
            prompts[aid] = engine.initial_messages(run, task, agent)
            context = json.loads(prompts[aid][1]['content'])
            assert context['execution_policy'] == {'execution_mode': 'local', 'permission_profile': 'full' if aid == dev else 'ask'}
            assert {item['id'] for item in context['team_work']['team_capabilities']} == set(run.snapshot['agents'])
            assert context['task_intent']['current_request'] == run.request
        assert run.snapshot == before
    assert 'do not request_execution_permission' in prompts[dev][0]['content']
    assert 'Native ask/auto commands use macOS Seatbelt' not in prompts[dev][0]['content']
    assert 'Native ask/auto commands use macOS Seatbelt' in prompts[tester][0]['content']
    assert 'This member has a user-frozen full' not in prompts[tester][0]['content']
