"""Agent configuration edits persist locally; references and runs stay immutable."""
from copy import deepcopy

from conftest import run_body, seat_agents
from test_role_templates import role_item, reference
from test_v07_collaboration_skills import make_skill


def editable(agent):
    return {key: value for key, value in agent.items() if key not in ('id', 'version')}


def test_provider_owned_binding_saves_and_new_runs_freeze_it_without_changing_old_run(client, configured):
    agent = configured['agents'][0]
    old = client.post('/api/runs', json=run_body(configured))
    assert old.status_code == 202, old.text
    old_id = old.json()['id']
    snapshot = client.get('/api/runs/' + old_id).json()['snapshot']
    body = editable(agent)
    new_model = next(mid for mid in configured['models'] if mid != body['model_profile_id'])
    body.update(model_profile_id=new_model, department='品牌部', job_title='品牌研究员')
    saved = client.put('/api/agents/' + agent['id'], json=body)
    assert saved.status_code == 200, saved.text
    reread = next(a for a in client.get('/api/agents').json() if a['id'] == agent['id'])
    assert reread['model_profile_id'] == new_model
    assert reread['version'] == agent['version'] + 1
    assert reread['department'] == '品牌部'
    assert client.get('/api/runs/' + old_id).json()['snapshot'] == snapshot
    # Changing business department does not silently relocate the existing desk.
    blocked = client.post('/api/runs', json=run_body(configured))
    assert blocked.status_code == 422 and '工位' in blocked.text
    seat_agents(client)
    fresh = client.post('/api/runs', json=run_body(configured))
    assert fresh.status_code == 202, fresh.text
    current = client.get('/api/runs/' + fresh.json()['id']).json()
    frozen = current['snapshot']['agents'][agent['id']]
    assert frozen['models'][0]['profile_id'] == new_model
    assert frozen['models'][0]['model']['provider_id'] == configured['providers'][1]
    assert frozen['job_title'] == '品牌研究员' and frozen['department'] == '品牌部'
    assert current['calls'] == 0


def test_old_parameter_override_is_rejected_not_silently_discarded_or_partly_saved(client, configured):
    agent = configured['agents'][0]
    body = editable(agent)
    body['model_parameters'] = {'temperature': .4}
    saved = client.put('/api/agents/' + agent['id'], json=body).json()
    model = client.post('/api/models', json={
        'name': 'Model without temperature', 'provider_id': configured['providers'][1],
        'model_id': 'only-max-tokens', 'allowed_parameters': ['max_tokens'],
        'parameters': {'max_tokens': 1024}, 'supports_tools': True,
    })
    assert model.status_code == 201, model.text
    body['model_profile_id'] = model.json()['id']
    failed = client.put('/api/agents/' + agent['id'], json=body)
    assert failed.status_code == 422 and '未声明支持的参数' in failed.text
    assert next(a for a in client.get('/api/agents').json() if a['id'] == agent['id']) == saved
    body['model_parameters'] = {}
    accepted = client.put('/api/agents/' + agent['id'], json=body)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()['model_profile_id'] == model.json()['id']


def test_role_copy_local_edits_keep_original_reference_content_and_other_agent_unchanged(client, configured):
    item = role_item(client)
    path = '/api/role-templates/items/' + item['id']
    original = client.get(path).json()
    proposal = client.post(path + '/proposal', json={
        'revision_id': item['revision_id'], 'digest': item['digest'], 'role': 'developer',
    }).json()['member']
    agent, other = configured['agents'][:2]
    body = editable(agent)
    body.update(role_template_ref=reference(item), instructions=proposal['instructions'] + '\n此成员新增要求。')
    saved = client.put('/api/agents/' + agent['id'], json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()['role_template_ref'] == reference(item)
    assert saved.json()['instructions'].endswith('此成员新增要求。')
    assert saved.json()['model_profile_id'] == agent['model_profile_id']
    assert saved.json()['role'] == agent['role']
    assert client.get(path).json() == original
    assert next(a for a in client.get('/api/agents').json() if a['id'] == other['id']) == other


def test_skill_binding_personal_parameters_do_not_revise_approved_source_or_prior_snapshot(client, configured):
    skill = make_skill(client)
    original = client.get('/api/skills/' + skill['id']).json()
    agent = configured['agents'][0]
    body = editable(agent)
    binding = {'skill_id': skill['id'], 'version': 1, 'enabled': True, 'mode': 'on_demand',
               'allow_scripts': False, 'parameters': {'focus': 'security'}}
    body['skills'] = [binding]
    assert client.put('/api/agents/' + agent['id'], json=body).status_code == 200
    run = client.post('/api/runs', json=run_body(configured))
    assert run.status_code == 202, run.text
    snapshot = client.get('/api/runs/' + run.json()['id']).json()['snapshot']
    changed = deepcopy(body)
    changed['skills'][0]['parameters'] = {'focus': 'regression', 'language': 'zh'}
    edited = client.put('/api/agents/' + agent['id'], json=changed)
    assert edited.status_code == 200, edited.text
    assert edited.json()['skills'][0]['version'] == 1
    assert edited.json()['skills'][0]['parameters']['focus'] == 'regression'
    assert client.get('/api/skills/' + skill['id']).json() == original
    assert client.get('/api/runs/' + run.json()['id']).json()['snapshot'] == snapshot
    assert snapshot['agents'][agent['id']]['skills'][0]['parameters'] == {'focus': 'security'}
    assert snapshot['agents'][agent['id']]['skills'][0]['digest'] == skill['digest']
