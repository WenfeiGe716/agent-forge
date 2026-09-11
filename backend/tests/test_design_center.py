"""Design proposals never gain runtime authority without exact human review."""
from copy import deepcopy

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from forge.db import Agent, Event, ModelProfile, Provider, SkillRevision, session
from forge.design_center import (
    DesignAsset, DesignRevision, create_asset, published_assets, resolve_published,
)
from conftest import run_body, seat_agents


def create(client, kind, config, name='Editable enterprise design'):
    response = client.post('/api/design/assets', json={'kind': kind, 'name': name, 'config': config})
    assert response.status_code == 201, response.text
    return response.json()


def confirm(client, asset, action='publish', **extra):
    return client.post(f'/api/design/assets/{asset["id"]}/{action}', json={
        'version': asset['version'], 'digest': asset['digest'], 'comment': '管理员确认此内容', **extra,
    })


def routing(configured):
    return {'task_type': '代码修复', 'assignments': {'planner': configured['models'][0],
            'developer': configured['models'][1]}, 'budget': 'medium', 'max_parallel': 3,
            'rules': {'allowed_provider_ids': configured['providers'], 'allow_external': False,
                      'require_tools': True, 'max_model_calls': 40}}


def team(configured):
    return {'members': [
        {'key': 'atlas', 'name': 'Atlas', 'role': 'planner', 'model_profile_id': configured['models'][0],
         'tool_permissions': ['read_file', 'send_message', 'finish', 'request_decision'],
         'office_position': {'x': 4, 'z': 1, 'floor': 1, 'zone': '架构区'}},
        {'key': 'nova', 'name': 'Nova', 'role': 'developer', 'model_profile_id': configured['models'][1],
         'reports_to': 'atlas', 'communication': {'scope': 'custom', 'recipients': ['atlas']},
         'office_position': {'x': 7, 'z': 2, 'floor': 2, 'zone': '开发区'}},
        {'key': 'sage', 'name': 'Sage', 'role': 'reviewer', 'model_profile_id': configured['models'][0],
         'reports_to': 'atlas'},
    ], 'workflow': {'mode': 'discussion', 'participants': ['atlas', 'nova', 'sage'],
                    'assessor_id': 'sage', 'strategy': 'sequential'}}


def test_incomplete_drafts_are_editable_but_cannot_publish(client):
    draft = create(client, 'team', {'members': [{'name': '尚未选择模型'}]})
    assert draft['status'] == 'draft' and draft['version'] == 1
    assert draft['published_version'] is None and draft['source'] == 'manual'
    before = len(client.get('/api/agents').json())
    assert confirm(client, draft).status_code == 422
    assert confirm(client, draft, 'instantiate').status_code == 422
    assert len(client.get('/api/agents').json()) == before
    assert client.get(f'/api/design/assets/{draft["id"]}').json()['status'] == 'draft'


def test_review_requires_exact_digest_and_revisions_preserve_published_content(client, configured):
    config = routing(configured)
    draft = create(client, 'routing', config)
    assert confirm(client, draft, digest='0' * 64).status_code == 409
    approved = confirm(client, draft).json()
    assert approved['status'] == 'published' and approved['published_by'] == draft['created_by']
    changed = deepcopy(config)
    changed['budget'] = 'low'
    response = client.put(f'/api/design/assets/{draft["id"]}', json={
        'name': '支付策略 v2', 'config': changed, 'expected_version': 1,
    })
    assert response.status_code == 200, response.text
    second = response.json()
    assert second['version'] == 2 and second['status'] == 'draft' and second['published_version'] == 1
    assert second['digest'] != draft['digest']
    original = client.get(f'/api/design/assets/{draft["id"]}/versions/1').json()
    assert original['config'] == config and original['name'] == draft['name']
    assert original['digest'] == draft['digest'] and original['status'] == 'published'
    assert client.put(f'/api/design/assets/{draft["id"]}', json={
        'name': 'stale', 'config': changed, 'expected_version': 1,
    }).status_code == 409
    with session() as s:
        assert resolve_published(s, {'id': draft['id'], 'version': 1})['config'] == config
        with pytest.raises(ValueError, match='未发布'):
            resolve_published(s, {'id': draft['id'], 'version': 2})
        assert published_assets(s, 'routing')[0]['version'] == 1
    detail = client.get(f'/api/design/assets/{draft["id"]}').json()
    assert [v['version'] for v in detail['versions']] == [2, 1]
    assert [event['action'] for event in detail['audit']] == ['proposed', 'published', 'revised']
    assert all(item['actor'] == draft['created_by'] for item in detail['audit'])
    assert len(client.get('/api/design/assets?kind=routing').json()) == 1
    assert client.get('/api/design/assets?kind=team').json() == []


