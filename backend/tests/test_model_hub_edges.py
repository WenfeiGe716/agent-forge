"""Regression tests for measured evidence and paid-call lifecycle boundaries."""
import json

import pytest

from forge import providers
from forge.db import ModelProfile, Provider, ProviderRevision, session
from forge.model_hub import SUITE, matches_case


@pytest.fixture(name='configured')
def model_assets_only(configured_unseated):
    # Recommendations remain design drafts, independent of assigned desks.
    return configured_unseated


def approve(client, draft):
    return client.post(f'/api/design/assets/{draft["id"]}/publish', json={
        'version': draft['version'], 'digest': draft['digest'], 'comment': 'Reviewed test evidence'})


def answering_model(answers=None, callback=None):
    requests = []
    expected = answers or {case['prompt']: case['expected'] for case in SUITE['cases']}

    async def complete(connection, model, messages, tools, key, timeout):
        requests.append({'connection': connection.copy(), 'model': model.copy(), 'messages': messages})
        if callback:
            callback(len(requests))
        if tools:
            nonce = messages[0]['content'].split('nonce ')[1].split('.')[0]
            return providers.Reply('', [{'id': 'echo_call', 'name': 'forge_benchmark_echo', 'arguments': {'nonce': nonce}}], 3, 1)
        if messages[0]['content'] in expected:
            return providers.Reply(expected[messages[0]['content']], [], 3, 1)
        return providers.Reply('OK', [], 3, 1)

    return complete, requests


def test_published_custom_suite_defaults_are_used_without_losing_evidence(client, configured, monkeypatch):
    draft = client.post('/api/design/assets', json={'kind': 'benchmark', 'name': 'Default matcher suite',
        'config': {'cases': [{'id': 'boundary', 'dimension': 'coding', 'prompt': 'Return B', 'expected': 'B'}]}}).json()
    assert approve(client, draft).status_code == 200
    complete, calls = answering_model({'Return B': 'B'})
    monkeypatch.setattr(providers, 'complete', complete)
    response = client.post(f'/api/model-hub/models/{configured["models"][0]}/benchmark', json={
        'acknowledge': True, 'suite_ref': {'id': draft['id'], 'version': 1}})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'completed', result
    assert result['requests'] == len(calls) == 3
    assert result['scores']['coding'] == result['scores']['tool'] == 100
    assert result['suite']['cases'][0]['match'] == 'exact'
    assert result['draft']['status'] == 'draft'


def test_maximum_model_name_still_records_completed_benchmark_and_draft(client, configured, monkeypatch):
    mid = configured['models'][0]
    model = next(item for item in client.get('/api/models').json() if item['id'] == mid)
    model = {key: value for key, value in model.items() if key not in ('id', 'version')}
    model['name'] = '长' * 120
    assert client.put(f'/api/models/{mid}', json=model).status_code == 200
    complete, calls = answering_model()
    monkeypatch.setattr(providers, 'complete', complete)
    response = client.post(f'/api/model-hub/models/{mid}/benchmark', json={'acknowledge': True})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'completed' and len(result['draft']['name']) <= 120
    assert result['requests'] == len(calls) == 6
    assert client.get('/api/model-hub').json()['benchmarks'][0]['status'] == 'completed'


@pytest.mark.parametrize('actual,expected,valid', [
    ('true', '1', False), ('false', '0', False), ('{"x":[true]}', '{"x":[1]}', False),
    ('NaN', 'NaN', False), ('Infinity', 'Infinity', False), ('1', '1.0', True),
    ('{"a":1,"b":2}', '{"b":2,"a":1}', True),
])
def test_json_benchmarks_distinguish_booleans_and_reject_nonfinite_numbers(actual, expected, valid):
    case = {'match': 'json', 'expected': expected}
    assert matches_case(case, providers.Reply(actual, [], 1, 1)) is valid


