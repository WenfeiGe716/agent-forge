"""A pull refreshes inventory; only explicit selection imports inactive configs."""
import pytest
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy import select

from forge import providers
from forge.db import ModelProfile, session


def directory(client, configured, monkeypatch, rows=None):
    pid = configured['providers'][0]
    inventory = rows if rows is not None else [{'id':'new-coder','name':'New coder'}, {'id':'new-embedding','name':'New embedding'}]
    async def upstream(*args, **kwargs):
        return inventory
    monkeypatch.setattr(providers,'discover',upstream)
    response = client.post(f'/api/providers/{pid}/catalog/sync')
    assert response.status_code == 200, response.text
    return pid, response.json(), inventory


def selection(catalog, ids):
    return {'revision_id':catalog['revision_id'],'catalog_digest':catalog['catalog_digest'],
            'model_ids':ids,'acknowledged':True}


def test_pull_always_fetches_upstream_and_persists_exact_change_report(client, configured, monkeypatch):
    calls = []
    inventory = [{'id':'keep','name':'Same'}, {'id':'change','name':'Before'}, {'id':'remove','name':'Gone'}]
    async def upstream(*args, **kwargs):
        calls.append(True)
        return inventory
    monkeypatch.setattr(providers,'discover',upstream)
    pid = configured['providers'][0]
    first = client.post(f'/api/providers/{pid}/catalog/sync').json()
    assert first['last_pull']['counts']['added'] == 3
    assert first['last_pull']['request_url'] == 'http://model-0.internal/v1/models'
    inventory[:] = [{'id':'keep','name':'Same'}, {'id':'change','name':'After'}, {'id':'add','name':'Added'}]
    second = client.post(f'/api/providers/{pid}/catalog/sync').json()
    assert len(calls) == 2  # Even when the one-hour cache is still fresh.
    assert second['last_pull']['changes'] == {'added':['add'],'updated':['change'],
        'removed':['remove'],'restored':[],'unchanged':['keep']}
    assert second['last_pull']['received_count'] == 3
    assert second['catalog_digest'] != first['catalog_digest']
    assert client.get(f'/api/providers/{pid}/catalog').json()['last_pull'] == second['last_pull']
    inventory.append({'id':'remove','name':'Returned'})
    third = client.post(f'/api/providers/{pid}/catalog/sync').json()
    assert third['last_pull']['changes']['restored'] == ['remove']
    assert third['last_pull']['changes']['removed'] == []


def test_explicit_import_creates_disabled_unbound_models_and_preserves_existing(client, configured, monkeypatch):
    before_models = client.get('/api/models').json()
    before_agents = client.get('/api/agents').json()
    pid, catalog, _ = directory(client, configured, monkeypatch)
    assert client.get('/api/models').json() == before_models
    response = client.post(f'/api/providers/{pid}/catalog/import',json=selection(catalog,['new-coder','new-embedding']))
    assert response.status_code == 201, response.text
    imported = response.json()
    assert len(imported['created']) == 2
    for model in imported['created']:
        assert model['provider_id'] == pid
        assert model['enabled'] is False and model['supports_tools'] is False
        assert model['parameters'] == {} and model['allowed_parameters'] == []
        assert model['input_cost_per_million'] is None and model['output_cost_per_million'] is None
        assert client.get('/api/model-hub/models/' + model['id'] + '/history').status_code == 200
    assert client.get('/api/agents').json() == before_agents
    after = client.get('/api/models').json()
    assert [m for m in after if m['id'] in {x['id'] for x in before_models}] == before_models
    repeated = client.post(f'/api/providers/{pid}/catalog/import',json=selection(catalog,['new-coder','new-embedding']))
    assert repeated.status_code == 201
    assert repeated.json()['created'] == []
    assert repeated.json()['skipped'] == ['new-coder','new-embedding']
    assert len(client.get('/api/models').json()) == len(before_models)+2


