"""Role documents stay non-executable, immutable references for team design."""
import copy
import json

from forge import skill_store as store, skills
from forge.db import session, Skill, SkillRevision
from test_skill_store import ROLE, LICENSE, upload, get_source, native


def test_role_documents_are_not_silently_converted_to_skill_packages():
    candidates, rejected = store.scan_catalog(
        {'engineering/example.md': ROLE, 'LICENSE': LICENSE}, 'agency-agents')
    assert not rejected
    assert len(candidates) == 1
    role = candidates[0]
    assert role['details']['kind'] == 'role_template'
    assert 'SKILL.md' not in role['files']
    assert role['files']['ROLE.md'] == ROLE
    assert role['files']['references/forge-upstream-license.txt'] == LICENSE
    assert any('编辑素材' in warning for warning in role['details']['warnings'])
    assert not any('只能加入可编辑团队草稿' in warning for warning in role['details']['warnings'])


def test_catalogs_separate_all_role_rows_without_installing_them(client):
    entries = {f'repo/engineering/example-{i}.md': ROLE.replace(
        'Example Frontend Specialist', f'Example Specialist {i}') for i in range(272)}
    result = upload(client, 'agency-agents', {'repo/LICENSE': LICENSE, **entries})
    assert result['summary']['counts'] == {'skill': 0, 'role_template': 272}
    skills = client.get('/api/skill-store/items').json()
    assert skills['total'] == 3
    assert all(item['kind'] == 'skill' for item in skills['items'])
    roles = client.get('/api/role-templates/items?limit=100').json()
    assert roles['total'] == 272 and roles['has_more'] and len(roles['items']) == 100
    assert all(item['kind'] == 'role_template' for item in roles['items'])
    assert client.get('/api/role-templates/items?q=Specialist+271').json()['total'] == 1
    assert client.get('/api/skill-store/items?kind=role_template').status_code == 422
    detail = client.get('/api/role-templates/items/' + roles['items'][0]['id']).json()
    assert detail['document']['content'].startswith('---\nname: Example Specialist')
    assert detail['document']['format'] == 'markdown' and len(detail['versions']) == 1
    assert 'SKILL.md' not in detail['files']
    assert get_source(client, 'agency-agents')['counts'] == {'skill': 0, 'role_template': 272}
    assert client.get('/api/skills').json() == []


def role_item(client, text=ROLE):
    upload(client, 'agency-agents', {'repo/LICENSE': LICENSE, 'repo/engineering/example.md': text})
    page = client.get('/api/role-templates/items').json()
    assert page['total'] == 1
    return page['items'][0]


def reference(item):
    return {'item_id': item['id'], 'revision_id': item['revision_id'], 'digest': item['digest']}


def test_historical_role_usage_notice_is_displayed_neutrally_without_revising_saved_content(client):
    item = role_item(client)
    old = '角色模板定义职责，只能加入可编辑团队草稿，不是可安装的Skill。'
    with session() as s:
        revision = s.get(store.StoreRevision, item['revision_id'])
        details = copy.deepcopy(revision.details)
        details['warnings'] = [old, '原文不得扩权。']
        revision.details = details
    response = client.get('/api/role-templates/items/' + item['id']).json()
    assert any('编辑素材' in warning for warning in response['warnings'])
    assert old not in response['warnings'] and '原文不得扩权。' in response['warnings']
    assert response['revision_id'] == item['revision_id'] and response['digest'] == item['digest']
    with session() as s:
        assert s.get(store.StoreRevision, item['revision_id']).details['warnings'] == [old, '原文不得扩权。']


def test_explicit_role_proposal_preserves_reference_without_activating_objects(client):
    item = role_item(client, ROLE.replace('color: cyan', 'allowed-tools: Bash(git:*) Write\ncolor: cyan'))
    before = client.get('/api/agents').json()
    data = {**reference(item), 'role': 'reviewer'}
    data.pop('item_id')
    response = client.post('/api/role-templates/items/' + item['id'] + '/proposal', json=data)
    assert response.status_code == 200, response.text
    result = response.json()
    member = result['member']
    assert member['role'] == 'reviewer'
    assert member['model_profile_id'] is None and not member['skills']
    assert member['role_template_ref'] == reference(item)
    assert member['tool_permissions'] == ['finish', 'request_decision']
    assert 'Bash(git:*) Write' in member['instructions']
    assert '原始角色文档' in member['instructions']
    assert result['provenance']['digest'] == item['digest']
    assert client.get('/api/agents').json() == before
    assert client.get('/api/skills').json() == []
    assert client.get('/api/design/assets').json() == []


