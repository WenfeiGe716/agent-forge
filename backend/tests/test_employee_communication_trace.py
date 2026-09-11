from conftest import run_body
from forge.db import session, emit


def test_employee_communication_trace_skips_tool_noise_and_other_scopes(client, configured):
    made = client.post('/api/runs', json=run_body(configured))
    assert made.status_code == 202, made.text
    rid = made.json()['id']
    first, second = [a['id'] for a in configured['agents'][:2]]
    other = client.post('/api/runs', json=run_body(configured)).json()['id']
    with session() as s:
        for _ in range(1002):
            emit(s, rid, 'tool.completed', {'agent_id': first, 'tool': 'read_file'})
        emit(s, rid, 'agent.message', {'from_agent_id': first, 'to_agent_id': second, 'body': 'current-task'})
        emit(s, rid, 'agent.message', {'from_agent_id': 'unrelated-a', 'to_agent_id': 'unrelated-b', 'body': 'not-my-message'})
        emit(s, other, 'agent.message', {'from_agent_id': first, 'to_agent_id': second, 'body': 'other-task'})
        emit(s, rid, 'agent.message.delivered', {'from_agent_id': first, 'to_agent_id': second, 'message_id': 'test'})
    response = client.get(f'/api/runs/{rid}/trace', params={'agent_id': first, 'communication_only': True, 'limit': 1})
    assert response.status_code == 200
    page = response.json()
    assert len(page['items']) == 1 and page['items'][0]['data']['body'] == 'current-task'
    assert page['has_more'] is True
    next_page = client.get(f'/api/runs/{rid}/trace', params={'agent_id': first, 'communication_only': True, 'after': page['next_cursor']}).json()
    assert [e['kind'] for e in next_page['items']] == ['agent.message.delivered']
    assert next_page['has_more'] is False
    assert all(e['run_id'] == rid for e in page['items'] + next_page['items'])


def test_normal_trace_still_contains_tool_records(client, configured):
    made = client.post('/api/runs', json=run_body(configured))
    assert made.status_code == 202, made.text
    rid = made.json()['id']
    with session() as s:
        emit(s, rid, 'tool.completed', {'tool': 'read_file'})
    response = client.get(f'/api/runs/{rid}/trace')
    assert response.status_code == 200
    assert any(e['kind'] == 'tool.completed' for e in response.json()['items'])
