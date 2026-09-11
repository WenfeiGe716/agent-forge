import json
import pytest

from forge import providers
from forge.db import session, ModelProfile, Provider, Agent, Run
from forge.model_hub import SUITE


@pytest.fixture(name='configured')
def model_assets_only(configured_unseated):
    # Asset browsing/benchmark tests do not create runs or require an office.
    return configured_unseated


def publish(client, draft):
    return client.post(f'/api/design/assets/{draft["id"]}/publish', json={'version': draft['version'], 'digest': draft['digest'], 'comment': 'Reviewed fixture'})


def capability(client, mid, overrides=None, **extra):
    return client.post('/api/design/assets', json={'kind': 'capability', 'name': 'Capability fixture',
        'config': {'model_profile_id': mid, 'benchmark_id': None, 'system_scores': {},
                   'overrides': overrides or {}, 'reason': 'Internal review evidence', **extra}}).json()


def test_overview_is_readonly_and_unmeasured_is_unknown(client, configured, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('browse triggered paid call')
    monkeypatch.setattr(providers, 'complete', forbidden)
    result = client.get('/api/model-hub')
    assert result.status_code == 200
    assert len(result.json()['models']) == 2
    assert all(m['capability'] is None for m in result.json()['models'])
    assert result.json()['benchmarks'] == []
    assert client.get('/api/design/assets').json() == []


def test_manual_capability_publish_and_latest_revision(client, configured):
    mid = configured['models'][0]
    draft = capability(client, mid, {'coding': 98, 'cost': 70})
    assert client.get('/api/model-hub').json()['models'][0]['capability'] is None
    assert publish(client, draft).status_code == 200
    row = next(m for m in client.get('/api/model-hub').json()['models'] if m['id'] == mid)
    assert row['capability']['final_scores']['coding'] == 98
    assert row['capability']['final_scores']['reasoning'] is None
    edited = client.put('/api/design/assets/' + draft['id'], json={'name': draft['name'], 'expected_version': 1,
        'config': {**draft['config'], 'overrides': {'coding': 10}}}).json()
    row = next(m for m in client.get('/api/model-hub').json()['models'] if m['id'] == mid)
    assert row['capability']['final_scores']['coding'] == 98
    assert publish(client, edited).status_code == 200
    row = next(m for m in client.get('/api/model-hub').json()['models'] if m['id'] == mid)
    assert row['capability']['final_scores']['coding'] == 10


@pytest.mark.parametrize('extra', [{'system_scores': {'coding': 99}}, {'overrides': {'coding': 101}},
    {'overrides': {'coding': True}}, {'reason': None}, {'system_scores': None}, {'unexpected': 'field'}])
def test_capability_rejects_untrusted_scores_and_malformed_config(client, configured, extra):
    draft = capability(client, configured['models'][0], **extra)
    result = publish(client, draft)
    assert result.status_code == 422, result.text


def test_benchmark_requires_explicit_metering_confirmation(client, configured):
    response = client.post(f'/api/model-hub/models/{configured["models"][0]}/benchmark', json={})
    assert response.status_code == 422
    assert client.get('/api/model-hub').json()['benchmarks'] == []


def benchmark_fake():
    answers = [case['expected'] for case in SUITE['cases']]
    async def complete(connection, model, messages, tools, key, timeout):
        if answers:
            return providers.Reply(answers.pop(0), [], 10, 5)
        if tools:
            nonce = messages[0]['content'].split('nonce ')[1].split('.')[0]
            return providers.Reply('', [{'id': 'call_1', 'name': 'forge_benchmark_echo', 'arguments': {'nonce': nonce}}], 12, 6)
        assert messages[1]['calls'][0]['id'] == 'call_1'
        assert messages[2]['call_id'] == 'call_1'
        return providers.Reply('OK', [], 15, 1)
    return complete


def test_real_benchmark_flow_records_measurements_as_unpublished_draft(client, configured, monkeypatch):
    monkeypatch.setattr(providers, 'complete', benchmark_fake())
    mid = configured['models'][0]
    response = client.post(f'/api/model-hub/models/{mid}/benchmark', json={'acknowledge': True})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'completed' and result['requests'] == 6
    assert result['scores']['coding'] == result['scores']['tool'] == result['scores']['reasoning'] == 100
    assert result['scores']['cost'] is None and result['estimated_cost'] is None
    assert result['draft']['status'] == 'draft' and result['draft']['source'] == 'benchmark'
    assert publish(client, result['draft']).status_code == 200
    updated = client.get('/api/models').json()[0]
    updated = next(m for m in client.get('/api/models').json() if m['id'] == mid)
    changed = {k: v for k, v in updated.items() if k not in ('id', 'version')}
    changed['name'] = 'Updated config'
    assert client.put('/api/models/' + mid, json=changed).status_code == 200
    row = next(m for m in client.get('/api/model-hub').json()['models'] if m['id'] == mid)
    assert row['capability']['stale'] is True
    assert result['provider_revision'] not in json.dumps(changed)


def test_failed_benchmark_does_not_produce_publishable_scores(client, configured, monkeypatch):
    async def fail(*args, **kwargs):
        raise providers.ProviderError('模型接口 HTTP 429', True)
    monkeypatch.setattr(providers, 'complete', fail)
    result = client.post(f'/api/model-hub/models/{configured["models"][0]}/benchmark', json={'acknowledge': True}).json()
    assert result['status'] == 'failed' and result['draft'] is None
    assert result['requests'] == 1 and result['successful_requests'] == 0
    assert result['scores']['coding'] is None


def test_recommendation_changes_no_live_agents_or_tasks(client, configured):
    before = client.get('/api/agents').json()
    result = client.post('/api/design/recommend', json={'kind': 'team', 'request': 'Develop an internal shop'}).json()
    assert result['status'] == 'draft' and result['generation']['method'] == 'rules'
    assert result['generation']['requests'] == 0
    assert client.get('/api/agents').json() == before
    assert client.get('/api/runs').json() == []
    assert all(not m['skills'] for m in result['config']['members'])


def test_recommendation_uses_only_published_scores(client, configured):
    first, second = configured['models']
    initial = capability(client, first, {'coding': 5, 'tool': 5, 'reasoning': 5})
    assert publish(client, initial).status_code == 200
    candidate = capability(client, second, {'coding': 100, 'tool': 100, 'reasoning': 100})
    request = {'kind': 'routing', 'request': 'Fix payments'}
    a = client.post('/api/design/recommend', json=request).json()
    assert a['config']['assignments']['developer'] == first
    assert publish(client, candidate).status_code == 200
    b = client.post('/api/design/recommend', json=request).json()
    assert b['config']['assignments']['developer'] == second


def test_ai_recommendation_requires_confirmation_and_validates_output(client, configured, monkeypatch):
    req = {'kind': 'routing', 'request': 'Fix payments', 'model_profile_id': configured['models'][0]}
    assert client.post('/api/design/recommend', json=req).status_code == 422
    async def malicious(*args, **kwargs):
        return providers.Reply('{"assignments":{"developer":"nonexistent"},"task_type":"pay"}', [], 12, 30)
    monkeypatch.setattr(providers, 'complete', malicious)
    response = client.post('/api/design/recommend', json={**req, 'acknowledge': True})
    assert response.status_code == 502, response.text
    assert client.get('/api/design/assets').json() == []


def test_model_history_records_explicit_versions(client, configured):
    mid = configured['models'][0]
    row = next(m for m in client.get('/api/models').json() if m['id'] == mid)
    payload = {k: v for k, v in row.items() if k not in ('id', 'version')}
    payload['name'] = 'Human rename'
    assert client.put('/api/models/' + mid, json=payload).status_code == 200
    history = client.get(f'/api/model-hub/models/{mid}/history').json()['versions']
    assert [v['version'] for v in history] == [1, 2]
    assert history[0]['config']['name'] != history[1]['config']['name']


@pytest.mark.parametrize('kind', ['routing', 'team'])
@pytest.mark.parametrize('format', ['tool', 'json', 'fenced'])
def test_ai_proposals_accept_structured_tool_and_complete_json_only(client, configured, monkeypatch, kind, format):
    async def respond(connection, model, messages, definitions, key, timeout):
        assert [d['name'] for d in definitions] == ['propose_design']
        config = json.loads(messages[-1]['content'])['editable_template']
        raw = json.dumps(config)
        if format == 'tool':
            return providers.Reply('', [{'id': 'proposal', 'name': 'propose_design', 'arguments': config}], 10, 20)
        return providers.Reply(('```json\n' + raw + '\n```') if format == 'fenced' else raw, [], 10, 20)
    monkeypatch.setattr(providers, 'complete', respond)
    before = client.get('/api/agents').json()
    result = client.post('/api/design/recommend', json={'kind': kind, 'request': 'Design a shop team',
        'model_profile_id': configured['models'][0], 'acknowledge': True})
    assert result.status_code == 201, result.text
    assert result.json()['status'] == 'draft' and result.json()['generation']['method'] == 'ai'
    assert client.get('/api/agents').json() == before and client.get('/api/runs').json() == []


@pytest.mark.parametrize('text,calls', [
    ('prefix {"members":[]} suffix', []), ('```json\n{}\n```\nextra', []),
    ('{}{}', []), ('', [{'name': 'run_command', 'arguments': {'command': 'echo unsafe'}}]),
    ('', [{'name': 'propose_design', 'arguments': {}}] * 2)])
def test_invalid_proposal_never_creates_assets_or_executes_tools(client, configured, monkeypatch, text, calls):
    async def respond(*args):
        return providers.Reply(text, calls, 10, 20)
    monkeypatch.setattr(providers, 'complete', respond)
    response = client.post('/api/design/recommend', json={'kind': 'team', 'request': 'Shop team design',
        'model_profile_id': configured['models'][0], 'acknowledge': True})
    assert response.status_code == 502
    assert client.get('/api/design/assets').json() == []
    assert client.get('/api/runs').json() == []