def test_revocation_is_explicit_and_cannot_reapprove_same_version(client, configured):
    first = create(client, 'routing', routing(configured))
    assert confirm(client, first).status_code == 200
    second = client.put(f'/api/design/assets/{first["id"]}', json={
        'name': first['name'], 'config': first['config'], 'expected_version': 1,
    }).json()
    assert confirm(client, second).status_code == 200
    with session() as s:
        assert resolve_published(s, {'id': first['id'], 'version': 1})['version'] == 1
        assert published_assets(s, 'routing')[0]['version'] == 2
    assert confirm(client, second, 'revoke', digest='0' * 64).status_code == 409
    revoked = confirm(client, second, 'revoke').json()
    assert revoked['status'] == 'revoked' and revoked['published_version'] == 1
    assert confirm(client, second, 'revoke').status_code == 200
    assert confirm(client, second).status_code == 409
    with session() as s:
        with pytest.raises(ValueError, match='撤销'):
            resolve_published(s, {'id': first['id'], 'version': 2})
        assert published_assets(s, 'routing')[0]['version'] == 1
    assert confirm(client, first, 'revoke').status_code == 200
    with session() as s:
        assert published_assets(s, 'routing') == []


def test_provenance_and_actor_cannot_be_forged_by_generic_api(client, configured):
    for source in ('ai', 'benchmark'):
        response = client.post('/api/design/assets', json={
            'kind': 'routing', 'name': 'forged', 'config': routing(configured), 'source': source,
        })
        assert response.status_code == 422
    assert client.post('/api/design/assets', json={
        'kind': 'routing', 'name': 'forged', 'config': routing(configured), 'created_by': 'somebody-else',
    }).status_code == 422
    with session() as s:
        internal = create_asset(s, 'routing', 'AI suggestion', routing(configured), 'service-actor', 'ai')
        assert internal['source'] == 'ai' and internal['status'] == 'draft'
        assert s.get(DesignAsset, internal['id']).published_version is None
    changed = client.put(f'/api/design/assets/{internal["id"]}', json={
        'name': 'Human revision', 'config': internal['config'], 'expected_version': 1,
    }).json()
    assert changed['source'] == 'manual' and changed['created_by'] != 'service-actor'


def test_only_admins_may_edit_publish_or_instantiate(client, configured):
    draft = create(client, 'team', team(configured))
    result = client.post('/api/users', json={'username': 'designer', 'password': 'another-password-12345', 'role': 'developer'})
    assert result.status_code == 201, result.text
    client.post('/api/auth/logout')
    assert client.post('/api/auth/login', json={'username': 'designer', 'password': 'another-password-12345'}).status_code == 200
    assert client.get('/api/design/assets').status_code == 200
    assert client.get(f'/api/design/assets/{draft["id"]}').status_code == 200
    assert confirm(client, draft).status_code == 403
    assert confirm(client, draft, 'instantiate').status_code == 403
    assert client.put(f'/api/design/assets/{draft["id"]}', json={
        'name': 'unauthorized', 'config': draft['config'], 'expected_version': 1,
    }).status_code == 403
    client.post('/api/auth/logout')
    assert client.get('/api/design/assets').status_code == 401


