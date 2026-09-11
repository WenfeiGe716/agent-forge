"""Invalid model finish payloads must be correctable, never silently normalized."""
import json

import httpx
import pytest

from conftest import run_body
from forge import autonomy, engine, providers, tools
from forge.providers import Reply


@pytest.mark.asyncio
async def test_autonomous_finish_wrong_field_names_returns_feedback_before_valid_retry(client, configured, monkeypatch):
    response = client.post('/api/runs', json=run_body(configured))
    assert response.status_code == 202, response.text
    rid = response.json()['id']
    engine.prepare_run({'run_id': rid})
    key = autonomy.autonomy_tick({'run_id': rid})['runnable'][0]['key']
    wrong = {'summary': 'Need a missing user decision', 'status': 'blocked', 'deliverables': [{'path': 'requirements.md'}]}
    correct = {'summary': 'Need the user to specify the target audience; no changes were made.',
               'notes': '', 'outcome': 'blocked', 'deliverables': [], 'evidence_task_keys': []}
    calls = []

    async def model(connection, model, messages, definitions, key, timeout):
        calls.append(messages)
        if len(calls) == 1:
            arguments = wrong
        else:
            feedback = [message for message in messages if message['role'] == 'tool']
            assert feedback and feedback[-1]['call_id'] == 'invalid-result'
            error = json.loads(feedback[-1]['content'])
            assert error['error'] == 'invalid_finish'
            assert error['retryable'] is True
            assert {'outcome'} <= {
                issue['loc'][-1] for issue in error['issues'] if issue['type'] == 'missing'}
            assert any(issue['type'] == 'extra_forbidden' and issue['loc'] == ['status'] for issue in error['issues'])
            assert any(issue['loc'] == ['deliverables', 0] for issue in error['issues'])
            arguments = correct
        return Reply('', [{'id': 'invalid-result' if len(calls) == 1 else 'valid-result',
                           'name': 'finish', 'arguments': arguments}], 1, 1)

    monkeypatch.setattr(engine.providers, 'complete', model)
    first = await engine.turn({'run_id': rid, 'key': key, 'index': 0})
    assert first['done'] is False
    state = client.get('/api/runs/' + rid).json()
    assert state['status'] != 'needs_attention' and state['plan'] is None
    second = await engine.turn({'run_id': rid, 'key': key, 'index': 1})
    assert second['done'] is True and second['result'] == correct
    assert len(calls) == 2
    assert await engine.turn({'run_id': rid, 'key': key, 'index': 0}) == first
    assert len(calls) == 2  # Persisted validation feedback is replay-safe.
    state = client.get('/api/runs/' + rid).json()
    assert state['plan'] is None and state['calls'] == 2
    events = client.get('/api/runs/' + rid + '/events?limit=1000').json()['events']
    assert len([event for event in events if event['kind'] == 'task.finished']) == 1


