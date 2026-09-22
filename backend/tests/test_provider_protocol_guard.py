"""Provider revisions must not strand saved model configurations or Agent overrides."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent, get_ident
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from forge.db import Event, ModelProfile, ProviderRevision, Run, engine, session, uid
from forge.model_hub import ModelRevision
from forge.provider_catalog import CatalogModelState


def provider(client, protocol='openai_chat', **changes):
    body = {'name': 'Guard provider', 'protocol': protocol,
            'base_url': 'https://guard.example/v1', 'api_key': 'synthetic-old-key', **changes}
    response = client.post('/api/providers', json=body)
    assert response.status_code == 201, response.text
    return response.json()['id'], body


def model(client, pid, **changes):
    body = {'name': 'GLM review configuration', 'provider_id': pid,
            'model_id': 'bailian/glm-5.2', 'allowed_parameters': ['max_tokens'],
            'parameters': {'max_tokens': 4096}, **changes}
    response = client.post('/api/models', json=body)
    assert response.status_code == 201, response.text
    return response.json()


def state(client):
    with session() as s:
        return {'providers': client.get('/api/providers').json(),
                'models': client.get('/api/models').json(),
                'agents': client.get('/api/agents').json(),
                'revisions': [(r.id, deepcopy(r.config), r.encrypted_key) for r in s.scalars(select(ProviderRevision))],
                'model_versions': [(r.id, deepcopy(r.config)) for r in s.scalars(select(ModelRevision))],
                'events': [(r.id, r.kind, deepcopy(r.data)) for r in s.scalars(select(Event))]}


def assert_context(detail, item, protocol):
    assert isinstance(detail, str)
    for value in (item['name'], item['model_id'], item['id'], protocol):
        assert value in detail


@pytest.mark.parametrize('enabled', [True, False])
def test_incompatible_protocol_switch_rejected_before_any_revision_key_or_audit(client, monkeypatch, enabled):
    pid, body = provider(client)
    item = model(client, pid, enabled=enabled)
    before = state(client)
    from forge import api
    encrypt_calls = []
    original_encrypt = api.encrypt
    def track_encrypt(value):
        encrypt_calls.append(True)
        return original_encrypt(value)
    monkeypatch.setattr(api, 'encrypt', track_encrypt)
    response = client.put('/api/providers/' + pid, json={**body, 'protocol': 'openai_responses',
        'name': 'Must not persist', 'api_key': 'synthetic-new-key', 'enabled': False})
    assert response.status_code == 422, response.text
    assert not encrypt_calls
    detail = response.json()['detail']
    assert_context(detail, item, 'openai_responses')
    assert 'max_tokens' in detail and 'max_output_tokens' in detail
    assert '新建' in detail and 'synthetic' not in detail
    assert state(client) == before


def test_compatible_switch_preserves_models_bindings_and_frozen_runs(client):
    pid, body = provider(client)
    model(client, pid, allowed_parameters=['temperature'], parameters={'temperature': 0.5})
    frozen = {'connection': {'protocol': 'openai_chat'}, 'immutable': 'old run'}
    with session() as s:
        run = Run(id=uid(), project_id='historical-project', owner_id='historical-user',
                  title='Frozen run', request='history', snapshot=deepcopy(frozen), status='completed')
        s.add(run)
        run_id = run.id
    before = state(client)
    response = client.put('/api/providers/' + pid, json={**body, 'protocol': 'openai_responses'})
    assert response.status_code == 200, response.text
    after = state(client)
    assert after['models'] == before['models'] and after['agents'] == before['agents']
    assert after['model_versions'] == before['model_versions']
    assert len(after['revisions']) == len(before['revisions']) + 1
    with session() as s:
        assert s.get(Run, run_id).snapshot == frozen


def custom_config(**changes):
    return {'endpoint': '/generate', 'body': {'model': '{{model}}', 'messages': '{{messages}}',
            'parameters': '{{parameters}}'}, 'response': {'text': '/answer'},
            'supported_parameters': ['output_limit'],
            'parameter_schema': {'output_limit': {'type': 'integer', 'maximum': 1000}}, **changes}


@pytest.mark.parametrize('change', ['schema', 'supported', 'tool-mapping'])
def test_same_custom_protocol_edit_validates_saved_models(client, change):
    config = custom_config()
    if change == 'tool-mapping':
        config['body']['tools'] = '{{tools}}'
        config['response'].update(calls='/calls', call_fields={'id': '/id', 'name': '/name', 'arguments': '/arguments'})
    pid, body = provider(client, 'custom_http', protocol_config=config)
    item = model(client, pid, allowed_parameters=['output_limit'], parameters={'output_limit': 512},
                 supports_tools=change == 'tool-mapping', enabled=False)
    proposed = deepcopy(config)
    if change == 'schema': proposed['parameter_schema']['output_limit']['maximum'] = 100
    elif change == 'supported': proposed.update(supported_parameters=[], parameter_schema={})
    else: proposed['response'] = {'text': '/answer'}
    before = state(client)
    response = client.put('/api/providers/' + pid, json={**body, 'protocol_config': proposed})
    assert response.status_code == 422, response.text
    assert_context(response.json()['detail'], item, 'custom_http')
    assert ('calls' if change == 'tool-mapping' else 'output_limit') in response.json()['detail']
    assert state(client) == before


def test_protocol_guard_ignores_other_providers_and_retired_history(client):
    pid, body = provider(client)
    retired = model(client, pid, enabled=False)
    with session() as s:
        s.add(CatalogModelState(id=retired['id'], provider_id=pid, retired=True))
    other, _ = provider(client)
    model(client, other)
    model(client, pid, name='Current compatible', allowed_parameters=[], parameters={})
    before = state(client)
    response = client.put('/api/providers/' + pid, json={**body, 'protocol': 'openai_responses'})
    assert response.status_code == 200, response.text
    assert state(client)['models'] == before['models']


@pytest.mark.parametrize('binding', ['model_profile_id', 'fallback_model_ids'])
def test_provider_guard_checks_bound_agent_overrides_including_disabled_agents(client, binding):
    pid, body = provider(client)
    item = model(client, pid, allowed_parameters=['max_tokens', 'temperature'])
    agent = client.post('/api/agents', json={'name': 'Human configured override', 'role': 'developer',
        'enabled': False, binding: [item['id']] if binding == 'fallback_model_ids' else item['id'],
        'model_parameters': {'temperature': 1.5}})
    assert agent.status_code == 201, agent.text
    before = state(client)
    response = client.put('/api/providers/' + pid, json={**body, 'protocol': 'anthropic_messages'})
    assert response.status_code == 422, response.text
    detail = response.json()['detail']
    assert_context(detail, item, 'anthropic_messages')
    assert 'Human configured override' in detail and agent.json()['id'] in detail and 'temperature' in detail
    assert state(client) == before


def test_bulk_preview_and_apply_report_all_invalid_models_atomically(client):
    pid, body = provider(client, 'openai_responses')
    items = [model(client, pid, name='Duplicate name', allowed_parameters=['max_output_tokens'],
                   parameters={'max_output_tokens': 4096}, enabled=False) for _ in range(2)]
    revision_id = next(p['revision_id'] for p in client.get('/api/providers').json() if p['id'] == pid)
    payload = {'revision_id': revision_id, 'enabled': True,
               'selections': [{'id': m['id'], 'version': m['version']} for m in items]}
    # Legacy configurations bypassed provider protocol validation before this fix.
    with session() as s:
        for item in items:
            row = s.get(ModelProfile, item['id'])
            row.config = {**deepcopy(row.config), 'allowed_parameters': ['max_tokens'], 'parameters': {'max_tokens': 4096}}
    before = state(client)
    for suffix, extra in [('preview', {}), ('', {'digest': '0' * 64, 'acknowledged': True})]:
        response = client.post(f'/api/providers/{pid}/models/bulk-state' + ('/' + suffix if suffix else ''), json={**payload, **extra})
        assert response.status_code == 422, response.text
        for item in items: assert_context(response.json()['detail'], item, 'openai_responses')
        assert 'max_output_tokens' in response.json()['detail']
        assert state(client) == before
    payload['enabled'] = False
    assert client.post(f'/api/providers/{pid}/models/bulk-state/preview', json=payload).status_code == 200


@pytest.mark.parametrize('existing', [False, True])
def test_model_save_error_identifies_exact_configuration(client, existing):
    pid, _ = provider(client, 'openai_responses')
    item = model(client, pid, allowed_parameters=['max_output_tokens'], parameters={'max_output_tokens': 4096})
    body = {k: v for k, v in item.items() if k not in ('id', 'version')}
    body.update(allowed_parameters=['max_tokens'], parameters={'max_tokens': 4096})
    before = state(client)
    response = (client.put('/api/models/' + item['id'], json=body) if existing else client.post('/api/models', json=body))
    assert response.status_code == 422, response.text
    detail = response.json()['detail']
    for value in (item['name'], item['model_id'], 'openai_responses', 'max_output_tokens'): assert value in detail
    assert (item['id'] if existing else '未保存') in detail
    assert state(client) == before


def test_repair_legacy_responses_configuration_versions_then_unblocks_original_batch(client):
    pid, _ = provider(client, 'openai_responses')
    broken = model(client, pid, allowed_parameters=['max_output_tokens'], parameters={'max_output_tokens': 4096})
    sibling = model(client, pid, name='Other enabled config', allowed_parameters=[], parameters={}, enabled=False)
    # Recreate the legacy pre-guard persistence pattern including its history.
    with session() as s:
        row = s.get(ModelProfile, broken['id'])
        row.config = {**deepcopy(row.config), 'allowed_parameters': ['max_tokens'], 'parameters': {'max_tokens': 4096}}
        s.get(ModelRevision, broken['id'] + ':1').config = deepcopy(row.config)
    before = state(client)
    body = {k: v for k, v in broken.items() if k not in ('id', 'version')}
    response = client.put('/api/models/' + broken['id'], json=body)
    assert response.status_code == 200, response.text
    fixed = response.json()
    assert fixed['version'] == 2
    assert fixed['parameters'] == {'max_output_tokens': 4096}
    assert fixed['allowed_parameters'] == ['max_output_tokens']
    after = state(client)
    assert after['providers'] == before['providers'] and after['agents'] == before['agents']
    returned_sibling = next(m for m in after['models'] if m['id'] == sibling['id'])
    assert {k: v for k, v in returned_sibling.items() if k != 'capability'} == sibling
    assert returned_sibling['capability']['max_output_tokens'] == 4096
    with session() as s:
        assert s.get(ModelRevision, broken['id'] + ':1').config['parameters'] == {'max_tokens': 4096}
        assert s.get(ModelRevision, broken['id'] + ':2').config['parameters'] == {'max_output_tokens': 4096}
    revision_id = next(p['revision_id'] for p in after['providers'] if p['id'] == pid)
    response = client.post(f'/api/providers/{pid}/models/bulk-state/preview', json={
        'revision_id': revision_id, 'enabled': True,
        'selections': [{'id': m['id'], 'version': m['version']} for m in (fixed, sibling)]})
    assert response.status_code == 200, response.text
    assert response.json()['counts']['selected'] == 2
    assert state(client) == after  # Preview is never an enable operation.


@pytest.mark.parametrize('writer', ['agent', 'model'])
def test_protocol_switch_serializes_against_model_and_agent_parameter_writes(client, monkeypatch, writer):
    from forge import api
    from forge.schema import AgentInput, ModelInput, ProviderInput
    pid, body = provider(client)
    item = model(client, pid, allowed_parameters=['max_tokens', 'temperature'], parameters={'max_tokens': 4096})
    actor = SimpleNamespace(id='concurrency-test')
    held, release, write_attempted = ThreadEvent(), ThreadEvent(), ThreadEvent()
    owner_thread = []
    original = api.validate_provider_change

    def pause_under_provider_lock(s, provider_id, connection):
        owner_thread.append(get_ident())
        held.set()
        assert release.wait(5)
        return original(s, provider_id, connection)

    def observe_lock_attempt(connection, cursor, statement, parameters, context, many):
        if (held.is_set() and owner_thread != [get_ident()] and statement.lower().startswith('update providers')):
            write_attempted.set()

    def concurrent_write():
        if writer == 'agent':
            return api.save_agent(AgentInput(name='Concurrent override', role='developer',
                model_profile_id=item['id'], model_parameters={'temperature': 1.5}), actor)
        config = {k: v for k, v in item.items() if k not in ('id', 'version')}
        config['parameters'] = {'max_tokens': 4096, 'temperature': 1.5}
        return api.save_model(ModelInput(**config), actor, item['id'])

    monkeypatch.setattr(api, 'validate_provider_change', pause_under_provider_lock)
    event.listen(engine, 'before_cursor_execute', observe_lock_attempt)
    before = state(client)
    try:
        with ThreadPoolExecutor() as pool:
            switch = pool.submit(api.save_provider, ProviderInput(**{**body, 'protocol': 'anthropic_messages'}), actor, pid)
            assert held.wait(5)
            write = pool.submit(concurrent_write)
            try:
                assert write_attempted.wait(5), 'Concurrent writer must acquire the shared provider lock'
                assert not write.done()
            finally:
                release.set()
            assert switch.result(5)['id'] == pid
            with pytest.raises(ValueError, match='temperature'):
                write.result(5)
    finally:
        release.set()
        event.remove(engine, 'before_cursor_execute', observe_lock_attempt)
    after = state(client)
    assert after['models'] == before['models'] and after['agents'] == before['agents']
    assert after['model_versions'] == before['model_versions']
