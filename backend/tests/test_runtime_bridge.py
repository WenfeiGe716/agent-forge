import asyncio
import os
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from forge.runtime_bridge import (CodexAppServerClient, RuntimeBridgeError,
                                  SUPPORTED_SERVER_REQUEST_KINDS, default_runtime_binary,
                                  normalize_notification, runtime_binary_status)


def test_runtime_status_does_not_use_personal_codex_home(monkeypatch):
    monkeypatch.setenv('CODEX_HOME', '/should-not-leak')
    status = runtime_binary_status(codex='codex', opencodex='opencodex')
    assert status['isolated_config'] is True
    assert status['personal_codex_home_used'] is False


def test_default_binary_never_uses_global_codex(tmp_path):
    client = CodexAppServerClient(runtime_home=tmp_path / 'runtime')
    assert client.executable == default_runtime_binary('codex')
    assert Path(client.executable).is_absolute()
    assert '/.local/runtimes/' in client.executable
    assert runtime_binary_status(codex='codex')['codex']['available'] is False


def test_runtime_rejects_personal_codex_directory():
    with pytest.raises(RuntimeBridgeError, match='个人'):
        CodexAppServerClient(runtime_home=Path.home() / '.codex')


@pytest.mark.parametrize('name', ['user-home', 'config.toml', 'auth.json'])
def test_runtime_rejects_config_symlinks_outside_its_home(tmp_path, name):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    (runtime / name).symlink_to(tmp_path / 'another-installation' / name)
    with pytest.raises(RuntimeBridgeError, match='越界'):
        CodexAppServerClient(runtime_home=runtime)