def test_team_publication_does_not_create_agents_but_explicit_instantiation_maps_relations(client, configured):
    draft = create(client, 'team', team(configured))
    before = len(client.get('/api/agents').json())
    assert confirm(client, draft).status_code == 200
    assert len(client.get('/api/agents').json()) == before
    assert confirm(client, draft, 'instantiate', digest='0' * 64).status_code == 409
    response = confirm(client, draft, 'instantiate')
    assert response.status_code == 201, response.text
    instance = response.json()
    mapping = instance['member_mapping']
    assert len(instance['agent_ids']) == 3 and set(mapping) == {'atlas', 'nova', 'sage'}
    live = {a['id']: a for a in client.get('/api/agents').json()}
    assert len(live) == before + 3
    nova = live[mapping['nova']]
    assert nova['reports_to'] == mapping['atlas']
    assert nova['communication']['recipients'] == [mapping['atlas']]
    assert nova['design_ref'] == {'id': draft['id'], 'version': 1, 'digest': draft['digest'],
                                 'member_key': 'nova', 'instance_id': instance['instance_id']}
    assert nova['office_position']['floor'] == 2
    assert live[mapping['atlas']]['tool_permissions'] == team(configured)['members'][0]['tool_permissions']
    assert instance['workflow']['participants'] == instance['agent_ids']
    assert instance['workflow']['assessor_id'] == mapping['sage']
    # A second explicit instance has isolated identities and relationship mappings.
    second = confirm(client, draft, 'instantiate').json()
    assert not set(second['agent_ids']) & set(instance['agent_ids'])


@pytest.mark.parametrize('mutation', ['cycle', 'duplicate_key', 'duplicate_name', 'outsider', 'privilege', 'missing_model'])
def test_team_validation_rejects_invalid_authority_and_topology(client, configured, mutation):
    config = team(configured)
    members = config['members']
    if mutation == 'cycle':
        members[0]['reports_to'] = 'nova'
    elif mutation == 'duplicate_key':
        members[1]['key'] = 'atlas'
    elif mutation == 'duplicate_name':
        members[1]['name'] = '  atlas  '
    elif mutation == 'outsider':
        members[1]['communication']['recipients'] = ['unrelated']
    elif mutation == 'privilege':
        members[0]['tool_permissions'] = ['write_file']
    elif mutation == 'missing_model':
        members[1]['model_profile_id'] = None
    draft = create(client, 'team', config)
    assert confirm(client, draft).status_code == 422


def test_team_skill_must_be_approved_and_new_instances_recheck_revocation(client, configured):
    result = client.post('/api/skills', json={'files': {'SKILL.md':
        '---\nname: design-review\ndescription: Review a human approved design.\n---\nCheck the design.'}})
    assert result.status_code == 201, result.text
    skill = result.json()
    config = team(configured)
    config['members'][0]['skills'] = [{'skill_id': skill['id'], 'version': 1}]
    draft = create(client, 'team', config)
    assert confirm(client, draft).status_code == 422
    review = {'version': 1, 'digest': skill['digest'], 'action': 'approve'}
    assert client.post(f'/api/skills/{skill["id"]}/review', json=review).status_code == 200
    assert confirm(client, draft).status_code == 200
    assert client.post(f'/api/skills/{skill["id"]}/review', json={**review, 'action': 'revoke'}).status_code == 200
    assert confirm(client, draft, 'instantiate').status_code == 422
    with session() as s:
        assert published_assets(s, 'team') == []


@pytest.mark.parametrize('violation', ['provider_whitelist', 'tools', 'external', 'disabled'])
def test_routing_rules_validate_actual_models(client, configured, violation):
    config = routing(configured)
    if violation == 'provider_whitelist':
        config['rules']['allowed_provider_ids'] = [configured['providers'][0]]
    else:
        with session() as s:
            model = s.get(ModelProfile, configured['models'][1])
            if violation == 'tools':
                model.config = {**model.config, 'supports_tools': False}
            elif violation == 'disabled':
                model.config = {**model.config, 'enabled': False}
            elif violation == 'external':
                from forge.db import ProviderRevision
                provider = s.get(Provider, configured['providers'][1])
                revision = s.get(ProviderRevision, provider.revision_id)
                revision.config = {**revision.config, 'external': True}
    draft = create(client, 'routing', config)
    assert confirm(client, draft).status_code == 422


