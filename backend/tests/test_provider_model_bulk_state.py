from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent

import pytest
from sqlalchemy import select

from forge.db import Agent, Event, ModelProfile, Provider, session, uid
from forge.model_hub import ModelRevision
from forge.provider_catalog import CatalogModelState


def body(client, configured, enabled=False, ids=None):
    pid = configured['providers'][0]
    provider = next(row for row in client.get('/api/providers').json() if row['id'] == pid)
    models = [row for row in client.get('/api/models').json() if row['id'] in (ids or [configured['models'][0]])]
    return pid, {'revision_id': provider['revision_id'], 'enabled': enabled,
                 'selections': [{'id': row['id'], 'version': row['version']} for row in models]}


def review(client, pid, payload):
    return client.post(f'/api/providers/{pid}/models/bulk-state/preview', json=payload)


def apply(client, pid, payload, preview):
    return client.post(f'/api/providers/{pid}/models/bulk-state', json={**payload, 'digest': preview['digest'], 'acknowledged': True})


def test_bulk_preview_is_read_only_and_reports_exact_provider_bindings(client, configured):
    pid, payload = body(client, configured)
    before = client.get('/api/models').json()
    response = review(client, pid, payload)
    assert response.status_code == 200, response.text
    value = response.json()
    assert value['counts']['selected'] == value['counts']['changed'] == 1
    assert value['counts']['primary_bindings'] == 2
    assert [item['id'] for item in value['items']] == [configured['models'][0]]
    assert client.get('/api/models').json() == before


def test_bulk_state_changes_only_enabled_and_keeps_versions_audit_and_agent_bindings(client, configured):
    pid, payload = body(client, configured)
    models, agents = client.get('/api/models').json(), client.get('/api/agents').json()
    response = review(client, pid, payload)
    assert response.status_code == 200, response.text
    changed = apply(client, pid, payload, response.json())
    assert changed.status_code == 200, changed.text
    assert len(changed.json()['changed']) == 1
    after = client.get('/api/models').json()
    for old, new in zip(models, after):
        if old['id'] == configured['models'][0]:
            assert new['enabled'] is False and new['version'] == old['version'] + 1
            assert {key: value for key, value in old.items() if key not in ('version', 'enabled')} == {
                key: value for key, value in new.items() if key not in ('version', 'enabled')}
        else:
            assert new == old
    assert client.get('/api/agents').json() == agents
    with session() as s:
        assert s.get(ModelRevision, configured['models'][0] + ':1').config['enabled'] is True
        assert s.get(ModelRevision, configured['models'][0] + ':2').config['enabled'] is False
    history = client.get(f'/api/providers/{pid}/models/bulk-state/history')
    assert history.status_code == 200 and len(history.json()['items']) == 1
    assert history.json()['items'][0]['bindings_changed'] is False


def sibling(configured, **updates):
    with session() as s:
        row = s.get(ModelProfile, configured['models'][0])
        other = ModelProfile(id=uid(), version=1, config={**deepcopy(row.config), 'name': 'Second configuration', **updates})
        s.add(other)
        return other.id


def test_bulk_noop_is_audited_without_spurious_model_version(client, configured):
    pid, payload = body(client, configured, enabled=True)
    preview = review(client, pid, payload).json()
    assert preview['counts']['changed'] == 0
    result = apply(client, pid, payload, preview)
    assert result.status_code == 200 and not result.json()['changed']
    assert result.json()['unchanged'][0]['version'] == payload['selections'][0]['version']
    assert len(client.get(f'/api/providers/{pid}/models/bulk-state/history').json()['items']) == 1