@pytest.mark.asyncio
async def test_autonomous_finish_schema_reaches_wire_without_plan_or_role_contract():
    definitions = tools.tool_definitions(['finish'], 'autonomous')

    def handler(request):
        body = json.loads(request.content)
        schema = body['tools'][0]['function']['parameters']
        assert '$ref' not in json.dumps(schema) and '$defs' not in schema
        assert set(schema['properties']) == {'summary', 'notes', 'outcome', 'deliverables', 'evidence_task_keys'}
        assert {'summary', 'outcome'} <= set(schema['required'])
        assert schema['additionalProperties'] is False
        assert set(schema['properties']['outcome']['enum']) == {'done', 'waiting', 'ready', 'blocked'}
        assert schema['properties']['deliverables']['items']['type'] == 'string'
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Fixture only'}}]})

    await providers.complete({'base_url': 'https://example.test/v1', 'protocol': 'openai_chat', 'external': True},
        {'model_id': 'fixture', 'allowed_parameters': [], 'parameters': {}},
        [{'role': 'user', 'content': 'Report the actual outcome and evidence'}], definitions, 'fixture-key', 5,
        transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize('violation', ['member_ready', 'unknown_evidence', 'unfinished_evidence', 'missing_deliverable', 'forged_permission'])
async def test_autonomous_finish_feedback_never_grants_completion_or_authority(client, configured, monkeypatch, violation):
    from forge.db import Run, Task, session
    rid = client.post('/api/runs', json=run_body(configured)).json()['id']
    engine.prepare_run({'run_id': rid})
    key = autonomy.autonomy_tick({'run_id': rid})['runnable'][0]['key']
    if violation == 'member_ready':
        aid = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
        delegated = autonomy.assign_work(rid, key, 'fixture-delegation', {
            'recipient_id': aid, 'title': 'Read source', 'instruction': 'Inspect the existing source only',
            'acceptance': 'Report the observed calculation without changing permissions'})
        with session() as s:
            task, run = s.get(Task, rid + ':' + key), s.get(Run, rid)
            task.result = engine.finish_result(run, task, run.snapshot['agents'][task.agent_id],
                                              {'outcome': 'waiting', 'summary': 'Waiting for delegated inspection'})
            task.status = 'done'
        engine.integrate_task({'run_id': rid, 'key': key})
        state = autonomy.autonomy_tick({'run_id': rid})
        assert state['runnable'] == [{'key': delegated['task_key'], 'agent_id': aid}]
        key = delegated['task_key']
    before = client.get('/api/runs/' + rid).json()
    arguments = {'outcome': 'ready', 'summary': 'Claimed completion', 'deliverables': ['calc.py']}
    if violation == 'unknown_evidence': arguments['evidence_task_keys'] = ['not-this-run']
    elif violation == 'unfinished_evidence': arguments['evidence_task_keys'] = [key]
    elif violation == 'missing_deliverable': arguments['deliverables'] = ['missing-result.md']
    elif violation == 'forged_permission': arguments['permission_profile'] = 'full_access'

    async def model(*args):
        return Reply('', [{'id': 'rejected-result', 'name': 'finish', 'arguments': arguments}], 1, 1)

    monkeypatch.setattr(engine.providers, 'complete', model)
    result = await engine.turn({'run_id': rid, 'key': key, 'index': 0})
    assert not result['done'] and result['result'] == {}
    after = client.get('/api/runs/' + rid).json()
    assert after['snapshot'] == before['snapshot'] and after['artifact_digest'] == before['artifact_digest']
    assert {item['key'] for item in after['tasks']} == {item['key'] for item in before['tasks']}
    task = next(item for item in after['tasks'] if item['key'] == key)
    assert task['status'] == 'running' and task['result'] == {}
    assert after['status'] != 'awaiting_acceptance' and after['calls'] == 1
    events = client.get('/api/runs/' + rid + '/events?limit=1000').json()['events']
    rejected = next(event['data']['result'] for event in events if event['kind'] == 'tool.completed')
    assert rejected['error'] == 'invalid_finish' and rejected['issues'] and rejected['retryable']
    assert not any(event['kind'] in ('artifacts.ready', 'plan.ready') for event in events)


@pytest.mark.asyncio
async def test_legacy_plan_schema_reaches_openai_wire_with_explicit_nested_fields():
    definitions = tools.tool_definitions(['finish'], 'plan')

    def handler(request):
        body = json.loads(request.content)
        schema = body['tools'][0]['function']['parameters']
        assert '$ref' not in json.dumps(schema) and '$defs' not in schema
        item = schema['properties']['tasks']['items']
        assert set(item['properties']) == {'key', 'title', 'description', 'agent_id', 'depends_on', 'acceptance'}
        assert {'key', 'title', 'description', 'agent_id', 'acceptance'} <= set(item['required'])
        assert item['additionalProperties'] is False
        assert item['properties']['key']['pattern']
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Fixture only'}}]})

    await providers.complete({'base_url': 'https://example.test/v1', 'protocol': 'openai_chat', 'external': True},
        {'model_id': 'fixture', 'allowed_parameters': [], 'parameters': {}},
        [{'role': 'user', 'content': 'Produce a plan'}], definitions, 'fixture-key', 5,
        transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize(('kind', 'invalid', 'valid'), [
    ('develop', {'summary': 'A document'}, {'summary': 'A document', 'notes': 'Saved requirements.md'}),
    ('develop', {'summary': 'A document', 'notes': 42}, {'summary': 'A document', 'notes': 'Saved requirements.md'}),
    ('develop', {'summary': 'A document', 'notes': '', 'fabricated': True}, {'summary': 'A document', 'notes': ''}),
    ('review', {'summary': 'Reviewed', 'verdict': 'approved', 'issues': []}, {'summary': 'Reviewed', 'verdict': 'pass', 'issues': []}),
    ('review', {'summary': 'Reviewed', 'verdict': 'pass', 'issues': [], 'extra': 'wrong'}, {'summary': 'Reviewed', 'verdict': 'pass', 'issues': []}),
    ('review', {'summary': 'Reviewed', 'verdict': 'pass', 'issues': [42]}, {'summary': 'Reviewed', 'verdict': 'rework', 'issues': ['Fix calculation']}),
    ('review', [], {'summary': 'Reviewed', 'verdict': 'pass', 'issues': []}),
])
async def test_internal_legacy_review_and_develop_finish_require_exact_contract_then_recover(client, configured, monkeypatch, kind, invalid, valid):
    rid = client.post('/api/runs', json=run_body(configured)).json()['id']
    engine.prepare_run({'run_id': rid})
    role = 'reviewer' if kind == 'review' else 'developer'
    aid = next(a['id'] for a in configured['agents'] if a['role'] == role)
    engine.create_task(rid, 'contract', kind, aid, 'Finish contract fixture', {})
    count = 0

    async def model(connection, model, messages, definitions, key, timeout):
        nonlocal count
        count += 1
        if count == 2:
            error = json.loads(messages[-1]['content'])
            assert error['error'] == 'invalid_finish' and error['retryable']
            assert error['expected_schema'] == next(d['parameters'] for d in definitions if d['name'] == 'finish')
        return Reply('', [{'id': str(count), 'name': 'finish', 'arguments': invalid if count == 1 else valid}], 1, 1)

    monkeypatch.setattr(engine.providers, 'complete', model)
    first = await engine.turn({'run_id': rid, 'key': 'contract', 'index': 0})
    assert first['done'] is False and first['result'] == {}
    second = await engine.turn({'run_id': rid, 'key': 'contract', 'index': 1})
    assert second['done'] is True and second['result'] == valid


@pytest.mark.asyncio
async def test_rejected_finish_consumes_steps_and_calls_without_automatic_budget_extension(client, configured, monkeypatch):
    rid = client.post('/api/runs', json=run_body(configured, runtime={'max_steps': 2})).json()['id']
    engine.prepare_run({'run_id': rid})
    key = autonomy.autonomy_tick({'run_id': rid})['runnable'][0]['key']
    calls = []

    async def model(*args):
        calls.append(1)
        return Reply('', [{'id': str(len(calls)), 'name': 'finish', 'arguments': {}}], 2, 3)

    monkeypatch.setattr(engine.providers, 'complete', model)
    for index in range(2):
        result = await engine.turn({'run_id': rid, 'key': key, 'index': index})
        assert not result['done']
    with pytest.raises(ValueError, match='最大执行步数'):
        await engine.turn({'run_id': rid, 'key': key, 'index': 2})
    assert len(calls) == 2
    state = client.get('/api/runs/' + rid).json()
    assert state['calls'] == 2 and state['input_tokens'] == 4 and state['output_tokens'] == 6
    events = client.get('/api/runs/' + rid + '/events?limit=1000').json()['events']
    errors = [event['data']['result'] for event in events if event['kind'] == 'tool.completed']
    assert [error['remaining_steps'] for error in errors] == [1, 0]
    assert [error['retryable'] for error in errors] == [True, False]
    assert not any(event['kind'] in ('task.finished', 'plan.ready') for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize('violation', ['unknown_agent', 'read_only_agent', 'reserved_key', 'cyclic_dependency'])
async def test_internal_legacy_finish_feedback_does_not_accept_invalid_plan_authority_or_dag(client, configured, monkeypatch, violation):
    rid = client.post('/api/runs', json=run_body(configured)).json()['id']
    engine.prepare_run({'run_id': rid})
    # Keep the old branch's authority checks without claiming that public new
    # runs still create plans or restrict autonomous assignments by job title.
    run = engine.load(rid)
    engine.create_task(rid, 'plan', 'plan', run.snapshot['entry_agent_id'], 'Internal legacy plan fixture', {})
    before_keys = {task['key'] for task in client.get('/api/runs/' + rid).json()['tasks']}
    aid = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    item = {'key': 'work', 'title': 'Work', 'description': 'Produce the requested work',
            'agent_id': aid, 'depends_on': [], 'acceptance': 'Review the saved output'}
    if violation == 'unknown_agent': item['agent_id'] = 'unknown-agent'
    elif violation == 'read_only_agent': item['agent_id'] = next(a['id'] for a in configured['agents'] if a['role'] == 'reviewer')
    elif violation == 'reserved_key': item['key'] = 'plan'
    else: item['depends_on'] = ['work']

    async def model(*args):
        return Reply('', [{'id': 'invalid', 'name': 'finish', 'arguments': {'summary': 'Proposed work', 'tasks': [item]}}], 1, 1)

    monkeypatch.setattr(engine.providers, 'complete', model)
    result = await engine.turn({'run_id': rid, 'key': 'plan', 'index': 0})
    assert not result['done'] and result['result'] == {}
    state = client.get('/api/runs/' + rid).json()
    assert state['plan'] is None
    task = next(item for item in state['tasks'] if item['key'] == 'plan')
    assert task['status'] == 'running' and task['result'] == {}
    assert {item['key'] for item in state['tasks']} == before_keys
    events = client.get('/api/runs/' + rid + '/events?limit=1000').json()['events']
    error = next(event['data']['result'] for event in events if event['kind'] == 'tool.completed')
    assert error['error'] == 'invalid_finish' and error['issues']
    assert not any(event['kind'] in ('task.finished', 'plan.ready') for event in events)


@pytest.mark.asyncio
async def test_mixed_finish_and_side_effect_is_still_rejected_without_execution(client, configured, monkeypatch):
    aid = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    rid = client.post('/api/runs', json=run_body(configured, agent_ids=[aid])).json()['id']
    engine.prepare_run({'run_id': rid})
    key = autonomy.autonomy_tick({'run_id': rid})['runnable'][0]['key']

    async def model(*args):
        return Reply('', [
            {'id': 'write', 'name': 'write_file', 'arguments': {'path': 'must-not-exist.txt', 'content': 'Wrong'}},
            {'id': 'finish', 'name': 'finish', 'arguments': {'summary': 'Wrong', 'notes': ''}},
        ], 1, 1)

    async def must_not_execute(*args, **kwargs):
        raise AssertionError('A mixed finish batch cannot execute side effects')

    monkeypatch.setattr(engine.providers, 'complete', model)
    monkeypatch.setattr(tools, 'invoke', must_not_execute)
    with pytest.raises(ValueError, match='finish必须单独提交'):
        await engine.turn({'run_id': rid, 'key': key, 'index': 0})