def test_runtime_resolution_rechecks_disabled_dependency_and_content_integrity(client, configured):
    draft = create(client, 'routing', routing(configured))
    assert confirm(client, draft).status_code == 200
    with session() as s:
        model = s.get(ModelProfile, configured['models'][0])
        model.config = {**model.config, 'enabled': False}
    with session() as s:
        with pytest.raises(ValueError, match='停用'):
            resolve_published(s, {'id': draft['id'], 'version': 1})
        model = s.get(ModelProfile, configured['models'][0])
        model.config = {**model.config, 'enabled': True}
        revision = s.get(DesignRevision, f'{draft["id"]}:1')
        revision.config = {**revision.config, 'budget': 'high'}
    with session() as s:
        with pytest.raises(HTTPException) as exc:
            resolve_published(s, {'id': draft['id'], 'version': 1})
        assert exc.value.status_code == 409


def test_workflow_office_and_benchmarks_have_editable_published_versions(client, configured_unseated):
    configured = configured_unseated
    ids = [a['id'] for a in configured['agents']]
    examples = [
        ('workflow', {'graph': {'mode': 'discussion', 'participants': ids}}),
        ('office', {'positions': {ids[0]: {'x': -3, 'z': 4, 'floor': 2, 'zone': '测试区'}}}),
        ('benchmark', {'description': '内部语言样例', 'cases': [{
            'id': 'json_sum', 'dimension': 'reasoning', 'prompt': 'Return {"sum":5}',
            'expected': '{"sum":5}', 'match': 'json',
        }]}),
    ]
    for kind, config in examples:
        draft = create(client, kind, config)
        assert confirm(client, draft).status_code == 200
        with session() as s:
            assert resolve_published(s, {'id': draft['id'], 'version': 1})['config'] == config
    invalid = create(client, 'benchmark', {'cases': [{
        'id': 'bad_json', 'dimension': 'coding', 'prompt': 'Return JSON', 'expected': 'not-json', 'match': 'json',
    }]})
    assert confirm(client, invalid).status_code == 422
    with session() as s:
        events = list(s.scalars(select(Event).where(Event.kind == 'design.published')))
        assert len(events) == 3
        assert all(e.data['digest'] and e.data['actor'] for e in events)


def full_routing(configured, **changes):
    config = routing(configured)
    config['assignments'].update({'tester': configured['models'][1], 'reviewer': configured['models'][0]})
    return config | changes


def preview(client, configured, **changes):
    return client.post('/api/runs/preview-config', json=run_body(configured, **changes))


def test_runtime_routing_requires_publication_and_freezes_exact_version(client, configured):
    draft = create(client, 'routing', full_routing(configured))
    ref = {'id': draft['id'], 'version': 1}
    assert preview(client, configured, routing_ref=ref).status_code == 422
    assert confirm(client, draft).status_code == 200
    response = client.post('/api/runs', json=run_body(
        configured, routing_ref=ref, routing_overrides={'developer': configured['models'][0]}))
    assert response.status_code == 202, response.text
    run_id = response.json()['id']
    snapshot = client.get(f'/api/runs/{run_id}').json()['snapshot']
    assert snapshot['mode'] == 'runtime' and snapshot['design']['routing']['digest'] == draft['digest']
    developer = next(a for a in snapshot['agents'].values() if a['role'] == 'developer')
    assert developer['models'][0]['profile_id'] == configured['models'][0]
    assert developer['model_selection']['source'] == 'task_override'
    assert snapshot['runtime']['max_model_calls'] == 40
    assert snapshot['design']['routing_overrides'] == {'developer': configured['models'][0]}
    changed = deepcopy(draft['config'])
    changed['assignments']['developer'] = configured['models'][0]
    changed['max_parallel'] = 1
    second = client.put(f'/api/design/assets/{draft["id"]}', json={
        'name': draft['name'], 'config': changed, 'expected_version': 1,
    }).json()
    assert confirm(client, second).status_code == 200
    assert client.get(f'/api/runs/{run_id}').json()['snapshot'] == snapshot
    assert confirm(client, draft, 'revoke').status_code == 200
    assert preview(client, configured, routing_ref=ref).status_code == 422
    assert client.get(f'/api/runs/{run_id}').json()['snapshot'] == snapshot