@pytest.mark.parametrize('alteration', ['cross_provider', 'missing', 'version', 'provider_revision', 'bindings', 'digest', 'target', 'retired_after_review'])
def test_bulk_conflicts_leave_every_model_and_history_untouched(client, configured, alteration):
    second = sibling(configured, enabled=False)
    pid, payload = body(client, configured, enabled=True, ids=[configured['models'][0], second])
    before_preview = review(client, pid, payload)
    assert before_preview.status_code == 200, before_preview.text
    reviewed = before_preview.json()
    if alteration == 'cross_provider':
        payload['selections'][1]['id'] = configured['models'][1]
    elif alteration == 'missing':
        payload['selections'][1]['id'] = 'a' * 32
    elif alteration == 'version':
        with session() as s:
            s.get(ModelProfile, second).version += 1
    elif alteration == 'provider_revision':
        payload['revision_id'] = 'f' * 32
    elif alteration == 'bindings':
        with session() as s:
            agent = s.get(Agent, configured['agents'][0]['id'])
            agent.config = {**agent.config, 'fallback_model_ids': [second]}
            agent.version += 1
    elif alteration == 'digest':
        reviewed['digest'] = '0' * 64
    elif alteration == 'target':
        payload['enabled'] = False
    else:
        with session() as s:
            s.add(CatalogModelState(id=second, provider_id=pid, retired=True))
    before = client.get('/api/models').json()
    with session() as s:
        versions = [(r.id, r.version) for r in s.scalars(select(ModelRevision))]
        events = list(s.scalars(select(Event.id)))
    response = apply(client, pid, payload, reviewed)
    assert response.status_code == 409, response.text
    assert client.get('/api/models').json() == before
    with session() as s:
        assert [(r.id, r.version) for r in s.scalars(select(ModelRevision))] == versions
        assert list(s.scalars(select(Event.id))) == events


def test_retired_profiles_cannot_be_revived_even_if_upstream_returned(client, configured):
    from forge.provider_catalog import ProviderCatalog
    pid, payload = body(client, configured, enabled=True)
    with session() as s:
        s.add(CatalogModelState(id=configured['models'][0], provider_id=pid, retired=True))
        s.add(ProviderCatalog(id=pid + ':' + payload['revision_id'], provider_id=pid,
            revision_id=payload['revision_id'], entries=[{'id': 'configurable-0', 'available': True}]))
    response = review(client, pid, payload)
    assert response.status_code == 409 and '恢复' in response.text
    payload['enabled'] = False
    assert review(client, pid, payload).status_code == 200


def test_disabled_provider_only_allows_disabling(client, configured):
    pid, payload = body(client, configured, enabled=True)
    with session() as s:
        s.get(Provider, pid).enabled = False
    assert review(client, pid, payload).status_code == 409
    payload['enabled'] = False
    assert apply(client, pid, payload, review(client, pid, payload).json()).status_code == 200


def test_enabling_preserves_unconfirmed_tools_and_all_manual_parameters(client, configured):
    second = sibling(configured, enabled=False, supports_tools=False, parameters={}, allowed_parameters=[])
    pid, payload = body(client, configured, enabled=True, ids=[second])
    preview = review(client, pid, payload).json()
    assert preview['counts']['unconfirmed_tools'] == 1
    result = apply(client, pid, payload, preview)
    assert result.status_code == 200
    value = result.json()['changed'][0]
    assert value['enabled'] and not value['supports_tools'] and value['parameters'] == {}
    assert value['allowed_parameters'] == []


@pytest.mark.parametrize('invalid', ['duplicate', 'empty', 'missing_ack', 'false_ack', 'string_ack', 'string_enabled', 'boolean_version', 'huge'])
def test_bulk_strict_input_and_explicit_confirmation(client, configured, invalid):
    pid, payload = body(client, configured)
    reviewed = review(client, pid, payload).json()
    value = {**payload, 'digest': reviewed['digest'], 'acknowledged': True}
    if invalid == 'duplicate': value['selections'] *= 2
    elif invalid == 'empty': value['selections'] = []
    elif invalid == 'missing_ack': value.pop('acknowledged')
    elif invalid == 'false_ack': value['acknowledged'] = False
    elif invalid == 'string_ack': value['acknowledged'] = 'true'
    elif invalid == 'string_enabled': value['enabled'] = 'false'
    elif invalid == 'boolean_version': value['selections'][0]['version'] = True
    else: value['selections'] = [{'id': format(i, '032x'), 'version': 1} for i in range(2001)]
    before = client.get('/api/models').json()
    response = client.post(f'/api/providers/{pid}/models/bulk-state', json=value)
    assert response.status_code == 422, response.text
    assert client.get('/api/models').json() == before


