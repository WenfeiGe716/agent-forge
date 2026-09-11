"""Provider directory HTTP contract: discovery is inventory, never activation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from forge import providers


def test_catalog_persists_full_inventory_without_creating_runtime_configuration(client, configured, monkeypatch):
    pid = configured['providers'][0]
    before_models = client.get('/api/models').json()
    before_agents = client.get('/api/agents').json()

    async def upstream(*args, **kwargs):
        return [{'id': 'configurable-0', 'name': 'Configured'},
                {'id': 'image-id', 'name': 'Image', 'capabilities': ['image']},
                {'id': 'embedding-id', 'name': 'Embedding', 'capabilities': ['embedding']}]

    monkeypatch.setattr(providers, 'discover', upstream)
    response = client.post(f'/api/providers/{pid}/catalog/sync')
    assert response.status_code == 200, response.text
    stored = client.get(f'/api/providers/{pid}/catalog').json()
    assert stored['counts']['available'] == 3
    assert stored['counts']['unconfigured'] == 2
    assert stored['last_synced_at']
    assert [m['id'] for m in stored['models']] == ['configurable-0', 'embedding-id', 'image-id']
    assert stored['models'][1]['capabilities'] == ['embedding']
    assert client.get('/api/models').json() == before_models
    assert client.get('/api/agents').json() == before_agents


def test_failed_sync_keeps_last_success_and_reports_error_without_leaking_credentials(client, configured, monkeypatch):
    pid = configured['providers'][0]
    async def good(*args, **kwargs):
        return [{'id': 'last-good', 'name': 'Last success'}]
    monkeypatch.setattr(providers, 'discover', good)
    good_result = client.post(f'/api/providers/{pid}/catalog/sync').json()
    async def failed(*args, **kwargs):
        raise providers.ProviderError('模型目录接口 HTTP 429')
    monkeypatch.setattr(providers, 'discover', failed)
    result = client.post(f'/api/providers/{pid}/catalog/sync')
    assert result.status_code == 502
    cached = client.get(f'/api/providers/{pid}/catalog').json()
    assert cached['error'] == '模型目录接口 HTTP 429'
    assert cached['sync_status'] == 'error'
    assert cached['last_synced_at'] == good_result['last_synced_at']
    assert cached['models'] == good_result['models']
    assert 'secret-key' not in result.text + str(cached)


def test_catalog_marks_upstream_removals_and_reappearances_without_disabling_models(client, configured, monkeypatch):
    pid = configured['providers'][0]
    inventory = [{'id': 'configurable-0', 'name': 'Saved'}, {'id': 'temporary', 'name': 'Temporary'}]
    async def upstream(*args, **kwargs):
        return inventory
    monkeypatch.setattr(providers, 'discover', upstream)
    first = client.post(f'/api/providers/{pid}/catalog/sync').json()
    inventory.clear()
    removed = client.post(f'/api/providers/{pid}/catalog/sync').json()
    assert removed['counts']['available'] == 0
    assert removed['counts']['missing'] == 2
    assert all(row['removed_at'] for row in removed['entries'])
    assert client.get('/api/models').json()[0]['enabled']
    inventory.append({'id': 'temporary', 'name': 'Returned'})
    restored = client.post(f'/api/providers/{pid}/catalog/sync').json()['models'][0]
    assert restored['first_seen_at'] == next(row for row in first['models'] if row['id'] == 'temporary')['first_seen_at']
    assert restored['removed_at'] == ''
    assert restored['label'] == 'Returned'


def test_older_concurrent_sync_cannot_overwrite_newer_directory(client, configured, monkeypatch):
    pid = configured['providers'][0]
    started, release = Event(), Event()
    request_number = 0
    async def upstream(*args, **kwargs):
        nonlocal request_number
        request_number += 1
        if request_number == 1:
            started.set()
            await asyncio.to_thread(release.wait, 5)
            return [{'id': 'old', 'name': 'Old response'}]
        return [{'id': 'latest', 'name': 'Latest response'}]
    monkeypatch.setattr(providers, 'discover', upstream)
    with ThreadPoolExecutor() as pool:
        old = pool.submit(client.post, f'/api/providers/{pid}/catalog/sync')
        assert started.wait(5)
        latest = client.post(f'/api/providers/{pid}/catalog/sync')
        release.set()
        stale = old.result(5)
    assert latest.status_code == 200
    assert stale.status_code == 409
    assert [r['id'] for r in client.get(f'/api/providers/{pid}/catalog').json()['models']] == ['latest']


def test_connection_revision_change_isolates_cached_credentials_and_rejects_inflight_response(client, configured, monkeypatch):
    pid = configured['providers'][0]
    started, release = Event(), Event()
    async def old_upstream(*args, **kwargs):
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return [{'id': 'old-account-private-model', 'name': 'Old account model'}]
    monkeypatch.setattr(providers, 'discover', old_upstream)
    with ThreadPoolExecutor() as pool:
        old = pool.submit(client.post, f'/api/providers/{pid}/catalog/sync')
        assert started.wait(5)
        saved = next(p for p in client.get('/api/providers').json() if p['id'] == pid)
        changed = client.put('/api/providers/'+pid, json={k: v for k, v in saved.items() if k not in ('id', 'revision_id', 'has_key')} | {'api_key': 'new-test-only-key'})
        assert changed.status_code == 200, changed.text
        fresh = client.get(f'/api/providers/{pid}/catalog').json()
        assert fresh['revision_id'] != saved['revision_id']
        assert fresh['models'] == []
        assert fresh['last_synced_at'] == ''
        release.set()
        assert old.result(5).status_code == 409
    assert client.get(f'/api/providers/{pid}/catalog').json()['models'] == []


def test_legacy_discover_persists_same_provider_owned_catalog(client, configured, monkeypatch):
    pid = configured['providers'][0]
    async def upstream(*args, **kwargs):
        return [{'id': 'legacy-visible', 'name': 'Persist me'}]
    monkeypatch.setattr(providers, 'discover', upstream)
    response = client.post(f'/api/model-hub/providers/{pid}/discover')
    assert response.status_code == 200
    assert response.json()['installed'] is False
    assert client.get(f'/api/providers/{pid}/catalog').json()['counts']['available'] == 1


def test_connections_with_same_model_id_keep_separate_inventories(client, configured, monkeypatch):
    a, b = configured['providers']
    async def upstream(connection, key):
        label = 'account-a' if connection['base_url'].startswith('http://model-0') else 'account-b'
        return [{'id':'shared-id','name':label}, {'id':label+'-only','name':label}]
    monkeypatch.setattr(providers, 'discover', upstream)
    for pid in (a,b):
        assert client.post(f'/api/providers/{pid}/catalog/sync').status_code == 200
    aa = client.get(f'/api/providers/{a}/catalog').json()
    bb = client.get(f'/api/providers/{b}/catalog').json()
    assert {r['id'] for r in aa['models']} == {'shared-id','account-a-only'}
    assert {r['id'] for r in bb['models']} == {'shared-id','account-b-only'}
    assert next(r for r in aa['models'] if r['id']=='shared-id')['label'] == 'account-a'
    assert next(r for r in bb['models'] if r['id']=='shared-id')['label'] == 'account-b'


def test_directory_is_readable_but_sync_requires_admin(client, configured, monkeypatch):
    from forge.db import User, session, uid
    from forge.security import password_hash
    pid = configured['providers'][0]
    with session() as s:
        s.add(User(id=uid(),username='catalog-viewer',password_hash=password_hash('local-fixture-viewer'),role='viewer'))
    client.post('/api/auth/logout')
    assert client.get(f'/api/providers/{pid}/catalog').status_code == 401
    client.post('/api/auth/login',json={'username':'catalog-viewer','password':'local-fixture-viewer'})
    assert client.get(f'/api/providers/{pid}/catalog').status_code == 200
    assert client.post(f'/api/providers/{pid}/catalog/sync').status_code == 403
    assert client.post(f'/api/model-hub/providers/{pid}/discover').status_code == 403