def test_compose_duplicate_role_selections_creates_only_an_editable_team_draft(client):
    item = role_item(client)
    before = client.get('/api/agents').json()
    response = client.post('/api/role-templates/compose', json={
        'name': 'Reviewed later team', 'items': [reference(item), reference(item)]})
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft['kind'] == 'team' and draft['status'] == 'draft'
    assert draft['published_version'] is None and draft['source'] == 'manual'
    members = draft['config']['members']
    assert len(members) == 2 and len({m['name'].casefold() for m in members}) == 2
    assert len({m['key'] for m in members}) == 2
    assert all(m['role_template_ref'] == reference(item) for m in members)
    assert all(m['model_profile_id'] is None for m in members)
    assert draft['generation']['method'] == 'role_templates'
    assert draft['generation']['model_requests'] == 0
    assert len(draft['generation']['sources']) == 2
    assert client.get('/api/agents').json() == before
    assert client.get('/api/skills').json() == []
    assert client.post('/api/design/assets/' + draft['id'] + '/publish', json={
        'version': 1, 'digest': draft['digest']}).status_code == 422


def legacy_role(client, *, installed=False, republished=False):
    """Fixture a pre-separation catalog exactly as the previous release stored it."""
    item = role_item(client)
    with session() as s:
        entry = s.get(store.StoreItem, item['id'])
        revision = s.get(store.StoreRevision, item['revision_id'])
        files = native('legacy-role-wrapper')
        files['references/original-agent.md'] = ROLE
        origin = {**revision.origin, 'adapter': 'forge-role-reference-v1'}
        files['assets/forge-store-origin.json'] = json.dumps(origin)
        if republished:
            files['references/previous-store-origin.json'] = files['assets/forge-store-origin.json']
            files['assets/forge-store-origin.json'] = json.dumps({'adapter': 'team-publish-v1'})
            revision.details = {**revision.details, 'kind': 'skill', 'adapter': 'team-publish-v1'}
        revision.files = files
        revision.digest = skills.checksum(files)
        item['digest'] = revision.digest
        if installed:
            row, local = skills.save(s, files, 'legacy-fixture')
            local.status = 'approved'
            entry.skill_id = row.id
            entry.installed_version = 1
            s.add(store.StoreInstall(revision_id=revision.id, item_id=entry.id,
                                    skill_id=row.id, skill_version=1, digest=local.digest))
            item['local'] = {'id': row.id, 'version': 1, 'digest': local.digest}
    return item


def test_legacy_role_original_and_history_readable_but_install_review_bind_publish_rejected(client):
    item = legacy_role(client, installed=True)
    detail = client.get('/api/role-templates/items/' + item['id']).json()
    assert detail['document']['content'] == ROLE and detail['document']['path'] == 'references/original-agent.md'
    assert 'SKILL.md' not in detail['files']
    assert detail['legacy_skill_id'] == item['local']['id']
    assert client.get('/api/skill-store/items/' + item['id']).status_code == 422
    assert client.post('/api/skill-store/items/' + item['id'] + '/install', json={
        'revision_id': item['revision_id'], 'digest': item['digest'], 'reviewed_source': True}).status_code == 422
    agent = client.get('/api/agents').json()[0]
    assert client.post('/api/skill-store/items/' + item['id'] + '/bind', json={
        'revision_id': item['revision_id'], 'agent_id': agent['id'],
        'expected_agent_version': agent['version']}).status_code == 422
    assert client.post('/api/skills/' + item['local']['id'] + '/review', json={
        **{k: v for k, v in item['local'].items() if k != 'id'}, 'action': 'approve'}).status_code == 422
    assert client.post('/api/skill-store/publish', json={
        'skill_id': item['local']['id'], 'version': 1, 'digest': item['local']['digest'],
        'title': 'Must not masquerade as native Skill'}).status_code == 422
    assert client.get('/api/skills/' + item['local']['id']).json()['files']['references/original-agent.md'] == ROLE


