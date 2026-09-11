"""Invalid model finish payloads must be correctable, never silently normalized."""
import json

import httpx
import pytest

from conftest import run_body
from forge import engine, providers, tools
from forge.providers import Reply


@pytest.mark.asyncio
async def test_plan_finish_wrong_field_names_returns_feedback_before_valid_retry(client, configured, monkeypatch):
    response = client.post('/api/runs', json=run_body(configured))
    assert response.status_code == 202, response.text
    rid = response.json()['id']
    engine.prepare_run({'run_id': rid})
    developer = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    wrong = {'summary': 'Draft work plan', 'tasks': [{
        'id': 'requirements', 'title': 'Write requirements', 'description': 'Write a requirements document',
        'assignee': developer, 'dependencies': [],
    }]}
    correct = {'summary': 'Draft work plan', 'tasks': [{
        'key': 'requirements', 'title': 'Write requirements', 'description': 'Write a requirements document',
        'agent_id': developer, 'depends_on': [], 'acceptance': 'requirements.md contains acceptance criteria',
    }]}
    calls = []

    async def model(connection, model, messages, definitions, key, timeout):
        calls.append(messages)
        if len(calls) == 1:
            arguments = wrong
        else:
            feedback = [message for message in messages if message['role'] == 'tool']
            assert feedback and feedback[-1]['call_id'] == 'invalid-plan'
            error = json.loads(feedback[-1]['content'])
            assert error['error'] == 'invalid_finish'
            assert error['retryable'] is True
            assert {'key', 'agent_id', 'acceptance'} <= {
                issue['loc'][-1] for issue in error['issues'] if issue['type'] == 'missing'}
            arguments = correct
        return Reply('', [{'id': 'invalid-plan' if len(calls) == 1 else 'valid-plan',
                           'name': 'finish', 'arguments': arguments}], 1, 1)

    monkeypatch.setattr(engine.providers, 'complete', model)
    first = await engine.turn({'run_id': rid, 'key': 'plan', 'index': 0})
    assert first['done'] is False
    state = client.get('/api/runs/' + rid).json()
    assert state['status'] != 'needs_attention' and state['plan'] is None
    second = await engine.turn({'run_id': rid, 'key': 'plan', 'index': 1})
    assert second['done'] is True and second['result'] == correct
    assert len(calls) == 2
    assert await engine.turn({'run_id': rid, 'key': 'plan', 'index': 0}) == first
    assert len(calls) == 2  # Persisted validation feedback is replay-safe.
    state = client.get('/api/runs/' + rid).json()
    assert state['plan'] is None and state['calls'] == 2
    events = client.get('/api/runs/' + rid + '/events?limit=1000').json()['events']
    assert len([event for event in events if event['kind'] == 'task.finished']) == 1


@pytest.mark.asyncio
async def test_plan_schema_reaches_openai_wire_with_explicit_nested_fields():
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
async def test_review_and_develop_finish_require_exact_contract_then_recover(client, configured, monkeypatch, kind, invalid, valid):
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
    aid = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    engine.create_task(rid, 'bounded', 'develop', aid, 'Bounded correction', {})
    calls = []

    async def model(*args):
        calls.append(1)
        return Reply('', [{'id': str(len(calls)), 'name': 'finish', 'arguments': {}}], 2, 3)

    monkeypatch.setattr(engine.providers, 'complete', model)
    for index in range(2):
        result = await engine.turn({'run_id': rid, 'key': 'bounded', 'index': index})
        assert not result['done']
    with pytest.raises(ValueError, match='最大执行步数'):
        await engine.turn({'run_id': rid, 'key': 'bounded', 'index': 2})
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
async def test_finish_feedback_does_not_accept_invalid_plan_authority_or_dag(client, configured, monkeypatch, violation):
    rid = client.post('/api/runs', json=run_body(configured)).json()['id']
    engine.prepare_run({'run_id': rid})
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
    assert [task['key'] for task in state['tasks']] == ['plan']
    assert state['tasks'][0]['status'] == 'running' and state['tasks'][0]['result'] == {}
    events = client.get('/api/runs/' + rid + '/events?limit=1000').json()['events']
    error = next(event['data']['result'] for event in events if event['kind'] == 'tool.completed')
    assert error['error'] == 'invalid_finish' and error['issues']
    assert not any(event['kind'] in ('task.finished', 'plan.ready') for event in events)


@pytest.mark.asyncio
async def test_mixed_finish_and_side_effect_is_still_rejected_without_execution(client, configured, monkeypatch):
    rid = client.post('/api/runs', json=run_body(configured)).json()['id']
    engine.prepare_run({'run_id': rid})
    aid = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    engine.create_task(rid, 'mixed', 'develop', aid, 'No mixed side effects', {})

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
        await engine.turn({'run_id': rid, 'key': 'mixed', 'index': 0})