@pytest.mark.parametrize('change', ['digest','revision','unknown','duplicate','acknowledgement'])
def test_import_requires_exact_reviewed_catalog_and_valid_whole_selection(client, configured, monkeypatch, change):
    pid, catalog, _ = directory(client, configured, monkeypatch)
    before = client.get('/api/models').json()
    body = selection(catalog,['new-coder'])
    if change == 'digest': body['catalog_digest'] = '0'*64
    if change == 'revision': body['revision_id'] = '0'*32
    if change == 'unknown': body['model_ids'].append('not-returned')
    if change == 'duplicate': body['model_ids'].append('new-coder')
    if change == 'acknowledgement': body['acknowledged'] = False
    response = client.post(f'/api/providers/{pid}/catalog/import',json=body)
    assert response.status_code in (409,422), response.text
    assert client.get('/api/models').json() == before


def test_import_rejects_changed_catalog_and_removed_models(client, configured, monkeypatch):
    pid, first, rows = directory(client, configured, monkeypatch)
    before = client.get('/api/models').json()
    rows[:] = [{'id':'replacement','name':'Replacement'}]
    second = client.post(f'/api/providers/{pid}/catalog/sync').json()
    assert client.post(f'/api/providers/{pid}/catalog/import',json=selection(first,['new-coder'])).status_code == 409
    assert client.post(f'/api/providers/{pid}/catalog/import',json=selection(second,['new-coder'])).status_code == 422
    assert client.get('/api/models').json() == before


def test_failed_pull_keeps_last_success_report_and_digest(client, configured, monkeypatch):
    pid, first, _ = directory(client, configured, monkeypatch)
    async def failed(*args, **kwargs):
        raise providers.ProviderError('模型目录接口 HTTP 503')
    monkeypatch.setattr(providers,'discover',failed)
    assert client.post(f'/api/providers/{pid}/catalog/sync').status_code == 502
    cached = client.get(f'/api/providers/{pid}/catalog').json()
    assert cached['last_pull'] == first['last_pull']
    assert cached['catalog_digest'] == first['catalog_digest']
    assert cached['error'] and cached['sync_status'] == 'error'


def test_identical_catalog_has_stable_digest_and_successful_empty_directory_is_explicit(client, configured, monkeypatch):
    pid, first, rows = directory(client, configured, monkeypatch)
    repeat = client.post(f'/api/providers/{pid}/catalog/sync').json()
    assert repeat['catalog_digest'] == first['catalog_digest']
    assert repeat['last_pull']['counts']['unchanged'] == 2
    assert not repeat['last_pull']['changes']['updated']
    rows.clear()
    empty = client.post(f'/api/providers/{pid}/catalog/sync').json()
    assert empty['last_pull']['received_count'] == 0
    assert empty['last_pull']['counts']['removed'] == 2
    assert empty['catalog_digest'] != first['catalog_digest']


def test_concurrent_imports_do_not_duplicate_profiles(client, configured, monkeypatch):
    pid, catalog, _ = directory(client, configured, monkeypatch)
    body = selection(catalog,['new-coder'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [pool.submit(client.post,f'/api/providers/{pid}/catalog/import',json=body) for _ in range(2)]
        results = [call.result(10) for call in calls]
    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    assert sum(len(r.json()['created']) for r in results) == 1
    with session() as s:
        assert sum(m.config['provider_id'] == pid and m.config['model_id'] == 'new-coder' for m in s.scalars(select(ModelProfile))) == 1


def test_import_is_admin_only_and_failed_attempt_does_not_change_catalog(client, configured, monkeypatch):
    from forge.db import User, uid
    from forge.security import password_hash
    pid, catalog, _ = directory(client, configured, monkeypatch)
    before = client.get('/api/models').json()
    with session() as s:
        s.add(User(id=uid(),username='import-viewer',password_hash=password_hash('read-only-fixture-123'),role='viewer'))
    client.post('/api/auth/logout')
    assert client.post(f'/api/providers/{pid}/catalog/import',json=selection(catalog,['new-coder'])).status_code == 401
    client.post('/api/auth/login',json={'username':'import-viewer','password':'read-only-fixture-123'})
    assert client.post(f'/api/providers/{pid}/catalog/import',json=selection(catalog,['new-coder'])).status_code == 403
    assert client.get('/api/models').json() == before