def test_model_disabling_interrupts_subsequent_benchmark_calls(client, configured, monkeypatch):
    mid = configured['models'][0]

    def disable_model(number):
        if number == 1:
            with session() as s:
                model = s.get(ModelProfile, mid)
                model.config = {**model.config, 'enabled': False}

    complete, calls = answering_model(callback=disable_model)
    monkeypatch.setattr(providers, 'complete', complete)
    result = client.post(f'/api/model-hub/models/{mid}/benchmark', json={'acknowledge': True}).json()
    assert result['status'] == 'failed' and result['draft'] is None
    assert result['requests'] == len(calls) == 1
    assert '停用' in result['error']


def test_model_revision_edits_do_not_hot_reload_a_running_benchmark(client, configured, monkeypatch):
    mid = configured['models'][0]
    original = next(item for item in client.get('/api/models').json() if item['id'] == mid)

    def edit_model(number):
        if number == 1:
            with session() as s:
                model = s.get(ModelProfile, mid)
                model.config = {**model.config, 'model_id': 'new-serving-model'}
                model.version += 1

    complete, calls = answering_model(callback=edit_model)
    monkeypatch.setattr(providers, 'complete', complete)
    result = client.post(f'/api/model-hub/models/{mid}/benchmark', json={'acknowledge': True}).json()
    assert result['status'] == 'completed' and result['model_version'] == original['version']
    assert all(call['model']['model_id'] == original['model_id'] for call in calls)
    assert approve(client, result['draft']).status_code == 422


def test_non_admins_cannot_trigger_paid_benchmarks_or_ai_recommendations(client, configured, monkeypatch):
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('Unauthorised paid model call')

    monkeypatch.setattr(providers, 'complete', forbidden)
    assert client.post('/api/users', json={'username': 'hub-reader', 'password': 'reader-password-12345', 'role': 'viewer'}).status_code == 201
    client.post('/api/auth/logout')
    assert client.post('/api/auth/login', json={'username': 'hub-reader', 'password': 'reader-password-12345'}).status_code == 200
    assert client.get('/api/model-hub').status_code == 200
    assert client.post(f'/api/model-hub/models/{configured["models"][0]}/benchmark', json={'acknowledge': True}).status_code == 403
    assert client.post('/api/design/recommend', json={'kind': 'team', 'request': 'Make a shop',
        'model_profile_id': configured['models'][0], 'acknowledge': True}).status_code == 403
    assert client.post(f'/api/model-hub/providers/{configured["providers"][0]}/discover').status_code == 403
    assert not calls and client.get('/api/design/assets').json() == []


def test_paid_benchmark_retry_reuses_original_record_and_draft(client, configured, monkeypatch):
    complete, calls = answering_model()
    monkeypatch.setattr(providers, 'complete', complete)
    headers = {'Idempotency-Key': 'one-logical-benchmark'}
    url = f'/api/model-hub/models/{configured["models"][0]}/benchmark'
    one = client.post(url, json={'acknowledge': True}, headers=headers)
    two = client.post(url, json={'acknowledge': True}, headers=headers)
    assert one.status_code == two.status_code == 200
    assert one.json() == two.json() and len(calls) == 6
    assert len(client.get('/api/model-hub').json()['benchmarks']) == 1
    assert len(client.get('/api/design/assets?kind=capability').json()) == 1
    conflict = client.post(f'/api/model-hub/models/{configured["models"][1]}/benchmark', json={'acknowledge': True}, headers=headers)
    assert conflict.status_code == 409 and len(calls) == 6


def test_failed_paid_recommendation_does_not_retry_model_on_same_key(client, configured, monkeypatch):
    calls = []

    async def unavailable(*args, **kwargs):
        calls.append(True)
        raise providers.ProviderError('模型接口 HTTP 503', True)

    monkeypatch.setattr(providers, 'complete', unavailable)
    request = {'kind': 'team', 'request': 'Design a payment team', 'acknowledge': True,
               'model_profile_id': configured['models'][0]}
    headers = {'Idempotency-Key': 'failed-ai-design'}
    first = client.post('/api/design/recommend', json=request, headers=headers)
    second = client.post('/api/design/recommend', json=request, headers=headers)
    assert first.status_code == second.status_code == 502
    assert first.json() == second.json() and len(calls) == 1
    assert client.get('/api/design/assets').json() == []
    assert client.post('/api/design/recommend', json={**request, 'request': 'Changed request'}, headers=headers).status_code == 409
    assert len(calls) == 1