def test_runtime_reconstructs_published_team_and_preserves_permissions_after_live_edits(client, configured):
    config = team(configured)
    config.pop('workflow')
    config['members'][2]['reports_to'] = 'nova'
    draft = create(client, 'team', config)
    assert confirm(client, draft).status_code == 200
    instance = confirm(client, draft, 'instantiate').json()
    mapping = instance['member_mapping']
    body = run_body(configured, team_ref=instance['team_ref'], agent_ids=instance['agent_ids'])
    seat_agents(client)
    initial = client.post('/api/runs', json=body)
    assert initial.status_code == 202, initial.text
    frozen = client.get(f'/api/runs/{initial.json()["id"]}').json()['snapshot']
    live = next(a for a in client.get('/api/agents').json() if a['id'] == mapping['nova'])
    edit = {k: v for k, v in live.items() if k not in ('id', 'version')}
    edit.update(name='Manually changed live name', instructions='different instructions', reports_to=None,
                office_position={'x': 0, 'z': 0, 'floor': 1, 'zone': 'temporary'}, tool_permissions=['finish'])
    assert client.put(f'/api/agents/{live["id"]}', json=edit).status_code == 200
    response = client.post('/api/runs/preview-config', json=body)
    assert response.status_code == 200, response.text
    reconstructed = response.json()
    nova = reconstructed['agents'][mapping['nova']]
    sage = reconstructed['agents'][mapping['sage']]
    assert nova['name'] == 'Nova' and nova['office_position']['floor'] == 2
    assert nova['instructions'] == frozen['agents'][mapping['nova']]['instructions']
    assert 'write_file' in nova['tools'] and nova['reporting_chain'] == [mapping['atlas']]
    assert sage['reporting_chain'] == [mapping['nova'], mapping['atlas']]
    assert reconstructed['agents'][mapping['atlas']]['tools'] == config['members'][0]['tool_permissions']
    assert nova['design_ref']['instance_id'] == instance['instance_id']
    assert reconstructed['design']['team_instance_id'] == instance['instance_id']
    assert client.get(f'/api/runs/{initial.json()["id"]}').json()['snapshot'] == frozen
    second_config = deepcopy(config)
    second_config['members'][1]['name'] = 'Nova v2'
    second = client.put(f'/api/design/assets/{draft["id"]}', json={
        'name': draft['name'], 'config': second_config, 'expected_version': 1,
    }).json()
    assert client.post('/api/runs/preview-config', json={**body, 'team_ref': {'id': draft['id'], 'version': 2}}).status_code == 422
    assert confirm(client, second).status_code == 200
    assert client.post('/api/runs/preview-config', json={**body, 'team_ref': {'id': draft['id'], 'version': 2}}).status_code == 422
    assert client.post('/api/runs/preview-config', json=body).json()['agents'][mapping['nova']]['name'] == 'Nova'


def test_runtime_rejects_mixed_instances_even_when_member_keys_are_unique(client, configured):
    config = team(configured)
    config.pop('workflow')
    draft = create(client, 'team', config)
    assert confirm(client, draft).status_code == 200
    one = confirm(client, draft, 'instantiate').json()
    two = confirm(client, draft, 'instantiate').json()
    mixed = [one['member_mapping']['atlas'], two['member_mapping']['nova'], one['member_mapping']['sage']]
    response = preview(client, configured, team_ref=one['team_ref'], agent_ids=mixed)
    assert response.status_code == 422 and '多个实例' in response.text
    assert preview(client, configured, team_ref=one['team_ref'], agent_ids=one['agent_ids'][:2]).status_code == 422
    assert preview(client, configured, team_ref=one['team_ref']).status_code == 422