def test_bulk_history_scopes_provider_and_mutation_requires_admin(client, configured):
    pid, payload = body(client, configured)
    preview = review(client, pid, payload).json()
    assert apply(client, pid, payload, preview).status_code == 200
    assert client.get(f'/api/providers/{configured["providers"][1]}/models/bulk-state/history').json() == {'items': []}
    created = client.post('/api/users', json={'username': 'reader', 'password': 'reader-password-12345', 'role': 'viewer'})
    assert created.status_code == 201
    client.post('/api/auth/logout')
    assert client.post('/api/auth/login', json={'username': 'reader', 'password': 'reader-password-12345'}).status_code == 200
    assert review(client, pid, payload).status_code == 403
    assert apply(client, pid, payload, preview).status_code == 403
    assert client.get(f'/api/providers/{pid}/models/bulk-state/history').status_code == 200


def test_two_confirmations_with_the_same_versions_serialize_without_lost_updates(client, configured, monkeypatch):
    from forge import provider_model_state
    pid, payload = body(client, configured)
    reviewed = review(client, pid, payload).json()
    held, release, second_started = ThreadEvent(), ThreadEvent(), ThreadEvent()
    original = provider_model_state.record_model
    first = True

    def pause_after_lock(s, row, actor):
        nonlocal first
        if first:
            first = False
            held.set()
            assert release.wait(5)
        original(s, row, actor)

    monkeypatch.setattr(provider_model_state, 'record_model', pause_after_lock)
    def duplicate():
        second_started.set()
        return apply(client, pid, payload, reviewed)
    with ThreadPoolExecutor() as pool:
        one = pool.submit(apply, client, pid, payload, reviewed)
        assert held.wait(5)
        two = pool.submit(duplicate)
        assert second_started.wait(5)
        try:
            assert not one.done() and not two.done()
        finally:
            release.set()
        responses = [one.result(5), two.result(5)]
    assert [response.status_code for response in responses] == [200, 409]
    assert len(client.get(f'/api/providers/{pid}/models/bulk-state/history').json()['items']) == 1
    with session() as s:
        assert s.get(ModelProfile, configured['models'][0]).version == payload['selections'][0]['version'] + 1


def test_transaction_failure_rolls_back_already_flushed_models_history_and_audit(client, configured, monkeypatch):
    from forge import provider_model_state
    second = sibling(configured)
    pid, payload = body(client, configured, ids=[configured['models'][0], second])
    reviewed = review(client, pid, payload).json()
    original, calls = provider_model_state.record_model, 0
    def fail_during_second(s, row, actor):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError('test transaction interruption')
        original(s, row, actor)
    monkeypatch.setattr(provider_model_state, 'record_model', fail_during_second)
    before = client.get('/api/models').json()
    with session() as s:
        versions = list(s.scalars(select(ModelRevision.id)))
        events = list(s.scalars(select(Event.id)))
    with pytest.raises(RuntimeError, match='test transaction interruption'):
        apply(client, pid, payload, reviewed)
    assert client.get('/api/models').json() == before
    with session() as s:
        assert list(s.scalars(select(ModelRevision.id))) == versions
        assert list(s.scalars(select(Event.id))) == events


def test_bulk_never_contacts_upstream_or_inference_and_preserves_run_snapshots(client, configured, monkeypatch):
    from forge import providers
    from forge.db import Run
    async def unexpected(*args, **kwargs):
        raise AssertionError('Bulk model state must not contact any model provider')
    monkeypatch.setattr(providers, 'discover', unexpected)
    monkeypatch.setattr(providers, 'complete', unexpected)
    frozen = {'models': deepcopy(client.get('/api/models').json()), 'agents': deepcopy(configured['agents'])}
    with session() as s:
        run_id = uid()
        s.add(Run(id=run_id, project_id=configured['project']['id'], owner_id='test', title='Frozen run', request='test',
                  snapshot=frozen, status='completed'))
    pid, payload = body(client, configured)
    assert apply(client, pid, payload, review(client, pid, payload).json()).status_code == 200
    pid, payload = body(client, configured, enabled=True)
    assert apply(client, pid, payload, review(client, pid, payload).json()).status_code == 200
    with session() as s:
        assert s.get(Run, run_id).snapshot == frozen


def test_bulk_enable_validates_existing_parameters_but_disable_remains_available(client, configured):
    second = sibling(configured, enabled=False, parameters={'undeclared_parameter': 5})
    pid, payload = body(client, configured, enabled=True, ids=[second])
    assert review(client, pid, payload).status_code == 422
    payload['enabled'] = False
    assert review(client, pid, payload).status_code == 200