def test_successful_ai_recommendation_is_replayed_without_another_draft(client, configured, monkeypatch):
    calls = []

    async def design(connection, model, messages, definitions, key, timeout):
        calls.append(True)
        template = json.loads(messages[-1]['content'])['editable_template']
        return providers.Reply(json.dumps(template), [], 10, 10)

    monkeypatch.setattr(providers, 'complete', design)
    request = {'kind': 'routing', 'request': 'Design a payment routing policy', 'acknowledge': True,
               'model_profile_id': configured['models'][0]}
    headers = {'Idempotency-Key': 'confirmed-ai-design'}
    first = client.post('/api/design/recommend', json=request, headers=headers)
    second = client.post('/api/design/recommend', json=request, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json() and len(calls) == 1
    assert len(client.get('/api/design/assets').json()) == 1
    assert first.json()['source'] == 'ai' and first.json()['status'] == 'draft'


@pytest.mark.asyncio
async def test_inflight_and_cancelled_invocations_never_duplicate_paid_work(client):
    import asyncio
    from fastapi import HTTPException
    from forge.model_hub import invoke_once
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def paid_work():
        calls.append(True)
        started.set()
        await release.wait()
        return {'result': 'done'}

    first = asyncio.create_task(invoke_once('recommend', 'actor', {'request': 'design'}, 'active-call', 201, paid_work))
    await asyncio.wait_for(started.wait(), timeout=2)
    with pytest.raises(HTTPException) as exc:
        await invoke_once('recommend', 'actor', {'request': 'design'}, 'active-call', 201, paid_work)
    assert exc.value.status_code == 409 and len(calls) == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    response = await invoke_once('recommend', 'actor', {'request': 'design'}, 'active-call', 201, paid_work)
    assert response.status_code == 409 and len(calls) == 1
    assert '中断' in json.loads(response.body)['detail']


@pytest.mark.asyncio
async def test_startup_interruption_never_replays_uncertain_work(client):
    from forge.model_hub import ModelHubInvocation, interrupt_incomplete_invocations, invoke_once
    import hashlib
    ident = hashlib.sha256(json.dumps(['actor', 'benchmark', 'restart-call'], separators=(',', ':')).encode()).hexdigest()
    digest = hashlib.sha256(json.dumps({'model': 'test'}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with session() as s:
        s.add(ModelHubInvocation(id=ident, actor='actor', operation='benchmark', request_digest=digest))
    with session() as s:
        assert interrupt_incomplete_invocations(s) == 1

    async def forbidden():
        raise AssertionError('Restart replayed uncertain paid work')

    response = await invoke_once('benchmark', 'actor', {'model': 'test'}, 'restart-call', 200, forbidden)
    assert response.status_code == 409 and '重启' in json.loads(response.body)['detail']


@pytest.mark.parametrize('expected', ['NaN', 'Infinity', '[1e999]', '{"invalid":-Infinity}'])
def test_nonfinite_expected_json_cannot_be_published(client, expected):
    draft = client.post('/api/design/assets', json={'kind': 'benchmark', 'name': 'Invalid JSON evidence',
        'config': {'cases': [{'id': 'invalid', 'dimension': 'reasoning', 'prompt': 'Return JSON',
                              'expected': expected, 'match': 'json'}]}}).json()
    assert approve(client, draft).status_code == 422


@pytest.mark.asyncio
async def test_cancelled_benchmark_keeps_interrupted_evidence_instead_of_running_forever(client, configured, monkeypatch):
    import asyncio
    from sqlalchemy import select
    from forge.model_hub import BenchmarkInput, BenchmarkRecord, run_benchmark
    from forge.design_center import DesignAsset
    started = asyncio.Event()

    async def pending(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(providers, 'complete', pending)
    work = asyncio.create_task(run_benchmark(configured['models'][0], BenchmarkInput(acknowledge=True), 'admin-actor'))
    await asyncio.wait_for(started.wait(), timeout=2)
    work.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work
    with session() as s:
        row = s.scalar(select(BenchmarkRecord))
        assert row.status == 'interrupted'
        assert row.result['requests'] == 1 and row.result['successful_requests'] == 0
        assert row.result['finished_at'] and row.result['error']
        assert not s.scalar(select(DesignAsset))


@pytest.mark.asyncio
async def test_bootstrap_recovers_only_abandoned_invocations_and_never_replays(client, monkeypatch):
    import hashlib
    from sqlalchemy import select
    from forge.api import bootstrap
    from forge.db import Event
    from forge.model_hub import ModelHubInvocation, invoke_once
    monkeypatch.delattr(bootstrap, '_model_hub_recovered_pid', raising=False)
    ident = hashlib.sha256(json.dumps(['admin-actor', 'benchmark', 'startup-orphan'], separators=(',', ':')).encode()).hexdigest()
    payload = {'model': 'startup-test'}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with session() as s:
        s.add(ModelHubInvocation(id=ident, actor='admin-actor', operation='benchmark', request_digest=digest))
        s.add(ModelHubInvocation(id='already-completed', actor='admin-actor', operation='recommend',
                                request_digest='a' * 64, status='completed', status_code=201,
                                response_body={'draft': 'retained'}, finished_at='earlier'))
        s.add(ModelHubInvocation(id='already-failed', actor='admin-actor', operation='recommend',
                                request_digest='b' * 64, status='failed', status_code=502,
                                response_body={'detail': 'original failure'}, finished_at='earlier'))
    bootstrap()
    with session() as s:
        assert s.get(ModelHubInvocation, ident).status == 'interrupted'
        complete = s.get(ModelHubInvocation, 'already-completed')
        failed = s.get(ModelHubInvocation, 'already-failed')
        assert (complete.status, complete.response_body, complete.finished_at) == ('completed', {'draft': 'retained'}, 'earlier')
        assert (failed.status, failed.response_body, failed.finished_at) == ('failed', {'detail': 'original failure'}, 'earlier')
        s.add(ModelHubInvocation(id='current-process-work', actor='admin-actor', operation='recommend', request_digest='c' * 64))
    bootstrap()
    with session() as s:
        current = s.get(ModelHubInvocation, 'current-process-work')
        assert current.status == 'running' and not current.finished_at
        events = list(s.scalars(select(Event).where(Event.kind == 'model_hub.invocation.interrupted')))
        assert len(events) == 1 and events[0].data['invocation_id'] == ident

    async def forbidden():
        raise AssertionError('Bootstrap recovery replayed a paid call')

    result = await invoke_once('benchmark', 'admin-actor', payload, 'startup-orphan', 200, forbidden)
    assert result.status_code == 409 and '重启' in json.loads(result.body)['detail']


def test_bootstrap_does_not_mark_recovery_complete_if_transaction_fails(client, monkeypatch):
    from forge import model_hub
    from forge.api import bootstrap
    from forge.model_hub import ModelHubInvocation
    monkeypatch.delattr(bootstrap, '_model_hub_recovered_pid', raising=False)
    with session() as s:
        s.add(ModelHubInvocation(id='pending-recovery', actor='actor', operation='benchmark', request_digest='a' * 64))
    recover = model_hub.interrupt_incomplete_invocations

    def fail_after_recovery(s):
        recover(s)
        raise RuntimeError('simulated startup failure')

    monkeypatch.setattr(model_hub, 'interrupt_incomplete_invocations', fail_after_recovery)
    with pytest.raises(RuntimeError, match='startup failure'):
        bootstrap()
    assert not hasattr(bootstrap, '_model_hub_recovered_pid')
    with session() as s:
        assert s.get(ModelHubInvocation, 'pending-recovery').status == 'running'
    monkeypatch.setattr(model_hub, 'interrupt_incomplete_invocations', recover)
    bootstrap()
    with session() as s:
        assert s.get(ModelHubInvocation, 'pending-recovery').status == 'interrupted'