def test_runtime_accepts_schema_defaults_without_mutating_published_content(client, configured):
    sparse = {'task_type': 'Sparse valid route', 'assignments': full_routing(configured)['assignments']}
    route = create(client, 'routing', sparse)
    assert confirm(client, route).status_code == 200
    workflow_config = {'graph': {'mode': 'discussion', 'participants': [a['id'] for a in configured['agents']]}}
    workflow = create(client, 'workflow', workflow_config)
    assert confirm(client, workflow).status_code == 200
    response = preview(client, configured, routing_ref={'id': route['id'], 'version': 1},
                       workflow_ref={'id': workflow['id'], 'version': 1})
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert snapshot['runtime']['max_model_calls'] == 100
    assert snapshot['orchestration']['strategy'] == 'sequential'
    assert snapshot['design']['routing']['config'] == sparse
    assert snapshot['design']['workflow']['config'] == workflow_config


@pytest.mark.parametrize('violation', ['task_override', 'fallback', 'max_parallel', 'max_model_calls'])
def test_task_overrides_and_all_fallbacks_obey_published_enterprise_rules(client, configured, violation):
    config = full_routing(configured)
    config['assignments'] = {role: configured['models'][0] for role in config['assignments']}
    config['rules']['allowed_provider_ids'] = [configured['providers'][0]]
    draft = create(client, 'routing', config)
    assert confirm(client, draft).status_code == 200
    ref = {'id': draft['id'], 'version': 1}
    changes = {'routing_ref': ref}
    if violation == 'task_override':
        changes['routing_overrides'] = {'developer': configured['models'][1]}
    elif violation == 'fallback':
        agent = next(a for a in configured['agents'] if a['role'] == 'developer')
        edit = {k: v for k, v in agent.items() if k not in ('id', 'version')}
        edit['fallback_model_ids'] = [configured['models'][1]]
        assert client.put(f'/api/agents/{agent["id"]}', json=edit).status_code == 200
    else:
        changes['runtime'] = {violation: 4 if violation == 'max_parallel' else 41}
    response = preview(client, configured, **changes)
    assert response.status_code == 422, response.text
    # Globally selected enterprise rules cannot be bypassed by omitting routing_ref.
    assert client.put('/api/design/enterprise-policy', json={'ref': ref}).status_code == 200
    assert preview(client, configured).status_code == 422


def test_global_enterprise_policy_and_office_apply_require_explicit_publication(client, configured):
    draft = create(client, 'routing', full_routing(configured))
    ref = {'id': draft['id'], 'version': 1}
    assert client.put('/api/design/enterprise-policy', json={'ref': ref}).status_code == 422
    assert confirm(client, draft).status_code == 200
    assert client.put('/api/design/enterprise-policy', json={'ref': ref}).status_code == 200
    snapshot = preview(client, configured).json()
    assert snapshot['design']['enterprise']['digest'] == draft['digest']
    assert snapshot['runtime']['max_model_calls'] == 40
    assert preview(client, configured, runtime={'max_model_calls': 41}).status_code == 422
    aid = configured['agents'][0]['id']
    office = create(client, 'office', {'positions': {aid: {'x': 1, 'z': 2, 'floor': 3, 'zone': '测试区'}}})
    before = next(a for a in client.get('/api/agents').json() if a['id'] == aid)
    assert confirm(client, office, 'apply-office').status_code == 422
    assert confirm(client, office).status_code == 200
    assert next(a for a in client.get('/api/agents').json() if a['id'] == aid) == before
    assert confirm(client, office, 'apply-office').status_code == 200
    after = next(a for a in client.get('/api/agents').json() if a['id'] == aid)
    assert after['office_position']['floor'] == 3 and after['version'] == before['version'] + 1
    assert confirm(client, draft, 'revoke').status_code == 200
    assert preview(client, configured).status_code == 422


def test_concurrent_edits_append_exactly_one_next_version(client, configured):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from forge.design_center import edit_asset
    from forge.design_schema import AssetEdit
    draft = create(client, 'routing', full_routing(configured))
    barrier = Barrier(2)

    def edit(name):
        barrier.wait(timeout=5)
        try:
            with session() as s:
                result = edit_asset(s, draft['id'], AssetEdit(name=name, config=draft['config'], expected_version=1), draft['created_by'])
                return result['version']
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(edit, ['Concurrent edit A', 'Concurrent edit B']))
    assert sorted(results) == [2, 409]
    detail = client.get(f'/api/design/assets/{draft["id"]}').json()
    assert [item['version'] for item in detail['versions']] == [2, 1]