def test_legacy_republished_role_is_not_laundered_into_native_skill_catalog(client):
    item = legacy_role(client, republished=True)
    assert client.get('/api/skill-store/items').json()['total'] == 3
    assert client.get('/api/role-templates/items').json()['total'] == 1
    assert client.get('/api/role-templates/items/' + item['id']).json()['kind'] == 'role_template'
    response = client.post('/api/skill-store/items/' + item['id'] + '/install', json={
        'revision_id': item['revision_id'], 'digest': item['digest'], 'reviewed_source': True})
    assert response.status_code == 422


def test_exact_reference_rejects_tampered_digest_but_old_reviewed_version_stays_readable(client):
    item = role_item(client)
    before = client.get('/api/agents').json()
    body = {'name': 'No silent update', 'items': [dict(reference(item), digest='0' * 64)]}
    assert client.post('/api/role-templates/compose', json=body).status_code == 409
    updated = role_item(client, ROLE + '\nUpdated responsibilities for the next revision.\n')
    assert updated['revision_id'] != item['revision_id']
    old = client.get('/api/role-templates/items/' + item['id'],
                     params={'revision_id': item['revision_id']}).json()
    assert old['document']['content'] == ROLE and len(old['versions']) == 2
    body['items'] = [reference(item)]
    result = client.post('/api/role-templates/compose', json=body)
    assert result.status_code == 201, result.text
    assert 'Updated responsibilities' not in result.json()['config']['members'][0]['instructions']
    assert client.get('/api/agents').json() == before


def test_long_role_document_is_explicitly_excerpted_and_complete_original_retained(client):
    original = ROLE + '\n' + 'Detailed responsibility paragraph.\n' * 700 + 'END-OF-ORIGINAL'
    item = role_item(client, original)
    response = client.post('/api/role-templates/compose', json={'name': 'Long role', 'items': [reference(item)]})
    assert response.status_code == 201, response.text
    draft = response.json()
    member = draft['config']['members'][0]
    assert len(member['instructions']) <= 20000
    assert '明确节选' in member['instructions']
    assert draft['generation']['warnings']
    assert draft['generation']['sources'][0]['document_chars'] == len(original)
    full = client.get(draft['generation']['sources'][0]['document_url']).json()
    assert full['document']['content'] == original


def test_disabled_role_source_keeps_history_but_refuses_new_proposals(client):
    item = role_item(client)
    source = get_source(client, 'agency-agents')
    result = client.post('/api/skill-store/sources/agency-agents/enabled', json={
        'expected_version': source['version'], 'enabled': False})
    assert result.status_code == 200
    assert client.get('/api/role-templates/items/' + item['id']).status_code == 200
    assert client.post('/api/role-templates/compose', json={
        'name': 'Unavailable', 'items': [reference(item)]}).status_code == 409
    assert client.get('/api/design/assets').json() == []


def test_role_catalog_viewer_can_read_but_cannot_compose_or_propose(client):
    from forge.db import User, uid
    from forge.security import password_hash
    item = role_item(client)
    with session() as s:
        s.add(User(id=uid(), username='role-viewer', password_hash=password_hash('Viewer-Passw0rd-test'), role='viewer'))
    client.post('/api/auth/logout')
    assert client.get('/api/role-templates/items').status_code == 401
    client.post('/api/auth/login', json={'username': 'role-viewer', 'password': 'Viewer-Passw0rd-test'})
    assert client.get('/api/role-templates/items').status_code == 200
    assert client.get('/api/role-templates/items/' + item['id']).status_code == 200
    assert client.post('/api/role-templates/compose', json={
        'name': 'Unauthorized', 'items': [reference(item)]}).status_code == 403
    assert client.post('/api/role-templates/items/' + item['id'] + '/proposal', json={
        'revision_id': item['revision_id'], 'digest': item['digest']}).status_code == 403
