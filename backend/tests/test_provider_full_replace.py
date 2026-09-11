"""Replace one provider's current model set without crossing account boundaries."""
from copy import deepcopy

from test_upstream_model_pull import directory


def confirmation(catalog):
    return {key:catalog[key] for key in ('revision_id','catalog_digest')} | {'acknowledged':True}


def test_full_replace_adds_all_retires_missing_and_leaves_other_provider_and_agents_untouched(client, configured, monkeypatch):
    before_models = client.get('/api/models').json()
    before_agents = client.get('/api/agents').json()
    pid, catalog, rows = directory(client, configured, monkeypatch)
    response = client.post(f'/api/providers/{pid}/catalog/replace',json=confirmation(catalog))
    assert response.status_code == 201, response.text
    result = response.json()
    assert {m['model_id'] for m in result['created']} == {'new-coder','new-embedding'}
    assert [m['model_id'] for m in result['retired']] == ['configurable-0']
    assert all(m['enabled'] is False for m in result['created']+result['retired'])
    assert result['catalog']['counts']['configured'] == 2
    assert result['catalog']['counts']['retired_configured'] == 1
    assert result['catalog']['retired_profile_ids'] == [configured['models'][0]]
    assert client.get('/api/agents').json() == before_agents
    other = [m for m in client.get('/api/models').json() if m['provider_id'] != pid]
    assert other == [m for m in before_models if m['provider_id'] != pid]
    history = client.get('/api/model-hub/models/'+configured['models'][0]+'/history').json()
    assert any(v['config']['enabled'] is True for v in history['versions'])
    again = client.post(f'/api/providers/{pid}/catalog/replace',json=confirmation(catalog)).json()
    assert again['created'] == [] and again['retired'] == []


def test_full_replace_preserves_human_settings_on_matching_models_and_restores_without_enabling(client, configured, monkeypatch):
    old = next(m for m in client.get('/api/models').json() if m['id']==configured['models'][0])
    pid, first, rows = directory(client, configured, monkeypatch,[{'id':old['model_id'],'name':'Vendor rename'}, {'id':'extra','name':'Extra'}])
    response = client.post(f'/api/providers/{pid}/catalog/replace',json=confirmation(first))
    assert response.status_code == 201, response.text
    assert response.json()['kept'] == [old]
    assert next(m for m in client.get('/api/models').json() if m['id']==old['id']) == old
    rows[:] = [{'id':'extra','name':'Extra'}]
    second = client.post(f'/api/providers/{pid}/catalog/sync').json()
    assert client.post(f'/api/providers/{pid}/catalog/replace',json=confirmation(second)).status_code == 201
    rows.append({'id':old['model_id'],'name':'Back'})
    third = client.post(f'/api/providers/{pid}/catalog/sync').json()
    restored = client.post(f'/api/providers/{pid}/catalog/replace',json=confirmation(third)).json()
    assert [m['id'] for m in restored['restored']] == [old['id']]
    assert restored['restored'][0]['enabled'] is False
    assert restored['restored'][0]['parameters'] == old['parameters']
    assert restored['catalog']['retired_profile_ids'] == []


def test_empty_or_failed_upstream_cannot_replace_with_empty_model_set(client, configured, monkeypatch):
    from forge import providers
    pid, good, rows = directory(client, configured, monkeypatch)
    before = client.get('/api/models').json()
    async def failed(*args, **kwargs):
        raise providers.ProviderError('HTTP 503')
    monkeypatch.setattr(providers,'discover',failed)
    assert client.post(f'/api/providers/{pid}/catalog/sync').status_code == 502
    assert client.post(f'/api/providers/{pid}/catalog/replace',json=confirmation(good)).status_code == 409
    pid, empty, _ = directory(client, configured, monkeypatch,[])
    assert client.post(f'/api/providers/{pid}/catalog/replace',json=confirmation(empty)).status_code == 422
    assert client.get('/api/models').json() == before


def test_replace_requires_review_and_exact_provider_revision(client, configured, monkeypatch):
    pid, catalog, _ = directory(client, configured, monkeypatch)
    before = client.get('/api/models').json()
    for patch in ({'acknowledged':False},{'catalog_digest':'0'*64},{'revision_id':'0'*32}):
        response = client.post(f'/api/providers/{pid}/catalog/replace',json=confirmation(catalog)|patch)
        assert response.status_code in (409,422)
    assert client.get('/api/models').json() == before


def test_retired_profile_cannot_be_enabled_without_restoring_to_current_list(client, configured, monkeypatch):
    pid, catalog, _ = directory(client, configured, monkeypatch)
    result = client.post(f'/api/providers/{pid}/catalog/replace',json=confirmation(catalog)).json()
    retired = result['retired'][0]
    body = {key:value for key,value in retired.items() if key not in ('id','version')}
    assert client.put('/api/models/'+retired['id'],json=body | {'enabled':True}).status_code == 409
    assert client.put('/api/models/'+retired['id'],json=body | {'name':'Editable history'}).status_code == 200
    current = client.get(f'/api/providers/{pid}/catalog').json()
    assert current['retired_profile_ids'] == [retired['id']]
    assert next(m for m in client.get('/api/models').json() if m['id']==retired['id'])['enabled'] is False
