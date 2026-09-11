"""Role origins survive human edits and task snapshots without granting authority."""
from sqlalchemy import select

from conftest import run_body, seat_agents
from forge import skill_store as store
from forge.db import session
from test_skill_store import ROLE, LICENSE, source, upload
from test_design_center import create, confirm, team


def role_reference(client):
    src = source(client)
    upload(client, src['id'], {'repo/engineering/example.md': ROLE, 'repo/LICENSE': LICENSE})
    with session() as s:
        item = s.scalar(select(store.StoreItem).where(store.StoreItem.source_id == src['id']))
        revision = s.get(store.StoreRevision, item.latest_revision)
        return {'item_id': item.id, 'revision_id': revision.id, 'digest': revision.digest}


def payload(agent):
    return {k: v for k, v in agent.items() if k not in ('id', 'version')}


def test_role_reference_survives_legacy_editor_and_frozen_snapshot(client, configured):
    reference = role_reference(client)
    original = configured['agents'][0]
    body = {**payload(original), 'role_template_ref': reference}
    result = client.put('/api/agents/' + original['id'], json=body)
    assert result.status_code == 200, result.text
    assert result.json()['role_template_ref'] == reference
    # Older member forms do not yet send the provenance field.
    legacy = payload(result.json())
    legacy.pop('role_template_ref')
    legacy['name'] += ' edited'
    updated = client.put('/api/agents/' + original['id'], json=legacy)
    assert updated.status_code == 200, updated.text
    assert updated.json()['role_template_ref'] == reference
    preview = client.post('/api/runs/preview-config', json=run_body(configured))
    assert preview.status_code == 200, preview.text
    snapshot = preview.json()
    assert snapshot['agents'][original['id']]['role_template_ref'] == reference


def test_role_reference_cannot_forge_digest_or_point_to_native_skill(client, configured):
    ref = role_reference(client)
    agent = configured['agents'][0]
    response = client.put('/api/agents/' + agent['id'], json={**payload(agent),
        'role_template_ref': {**ref, 'digest': '0' * 64}})
    assert response.status_code == 409
    with session() as s:
        native = next(row for row in s.scalars(select(store.StoreItem)) if row.source_id == 'forge-builtins')
        revision = s.get(store.StoreRevision, native.latest_revision)
        wrong = {'item_id': native.id, 'revision_id': revision.id, 'digest': revision.digest}
    response = client.put('/api/agents/' + agent['id'], json={**payload(agent), 'role_template_ref': wrong})
    assert response.status_code == 422


def test_reference_has_no_authority_to_expand_role_tools(client, configured):
    ref = role_reference(client)
    reviewer = next(a for a in configured['agents'] if a['role'] == 'reviewer')
    result = client.put('/api/agents/' + reviewer['id'], json={**payload(reviewer),
        'role_template_ref': ref, 'tool_permissions': ['finish', 'run_command']})
    assert result.status_code == 422


def test_published_team_and_runtime_keep_pinned_role_after_source_update(client, configured):
    ref = role_reference(client)
    config = team(configured)
    config['members'][1]['role_template_ref'] = ref
    config['members'][1]['communication'] = {'scope': 'run', 'recipients': []}
    draft = create(client, 'team', config)
    published = confirm(client, draft)
    assert published.status_code == 200, published.text
    instance = confirm(client, draft, 'instantiate')
    assert instance.status_code == 201, instance.text
    seat_agents(client)
    with session() as s:
        item = s.get(store.StoreItem, ref['item_id'])
        sid, path = item.source_id, item.path
    upload(client, sid, {'repo/' + path: ROLE + '\nNew role revision.\n', 'repo/LICENSE': LICENSE})
    snapshot = client.post('/api/runs/preview-config', json=run_body(configured,
        agent_ids=instance.json()['agent_ids'], team_ref={'id':draft['id'], 'version':draft['version']}))
    assert snapshot.status_code == 200, snapshot.text
    nova = next(a for a in snapshot.json()['agents'].values() if a['name'] == 'Nova')
    assert nova['role_template_ref'] == ref
    old = client.get(f'/api/role-templates/items/{ref["item_id"]}', params={'revision_id':ref['revision_id']})
    assert old.status_code == 200
    assert old.json()['digest'] == ref['digest']