def test_generation_evidence_is_persistent_and_only_server_writable(client, configured):
    from forge.design_center import set_generation
    evidence = {'method': 'rules', 'reasons': [{'role': 'planner', 'reason': '管理员已发布的模型画像'}], 'requests': 0}
    with session() as s:
        draft = create_asset(s, 'routing', 'Auditable generated recommendation', full_routing(configured), 'server-actor')
        draft = set_generation(s, draft['id'], 1, evidence)
        assert draft['generation'] == evidence
    detail = client.get(f'/api/design/assets/{draft["id"]}').json()
    assert detail['generation'] == evidence
    assert client.put(f'/api/design/assets/{draft["id"]}', json={
        'name': draft['name'], 'config': draft['config'], 'expected_version': 1, 'generation': {'method': 'forged'},
    }).status_code == 422
    with session() as s:
        with pytest.raises(ValueError, match='不可覆盖'):
            set_generation(s, draft['id'], 1, {'method': 'forged'})
    assert confirm(client, draft).status_code == 200
    with session() as s:
        with pytest.raises(ValueError, match='不可修改'):
            set_generation(s, draft['id'], 1, evidence)
    changed = client.put(f'/api/design/assets/{draft["id"]}', json={
        'name': 'Human edited design', 'config': draft['config'], 'expected_version': 1,
    }).json()
    assert changed['source'] == 'manual'
    assert changed['generation'] == {**evidence, 'edited': True, 'origin_version': 1}
    assert client.get(f'/api/design/assets/{draft["id"]}/versions/1').json()['generation'] == evidence


def test_legacy_agent_edit_preserves_office_and_permissions_until_explicitly_cleared(client, configured):
    agent = next(item for item in configured['agents'] if item['role'] == 'developer')
    body = {key: value for key, value in agent.items() if key not in ('id', 'version')}
    position = {'x': 4, 'z': -2, 'floor': 2, 'zone': '发布办公区'}
    permissions = ['read_file', 'write_file', 'finish']
    initial = client.put(f'/api/agents/{agent["id"]}', json={**body, 'office_position': position, 'tool_permissions': permissions})
    assert initial.status_code == 200, initial.text
    legacy = {key: value for key, value in initial.json().items()
              if key not in ('id', 'version', 'office_position', 'tool_permissions')}
    legacy['instructions'] = 'Legacy client updated these instructions only.'
    changed = client.put(f'/api/agents/{agent["id"]}', json=legacy)
    assert changed.status_code == 200, changed.text
    assert changed.json()['office_position'] == position
    assert changed.json()['tool_permissions'] == permissions
    assert changed.json()['instructions'] == legacy['instructions']
    assert changed.json()['version'] == initial.json()['version'] + 1
    cleared = client.put(f'/api/agents/{agent["id"]}', json={**legacy, 'office_position': None, 'tool_permissions': None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()['office_position'] is None and cleared.json()['tool_permissions'] is None


def test_legacy_role_change_cannot_carry_incompatible_permissions_into_new_role(client, configured):
    agent = next(item for item in configured['agents'] if item['role'] == 'developer')
    body = {key: value for key, value in agent.items() if key not in ('id', 'version')}
    initial = client.put(f'/api/agents/{agent["id"]}', json={**body, 'tool_permissions': ['write_file', 'finish']})
    assert initial.status_code == 200
    legacy = {key: value for key, value in initial.json().items()
              if key not in ('id', 'version', 'office_position', 'tool_permissions')}
    legacy['role'] = 'reviewer'
    rejected = client.put(f'/api/agents/{agent["id"]}', json=legacy)
    assert rejected.status_code == 422
    persisted = next(item for item in client.get('/api/agents').json() if item['id'] == agent['id'])
    assert persisted['role'] == 'developer' and persisted['tool_permissions'] == ['write_file', 'finish']
    assert persisted['version'] == initial.json()['version']
