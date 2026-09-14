"""Behavioral compatibility gates for replacing FastAPI with the Rust host.

The frontend consumes these wire contracts. Assertions intentionally stay at
the HTTP/ASGI seam; they do not require a particular Rust implementation.
"""
import asyncio
import json

import pytest

from forge.api import app
from forge.config import settings
from forge.db import Event, Project, Run, User, session


@pytest.fixture
def contract_run(client):
    with session() as db:
        user = db.query(User).filter_by(username='admin').one()
        db.add(Project(id='contract-project', owner_id=user.id, config={'name': '兼容项目'}))
        db.add(Run(id='contract-run', project_id='contract-project', owner_id=user.id,
                   title='兼容任务', request='检查接口', status='paused',
                   snapshot={'execution_engine': 'codex', 'execution_strategy': 'autonomous'}))
        for event_id in (100, 101, 102):
            db.add(Event(id=event_id, run_id='contract-run', kind='runtime.item.agentMessage.delta',
                         data={'payload': {'delta': f'消息 {event_id}'}}))
    return 'contract-run'


def test_cookie_csrf_and_json_errors_are_compatible(client):
    login = client.post('/api/auth/login', json={'username': 'admin', 'password': settings.admin_password})
    assert login.status_code == 200
    cookie = login.headers['set-cookie'].lower()
    assert 'forge_session=' in cookie
    for attribute in ('httponly', 'samesite=strict', 'path=/', 'max-age=43200'):
        assert attribute in cookie
    assert login.headers['cache-control'] == 'no-store'
    assert login.headers['x-content-type-options'] == 'nosniff'
    assert client.post('/api/auth/logout', headers={'X-Forge-Request': ''}).status_code == 403
    missing = client.get('/api/a-route-that-does-not-exist')
    assert missing.status_code == 404
    assert missing.headers['content-type'].startswith('application/json')
    assert isinstance(missing.json()['detail'], str)
    client.cookies.clear()
    denied = client.get('/api/runs')
    assert denied.status_code == 401
    assert isinstance(denied.json()['detail'], str)


def test_event_pages_use_exclusive_cursor_and_frontend_envelope(client, contract_run):
    response = client.get(f'/api/runs/{contract_run}/events?after=100&limit=1')
    assert response.status_code == 200
    page = response.json()
    assert set(page) == {'events', 'has_more'}
    assert page['has_more'] is True
    event = page['events'][0]
    assert set(event) == {'id', 'run_id', 'kind', 'data', 'created_at'}
    assert event['id'] == 101
    assert event['run_id'] == contract_run
    assert event['data']['payload']['delta'] == '消息 101'
    assert client.get(f'/api/runs/{contract_run}/events?after=102').json() == {'events': [], 'has_more': False}
    for query in ('after=-1', 'limit=0', 'limit=1001'):
        invalid = client.get(f'/api/runs/{contract_run}/events?{query}')
        assert invalid.status_code == 422
        assert isinstance(invalid.json()['detail'], list)


async def capture_stream(client, run_id, *, after, last_event_id='0', revoke=False):
    """Read bounded real ASGI response frames and disconnect as a browser would."""
    cookie = '; '.join(f'{key}={value}' for key, value in client.cookies.items())
    headers = [(b'cookie', cookie.encode()), (b'last-event-id', last_event_id.encode()),
               (b'host', b'testserver')]
    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.3'},
             'http_version': '1.1', 'method': 'GET', 'scheme': 'http',
             'path': f'/api/runs/{run_id}/stream',
             'raw_path': f'/api/runs/{run_id}/stream'.encode(),
             'query_string': f'after={after}'.encode(), 'root_path': '',
             'headers': headers, 'client': ('127.0.0.1', 1234), 'server': ('testserver', 80)}
    disconnected = asyncio.Event()
    sent_request = False
    messages = []
    revoked = False

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        await disconnected.wait()
        return {'type': 'http.disconnect'}

    async def send(message):
        nonlocal revoked
        messages.append(message)
        body = message.get('body', b'')
        if not body:
            return
        if revoke and not revoked:
            with session() as db:
                db.query(User).filter_by(username='admin').one().enabled = False
            revoked = True
        elif not revoke or b'event: revoked\n' in body:
            disconnected.set()

    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    start = next(message for message in messages if message['type'] == 'http.response.start')
    body = b''.join(message.get('body', b'') for message in messages)
    return start, body.decode()


@pytest.mark.asyncio
async def test_sse_resumes_at_max_query_and_last_event_id(client, contract_run):
    start, text = await capture_stream(client, contract_run, after=100, last_event_id='101')
    assert start['status'] == 200
    headers = dict(start['headers'])
    assert headers[b'content-type'].startswith(b'text/event-stream')
    assert headers[b'x-accel-buffering'] == b'no'
    assert text.startswith('id: 102\ndata: ')
    event = json.loads(text.split('data: ', 1)[1].split('\n\n', 1)[0])
    assert event['id'] == 102
    assert event['data']['payload']['delta'] == '消息 102'
    _, invalid_cursor = await capture_stream(client, contract_run, after=101, last_event_id='invalid')
    assert invalid_cursor.startswith('id: 102\ndata: ')


@pytest.mark.asyncio
async def test_sse_keepalive_and_midstream_authorization_revocation(client, contract_run):
    _, body = await capture_stream(client, contract_run, after=102, revoke=True)
    assert body.startswith(': keepalive\n\n')
    assert 'event: revoked\ndata: {}\n\n' in body
