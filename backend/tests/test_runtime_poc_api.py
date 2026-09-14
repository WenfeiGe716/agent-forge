from types import SimpleNamespace

from forge.runtime_poc_api import CodexPocInput, _run_provider_snapshot, _run_leader_execution_policy


def test_poc_input_rejects_raw_api_key_and_unknown_fields():
    data = CodexPocInput(provider_id='provider', model_profile_id='model', prompt='hello')
    assert data.prompt == 'hello'
    try:
        CodexPocInput(provider_id='provider', model_profile_id='model', prompt='hello', api_key='secret')
    except ValueError:
        pass
    else:
        raise AssertionError('raw api_key must not be accepted by the POC API')


def test_poc_route_is_disabled_by_default(client):
    response = client.post('/api/runtime/poc-task', json={
        'provider_id': 'provider', 'model_profile_id': 'model', 'prompt': 'hello',
    })
    assert response.status_code == 404
    assert '尚未开启' in response.json()['detail']


def test_formal_codex_run_route_is_disabled_by_default(client):
    response = client.post('/api/runs/missing/codex-start', json={'prompt': 'hello'})
    assert response.status_code == 404
    assert '尚未开启' in response.json()['detail']


def test_poc_input_can_reference_an_existing_run_without_accepting_credentials():
    data = CodexPocInput(provider_id='provider', model_profile_id='model',
                         prompt='hello', run_id='run-1')
    assert data.run_id == 'run-1'


def test_frozen_run_provider_snapshot_uses_leader_model_only():
    run = SimpleNamespace(snapshot={
        'entry_agent_id': 'leader',
        'agents': {'leader': {'models': [{
            'connection': {'protocol': 'openai_responses', 'base_url': 'https://example.test/v1'},
            'credential_revision': 'rev-1',
            'model': {'model_id': 'model-1'},
        }]}}
    })
    connection, revision, model = _run_provider_snapshot(run)
    assert connection['base_url'] == 'https://example.test/v1'
    assert (revision, model) == ('rev-1', 'model-1')


def test_formal_run_uses_frozen_leader_permission_profile():
    run = SimpleNamespace(snapshot={
        'entry_agent_id': 'leader',
        'project': {'permission_profile': 'ask'},
        'agents': {'leader': {'execution_policy': {'profile': 'auto'}}},
    })
    assert _run_leader_execution_policy(run) == {
        'approval_policy': 'on-request',
        'approvals_reviewer': 'auto_review',
        'sandbox': 'workspace-write',
    }
