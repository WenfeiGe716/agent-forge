"""Real HTTP/DB retries in an isolated database; dispatch and models never run."""
from sqlalchemy import func, select

from forge.db import Event, Outbox, Run, session
from conftest import run_body


def test_uncertain_creation_can_be_found_after_model_is_disabled_without_a_second_run(client, configured):
    body = run_body(configured, run_project_checks=False, collaboration_flow=False)
    headers = {'Idempotency-Key': 'isolated-direct-send-one'}
    initial = client.post('/api/runs', json=body, headers=headers)
    assert initial.status_code == 202, initial.text
    run_id = initial.json()['id']
    # Simulate the administrator changing current configuration after the server
    # committed creation but before the client received its confirmation.
    model = next(m for m in client.get('/api/models').json() if m['id'] == configured['models'][0])
    changed = client.put('/api/models/' + model['id'], json={
        **{key: value for key, value in model.items() if key not in ('id', 'version')}, 'enabled': False})
    assert changed.status_code == 200, changed.text
    invalid_new = client.post('/api/runs', json=body, headers={'Idempotency-Key': 'new-unrelated-key'})
    assert invalid_new.status_code == 422, invalid_new.text
    recovered = client.post('/api/runs', json=body, headers=headers)
    assert recovered.status_code == 202, recovered.text
    assert recovered.json()['id'] == run_id
    assert recovered.json()['agent_ids'] == initial.json()['agent_ids']
    assert client.post('/api/runs', json={**body, 'request': 'Different goal'}, headers=headers).status_code == 409
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Run)) == 1
        assert s.scalar(select(func.count()).select_from(Outbox)) == 1
        assert s.scalar(select(func.count()).select_from(Event).where(Event.kind == 'run.created')) == 1


def test_idempotent_recovery_preserves_the_original_team_when_applied_layout_changes(client, configured):
    from conftest import seat_agents
    inventory = client.get('/api/execution/policies').json()
    team = inventory['teams'][0]
    body = run_body(configured, office_team_id=team['id'], office_layout_ref=inventory['layout_ref'])
    headers = {'Idempotency-Key': 'isolated-direct-send-layout'}
    initial = client.post('/api/runs', json=body, headers=headers)
    assert initial.status_code == 202, initial.text
    seat_agents(client)
    assert client.get('/api/execution/policies').json()['layout_ref'] != inventory['layout_ref']
    invalid_new = client.post('/api/runs', json=body, headers={'Idempotency-Key': 'another-key'})
    assert invalid_new.status_code in (409, 422), invalid_new.text
    recovered = client.post('/api/runs', json=body, headers=headers)
    assert recovered.status_code == 202 and recovered.json()['id'] == initial.json()['id'], recovered.text
    detail = client.get('/api/runs/' + recovered.json()['id']).json()
    assert detail['snapshot']['design']['organization']['ref'] == inventory['layout_ref']
    with session() as s:
        assert s.scalar(select(func.count()).select_from(Run)) == 1
        assert s.scalar(select(func.count()).select_from(Outbox)) == 1