@pytest.mark.asyncio
async def test_runtime_rechecks_home_before_launch(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    client = CodexAppServerClient(runtime_home=runtime)
    (runtime / 'user-home').symlink_to(tmp_path / 'another-installation')
    with pytest.raises(RuntimeBridgeError, match='越界'):
        await client.start()
    assert client.process is None


def test_runtime_does_not_inherit_personal_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv('OPENAI_API_KEY', 'personal-secret-must-not-leak')
    monkeypatch.setenv('CODEX_HOME', str(Path.home() / '.codex'))
    monkeypatch.setenv('CODEX_UNKNOWN_SETTING', 'personal-setting')
    client = CodexAppServerClient(runtime_home=tmp_path / 'runtime')
    environment = client._environment()
    assert environment['CODEX_HOME'] == str(tmp_path / 'runtime')
    assert 'OPENAI_API_KEY' not in environment
    assert 'CODEX_UNKNOWN_SETTING' not in environment
    assert environment['HOME'] == str(tmp_path / 'runtime/user-home')
    assert environment['USERPROFILE'] == environment['HOME']


def test_extra_environment_cannot_redirect_private_config_or_skills(tmp_path):
    bridge = CodexAppServerClient(runtime_home=tmp_path / 'runtime', env={
        'HOME': str(Path.home()), 'CODEX_SQLITE_HOME': str(Path.home() / '.codex'),
        'OPENAI_API_KEY': 'not-a-forge-route',
        'FORGE_GATEWAY_TOKEN': 'project-scoped',
    })
    environment = bridge._environment()
    assert environment['HOME'] == str(tmp_path / 'runtime/user-home')
    assert 'CODEX_SQLITE_HOME' not in environment
    assert 'OPENAI_API_KEY' not in environment
    assert environment['FORGE_GATEWAY_TOKEN'] == 'project-scoped'


def test_runtime_status_route_is_read_only_and_isolated(client):
    response = client.get('/api/runtime/status')
    assert response.status_code == 200
    body = response.json()
    assert body['isolated_config'] is True
    assert body['personal_codex_home_used'] is False
    assert body['configured_transport'] == 'stdio'
    assert body['task_execution_enabled'] is False


def test_notification_preserves_codex_ids_and_payload():
    event = normalize_notification({
        'method': 'item/agentMessage/delta',
        'params': {'threadId': 'thread-1', 'turnId': 'turn-1', 'itemId': 'item-1', 'delta': 'hello'},
    }, task_id='task-1', sequence=7)
    assert event is not None
    assert event.kind == 'item/agentMessage/delta'
    assert (event.task_id, event.thread_id, event.turn_id, event.item_id, event.sequence) == ('task-1', 'thread-1', 'turn-1', 'item-1', 7)
    assert event.payload['delta'] == 'hello'


def test_notification_nested_ids_and_server_request_id():
    started = normalize_notification({'method': 'thread/started', 'params': {'thread': {'id': 't1'}}})
    completed = normalize_notification({'method': 'turn/completed', 'params': {'threadId': 't1', 'turn': {'id': 'u1'}}})
    approval = normalize_notification({'id': 'approve-1', 'method': 'item/commandExecution/requestApproval',
                                       'params': {'threadId': 't1', 'turnId': 'u1', 'itemId': 'cmd1'}})
    assert started.thread_id == completed.thread_id == 't1'
    assert completed.turn_id == 'u1'
    assert approval.request_id == 'approve-1'
    assert approval.as_dict()['request_id'] == 'approve-1'
    assert 'item/tool/requestUserInput' not in SUPPORTED_SERVER_REQUEST_KINDS


class RecordingInput:
    def __init__(self):
        self.messages = []

    def write(self, data):
        self.messages.append(json.loads(data))

    async def drain(self):
        pass


@pytest.mark.asyncio
async def test_server_approval_requires_current_request_and_only_responds_once(tmp_path):
    bridge = CodexAppServerClient(runtime_home=tmp_path / 'runtime')
    output = asyncio.StreamReader()
    stdin = RecordingInput()
    bridge.process = SimpleNamespace(stdin=stdin, stdout=output, returncode=None)
    bridge._reader_task = asyncio.create_task(bridge._read_stdout())
    output.feed_data(b'{"id":"a1","method":"item/commandExecution/requestApproval","params":{"threadId":"t1"}}\n')
    stream = bridge.events()
    event = await asyncio.wait_for(anext(stream), 1)
    assert event.request_id == 'a1'
    # Receiving a request never approves it automatically.
    assert stdin.messages == []
    await bridge.respond('a1', {'decision': 'decline'})
    assert stdin.messages == [{'id': 'a1', 'result': {'decision': 'decline'}}]
    with pytest.raises(RuntimeBridgeError, match='失效'):
        await bridge.respond('a1', {'decision': 'accept'})
    output.feed_eof()
    await bridge._reader_task
    await stream.aclose()


@pytest.mark.asyncio
async def test_event_stream_ends_with_actionable_error_on_process_exit(tmp_path):
    bridge = CodexAppServerClient(runtime_home=tmp_path / 'runtime')
    output = asyncio.StreamReader()
    bridge.process = SimpleNamespace(stdout=output)
    bridge._reader_task = asyncio.create_task(bridge._read_stdout())
    output.feed_eof()
    with pytest.raises(RuntimeBridgeError, match='已退出'):
        await asyncio.wait_for(anext(bridge.events()), 1)
    await bridge._reader_task


@pytest.mark.asyncio
async def test_cancelled_request_does_not_leave_pending_future(tmp_path):
    bridge = CodexAppServerClient(runtime_home=tmp_path / 'runtime')
    bridge.process = SimpleNamespace(stdin=RecordingInput(), returncode=None)
    request = asyncio.create_task(bridge.request('thread/read', {'threadId': 't1'}))
    await asyncio.sleep(0)
    assert len(bridge._pending) == 1
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not bridge._pending


@pytest.mark.asyncio
async def test_thread_contract_uses_upstream_fields_without_resetting_resume_permissions(monkeypatch, tmp_path):
    bridge = CodexAppServerClient(runtime_home=tmp_path / 'runtime')
    calls = []

    async def record(method, params):
        calls.append((method, params))
        return {}

    monkeypatch.setattr(bridge, 'request', record)
    await bridge.start_thread(cwd=tmp_path, model='selected-model', model_provider='forge-gateway',
                              reasoning_effort='high', approvals_reviewer='auto_review')
    assert calls[-1][1]['config'] == {'model_reasoning_effort': 'high'}
    assert calls[-1][1]['modelProvider'] == 'forge-gateway'
    assert calls[-1][1]['approvalsReviewer'] == 'auto_review'
    assert 'reasoningEffort' not in calls[-1][1]
    await bridge.resume_thread('t1')
    assert calls[-1] == ('thread/resume', {'threadId': 't1', 'excludeTurns': True})
    await bridge.list_turns('t1', cursor='next', limit=1000)
    assert calls[-1][1] == {'threadId': 't1', 'cursor': 'next', 'limit': 100}
    await bridge.steer_turn('t1', 'u1', [{'type': 'text', 'text': '补充要求'}])
    assert calls[-1][1]['expectedTurnId'] == 'u1'


@pytest.mark.asyncio
async def test_real_codex_initialize_and_thread_start_is_project_owned(tmp_path):
    if not runtime_binary_status()['codex']['available']:
        pytest.skip('codex binary unavailable')
    client = CodexAppServerClient(runtime_home=tmp_path / 'codex-home', cwd=tmp_path)
    try:
        initialized = await client.start(timeout=20)
        assert initialized['userAgent'].startswith('agent-forge/')
        assert Path(initialized['codexHome']).resolve() == (tmp_path / 'codex-home').resolve()
        account = await client.request('account/read', {'refreshToken': False})
        assert account['account'] is None
        started = await client.start_thread(cwd=tmp_path, approval_policy='never', sandbox='workspace-write')
        thread = started['thread']
        assert thread['cwd'] == str(tmp_path)
        assert Path(thread['path']).is_absolute()
        assert str(tmp_path / 'codex-home') in thread['path']
        assert os.environ.get('CODEX_HOME') != str(tmp_path / 'codex-home')
    finally:
        await client.close()
