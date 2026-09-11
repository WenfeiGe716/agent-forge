"""Exercise provider-specific replacement through every real discovery adapter."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from forge import providers
from forge.db import session
from forge.provider_catalog import ProviderCatalog


PROTOCOLS = [*providers.PARAMETERS, 'custom_http']


def confirmation(catalog):
    return {key: catalog[key] for key in ('revision_id', 'catalog_digest')} | {'acknowledged': True}


def model_settings(protocol):
    parameter = {'openai_chat': 'max_tokens', 'anthropic_messages': 'max_tokens',
                 'openai_responses': 'max_output_tokens', 'google_gemini': 'maxOutputTokens'}.get(protocol)
    return {'allowed_parameters': [parameter] if parameter else [],
            'parameters': {parameter: 512} if parameter else {}, 'supports_tools': False,
            'enabled': True, 'context_window': 8192, 'input_cost_per_million': 1.25,
            'output_cost_per_million': 3.75}


def setup_accounts(client, monkeypatch, protocol):
    accounts, initial_profiles, inventory, calls = {}, {}, {}, []
    for account in ('a', 'b'):
        body = {'name': 'Account ' + account, 'protocol': protocol,
                'base_url': f'https://catalog-{account}.example/v1', 'external': True,
                'api_key': 'synthetic-replace-fixture-' + account}
        if protocol == 'custom_http':
            body['protocol_config'] = {
                'endpoint': '/generate',
                'body': {'model': '{{model}}', 'messages': '{{messages}}'},
                'response': {'text': '/answer'},
                'discovery': {'endpoint': '/inventory', 'items': '/entries', 'id': '/id', 'name': '/name'},
            }
        response = client.post('/api/providers', json=body)
        assert response.status_code == 201
        accounts[account] = response.json()['id']
        initial_profiles[account] = {}
        for model_id in ('shared', 'old-' + account):
            response = client.post('/api/models', json={
                'provider_id': accounts[account], 'model_id': model_id,
                'name': f'Human {account} {model_id}', **model_settings(protocol)})
            assert response.status_code == 201, response.text
            initial_profiles[account][model_id] = response.json()
        inventory[account] = [{'id': 'shared', 'name': 'Upstream ' + account},
                              {'id': 'new-' + account, 'name': 'New ' + account}]

    # Bind the exact profiles before any pull. Their IDs must survive replacement.
    agent = client.post('/api/agents', json={'name': 'Replacement isolation member', 'role': 'developer',
        'model_profile_id': initial_profiles['a']['shared']['id'],
        'fallback_model_ids': [initial_profiles['b']['shared']['id']]})
    assert agent.status_code == 201, agent.text

    original_discover = providers.discover

    def respond(request):
        account = {'catalog-a.example': 'a', 'catalog-b.example': 'b'}[request.url.host]
        assert request.method == 'GET'
        assert request.url.path == ('/v1/inventory' if protocol == 'custom_http' else '/v1/models')
        assert request.headers['cache-control'] == 'no-cache'
        auth = 'x-api-key' if protocol == 'anthropic_messages' else 'x-goog-api-key' if protocol == 'google_gemini' else 'authorization'
        expected_key = 'synthetic-replace-fixture-' + account
        assert request.headers[auth] == (expected_key if auth != 'authorization' else 'Bearer ' + expected_key)
        calls.append(account)
        rows = deepcopy(inventory[account])
        if protocol == 'google_gemini':
            return httpx.Response(200, json={'models': [{'name': 'models/' + row['id'],
                'displayName': row['name'], 'inputTokenLimit': 120000} for row in rows]})
        return httpx.Response(200, json={'entries' if protocol == 'custom_http' else 'data': rows})

    async def mock_discover(connection, key, **kwargs):
        assert connection['protocol'] == protocol
        return await original_discover(connection, key, transport=httpx.MockTransport(respond))

    async def never_infer(*args, **kwargs):
        pytest.fail('Catalog synchronization or replacement must not invoke a model')

    monkeypatch.setattr(providers, 'discover', mock_discover)
    monkeypatch.setattr(providers, 'complete', never_infer)
    return accounts, initial_profiles, inventory, calls


def pull(client, provider_id):
    response = client.post(f'/api/providers/{provider_id}/catalog/sync')
    assert response.status_code == 200, response.text
    return response.json()


def replace(client, provider_id, catalog):
    response = client.post(f'/api/providers/{provider_id}/catalog/replace', json=confirmation(catalog))
    assert response.status_code == 201, response.text
    return response.json()


def models_for(client, provider_id):
    return [model for model in client.get('/api/models').json() if model['provider_id'] == provider_id]


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_full_replacement_is_scoped_to_connection_even_for_identical_upstream_model_ids(client, monkeypatch, protocol):
    accounts, initial, inventory, calls = setup_accounts(client, monkeypatch, protocol)
    a, b = accounts['a'], accounts['b']
    before_models, before_agents = client.get('/api/models').json(), client.get('/api/agents').json()
    catalog_a, catalog_b = pull(client, a), pull(client, b)
    assert calls == ['a', 'b']
    assert client.get('/api/models').json() == before_models  # Pulling alone cannot apply.
    assert catalog_a['provider_id'] == a and catalog_b['provider_id'] == b
    assert {row['id'] for row in catalog_a['models']} == {'shared', 'new-a'}
    assert {row['id'] for row in catalog_b['models']} == {'shared', 'new-b'}
    assert next(row for row in catalog_a['models'] if row['id'] == 'shared')['name'] == 'Upstream a'
    assert next(row for row in catalog_b['models'] if row['id'] == 'shared')['name'] == 'Upstream b'

    changed_a = replace(client, a, catalog_a)
    assert changed_a['kept'] == [initial['a']['shared']]
    assert [row['id'] for row in changed_a['retired']] == [initial['a']['old-a']['id']]
    assert {row['model_id'] for row in changed_a['created']} == {'new-a'}
    assert changed_a['created'][0]['provider_id'] == a
    assert changed_a['created'][0]['enabled'] is False
    assert changed_a['created'][0]['supports_tools'] is False
    assert changed_a['created'][0]['parameters'] == {}
    assert changed_a['catalog']['counts']['configured'] == 2
    assert changed_a['catalog']['retired_profile_ids'] == [initial['a']['old-a']['id']]
    assert models_for(client, b) == list(initial['b'].values())
    assert client.get(f'/api/providers/{b}/catalog').json() == catalog_b
    assert client.get('/api/agents').json() == before_agents

    after_a = models_for(client, a)
    changed_b = replace(client, b, catalog_b)
    assert changed_b['kept'] == [initial['b']['shared']]
    assert {row['model_id'] for row in changed_b['created']} == {'new-b'}
    assert changed_b['created'][0]['provider_id'] == b
    assert changed_b['created'][0]['id'] != changed_a['created'][0]['id']
    assert models_for(client, a) == after_a
    assert client.get('/api/agents').json() == before_agents

    # Removing a shared upstream ID from A must not retire or edit B's shared ID.
    after_b, after_b_catalog = models_for(client, b), client.get(f'/api/providers/{b}/catalog').json()
    inventory['a'][:] = [{'id': 'new-a', 'name': 'New a'}]
    removed_a = replace(client, a, pull(client, a))
    assert [row['id'] for row in removed_a['retired']] == [initial['a']['shared']['id']]
    assert removed_a['retired'][0]['enabled'] is False
    assert models_for(client, b) == after_b
    assert client.get(f'/api/providers/{b}/catalog').json() == after_b_catalog
    assert client.get('/api/agents').json() == before_agents


@pytest.mark.parametrize('protocol', PROTOCOLS)
@pytest.mark.parametrize('change,status', [('ack', 422), ('revision', 409), ('digest', 409), ('stale', 409), ('other-provider', 409)])
def test_replacement_requires_fresh_review_of_the_exact_connection_catalog(client, monkeypatch, protocol, change, status):
    accounts, _, _, _ = setup_accounts(client, monkeypatch, protocol)
    a = accounts['a']
    catalog = pull(client, a)
    body = confirmation(catalog)
    if change == 'ack': body['acknowledged'] = False
    elif change == 'revision': body['revision_id'] = '0' * 32
    elif change == 'digest': body['catalog_digest'] = '0' * 64
    elif change == 'other-provider': body = confirmation(pull(client, accounts['b']))
    else:
        with session() as s:
            cache = s.get(ProviderCatalog, f'{a}:{catalog["revision_id"]}')
            cache.last_synced_at = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    before_models, before_agents = client.get('/api/models').json(), client.get('/api/agents').json()
    response = client.post(f'/api/providers/{a}/catalog/replace', json=body)
    assert response.status_code == status, response.text
    assert client.get('/api/models').json() == before_models
    assert client.get('/api/agents').json() == before_agents


@pytest.mark.parametrize('protocol', PROTOCOLS)
def test_viewer_can_read_each_protocol_catalog_but_cannot_pull_or_replace(client, monkeypatch, protocol):
    accounts, _, _, calls = setup_accounts(client, monkeypatch, protocol)
    a = accounts['a']
    catalog = pull(client, a)
    before_models, before_agents = client.get('/api/models').json(), client.get('/api/agents').json()
    password = 'read-only-replace-fixture-123'
    response = client.post('/api/users', json={'username': 'replace-viewer', 'password': password, 'role': 'viewer'})
    assert response.status_code == 201, response.text
    client.post('/api/auth/logout')
    assert client.post(f'/api/providers/{a}/catalog/replace', json=confirmation(catalog)).status_code == 401
    assert client.post('/api/auth/login', json={'username': 'replace-viewer', 'password': password}).status_code == 200
    assert client.get(f'/api/providers/{a}/catalog').json() == catalog
    assert client.post(f'/api/providers/{a}/catalog/sync').status_code == 403
    assert client.post(f'/api/providers/{a}/catalog/replace', json=confirmation(catalog)).status_code == 403
    assert calls == ['a']
    assert client.get('/api/models').json() == before_models
    assert client.get('/api/agents').json() == before_agents


@pytest.mark.parametrize('metadata', [{'has_more': True, 'last_id': 'two'},
                                    {'nextPageToken': 'two'}, {'error': {'message': 'Unavailable'}}])
def test_incomplete_custom_pull_keeps_last_success_and_blocks_full_replacement(client, monkeypatch, metadata):
    original_discover = providers.discover
    accounts, _, _, _ = setup_accounts(client, monkeypatch, 'custom_http')
    a = accounts['a']
    good = pull(client, a)
    before_models, before_agents = client.get('/api/models').json(), client.get('/api/agents').json()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'entries': [{'id': 'only-the-first-page', 'name': 'Incomplete'}]} | metadata)

    async def incomplete(connection, key, **kwargs):
        return await original_discover(connection, key, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(providers, 'discover', incomplete)
    failed = client.post(f'/api/providers/{a}/catalog/sync')
    assert failed.status_code == 502, failed.text
    assert len(requests) == 1
    cached = client.get(f'/api/providers/{a}/catalog').json()
    assert cached['models'] == good['models']
    assert cached['catalog_digest'] == good['catalog_digest']
    assert cached['last_synced_at'] == good['last_synced_at']
    assert cached['last_pull'] == good['last_pull']
    assert cached['sync_status'] == 'error'
    assert client.post(f'/api/providers/{a}/catalog/replace', json=confirmation(good)).status_code == 409
    assert client.get('/api/models').json() == before_models
    assert client.get('/api/agents').json() == before_agents
