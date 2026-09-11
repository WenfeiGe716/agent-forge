"""Real macOS Seatbelt probes, confined to temporary directories and a loopback fixture."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shlex
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import func, select

from forge import engine, local_executor as native, tools
from forge.db import Run, Task, session
from forge.execution_permissions import ExecutionPermission, request_permission, invoke
from forge.local_executor import NativeExecution
from conftest import run_body


@pytest.fixture
def native_enabled(monkeypatch):
    monkeypatch.setattr(native, 'settings', replace(native.settings, local_execution_enabled=True))


@pytest.fixture
def local_run(client, configured, native_enabled, request):
    # Existing shared fixtures deliberately remain container based.
    with session() as s:
        from forge.db import Project
        p = s.get(Project, configured['project']['id']); p.config = {**p.config, 'execution_mode': 'local', 'permission_profile': getattr(request, 'param', 'ask')}
    rid = client.post('/api/runs', json=run_body(configured)).json()['id']
    engine.prepare_run({'run_id': rid})
    aid = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    engine.create_task(rid, 'native', 'develop', aid, 'Native task fixture', {})
    return rid, aid, native.workspace.task_root(rid, 'native')


def actual_mac():
    if sys.platform != 'darwin' or not Path(native.SANDBOX).is_file(): pytest.skip('Real Seatbelt probes require macOS')


@pytest.mark.asyncio
async def test_real_node_os_and_original_test_runner_work_without_network(tmp_path, native_enabled):
    actual_mac()
    import shutil
    node = shutil.which('node', path=native.clean_environment(tmp_path)['PATH'])
    if not node: pytest.skip('Node is not installed')
    (tmp_path / 'core.test.cjs').write_text("const test=require('node:test'),assert=require('node:assert/strict');test('real OS module',()=>assert.equal(require('node:os').type(),'Darwin'));\n")
    result = await native.spawn(tmp_path.resolve(), shlex.quote(node) + ' --test core.test.cjs', {'command_timeout_s': 10})
    assert result['exit_code'] == 0, result
    assert '# pass 1' in result['output']
    assert '# fail 0' in result['output']


@pytest.mark.asyncio
async def test_real_seatbelt_writes_workspace_but_denies_external_and_dotgit(tmp_path, native_enabled):
    actual_mac()
    root = (tmp_path / 'workspace'); root.mkdir(); (root / '.git').mkdir()
    outside = tmp_path / 'outside'; outside.mkdir(); secret = outside / 'secret.txt'; secret.write_text('fixture-private')
    root = root.resolve(); secret = secret.resolve()
    command = 'echo saved > document.md; /bin/cat ' + shlex.quote(str(secret)) + '; echo BAD > ' + shlex.quote(str(outside.resolve() / 'escape.txt')) + '; echo BAD > .git/config'
    result = await native.spawn(root, command, {'command_timeout_s': 3})
    assert (root / 'document.md').read_text().strip() == 'saved'
    assert 'fixture-private' not in result['output']
    assert not (outside / 'escape.txt').exists() and not (root / '.git/config').exists()
    assert result['exit_code'] != 0


@pytest.mark.asyncio
async def test_real_seatbelt_denies_nested_sensitive_files_and_cleans_env(tmp_path, native_enabled, monkeypatch):
    actual_mac()
    (tmp_path / 'nested').mkdir(); (tmp_path / 'nested/.env.local').write_text('SYNTHETIC_SECRET')
    monkeypatch.setenv('API_KEY', 'SYNTHETIC_ENV_KEY')
    result = await native.spawn(tmp_path.resolve(), '/bin/cat nested/.env.local; /usr/bin/env', {'command_timeout_s': 3})
    assert 'SYNTHETIC_SECRET' not in result['output'] and 'SYNTHETIC_ENV_KEY' not in result['output']
    assert 'ENCRYPTION_KEY=' not in result['output'] and 'SANDBOX_TOKEN=' not in result['output']


@pytest.mark.asyncio
async def test_real_seatbelt_executes_python_and_precise_external_read_only(tmp_path, native_enabled):
    actual_mac()
    root = tmp_path / 'task'; root.mkdir(); outside = tmp_path / 'approved.txt'; outside.write_text('approved fixture')
    denied = tmp_path / 'adjacent.txt'; denied.write_text('not approved')
    paths = [{'path': str(outside.resolve()), 'access': 'read'}]
    result = await native.spawn(root.resolve(), '/bin/cat ' + shlex.quote(str(outside.resolve())), {'command_timeout_s': 3}, paths=paths)
    assert result['output'] == 'approved fixture'
    denied_result = await native.spawn(root.resolve(), '/bin/cat ' + shlex.quote(str(denied.resolve())), {'command_timeout_s': 3}, paths=paths)
    assert denied_result['exit_code'] != 0 and 'not approved' not in denied_result['output']
    write_result = await native.spawn(root.resolve(), 'echo BAD >> ' + shlex.quote(str(outside.resolve())), {'command_timeout_s': 3}, paths=paths)
    assert write_result['exit_code'] != 0
    assert outside.read_text() == 'approved fixture'
    python = await native.spawn(root.resolve(), 'python3 -I -c ' + shlex.quote('from pathlib import Path; import resource; Path("result.txt").write_text("real Python"); print(resource.getrlimit(resource.RLIMIT_FSIZE)[0])'), {'command_timeout_s': 3})
    assert python['exit_code'] == 0, python
    assert (root / 'result.txt').read_text() == 'real Python'
    assert python['output'].strip() == '104857600'


@pytest.mark.asyncio
async def test_real_seatbelt_exact_write_grant_does_not_open_parent_directory(tmp_path, native_enabled):
    actual_mac()
    root = tmp_path / 'task'; root.mkdir(); allowed = tmp_path / 'approved.txt'; denied = tmp_path / 'adjacent.txt'
    command = 'echo approved > ' + shlex.quote(str(allowed.resolve())) + '; echo BAD > ' + shlex.quote(str(denied.resolve()))
    result = await native.spawn(root.resolve(), command, {'command_timeout_s': 3}, paths=[{'path': str(allowed.resolve()), 'access': 'write'}])
    assert allowed.read_text().strip() == 'approved', result
    assert not denied.exists() and result['exit_code'] != 0


@pytest.mark.asyncio
async def test_real_default_no_network_and_explicit_network_grant(tmp_path, native_enabled):
    actual_mac()
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path); self.send_response(200); self.end_headers(); self.wfile.write(b'loopback-approved')
        def log_message(self, *_): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        command = '/usr/bin/curl --noproxy "*" --silent --show-error --connect-timeout 1 http://127.0.0.1:' + str(server.server_port) + '/fixture'
        denied = await native.spawn(tmp_path.resolve(), command, {'command_timeout_s': 3})
        assert denied['exit_code'] != 0 and not hits
        approved = await native.spawn(tmp_path.resolve(), command, {'command_timeout_s': 3}, network=True)
        assert approved['exit_code'] == 0 and approved['output'] == 'loopback-approved', approved
        assert hits == ['/fixture']
    finally: server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.asyncio
async def test_timeout_kills_inheriting_background_process_group(tmp_path, native_enabled):
    actual_mac()
    result = await native.spawn(tmp_path.resolve(), '(/bin/sleep 1; echo leaked > child.txt) & wait', {'command_timeout_s': .15})
    assert result['timed_out'] and result['exit_code'] == 124
    await asyncio.sleep(1.1)
    assert not (tmp_path / 'child.txt').exists()


@pytest.mark.asyncio
async def test_temporary_grant_cannot_escape_in_detached_session(tmp_path, native_enabled):
    actual_mac()
    root = tmp_path / 'task'; root.mkdir(); granted = tmp_path / 'exact.txt'; granted.write_text('unchanged')
    script = 'import os,time; from pathlib import Path\nchild=os.fork()\nif child: os._exit(0)\nos.setsid()\ntime.sleep(.2)\nPath(' + repr(str(granted.resolve())) + ').write_text("escaped")\n'
    result = await native.spawn(root.resolve(), 'python3 -I -c ' + shlex.quote(script), {'command_timeout_s': 1},
                                paths=[{'path': str(granted.resolve()), 'access': 'write'}])
    assert result['exit_code'] != 0 and 'Operation not permitted' in result['output']
    await asyncio.sleep(.3)
    assert granted.read_text() == 'unchanged'


@pytest.mark.asyncio
async def test_missing_or_disabled_native_runtime_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(native, 'settings', replace(native.settings, local_execution_enabled=False))
    assert not (await native.readiness())['ready']
    with pytest.raises(RuntimeError, match='未启用本机执行'):
        await native.spawn(tmp_path.resolve(), 'echo should-not-run > denied.txt', {})
    assert not (tmp_path / 'denied.txt').exists()


@pytest.mark.asyncio
async def test_tools_adapter_and_durable_identical_command_replay(client, local_run):
    actual_mac()
    rid, aid, root = local_run
    result = await tools.command(rid, 'native', 'once', 'echo one >> once.txt', {'command_timeout_s': 3}, 'unused')
    repeat = await tools.command(rid, 'native', 'once', 'echo one >> once.txt', {'command_timeout_s': 3}, 'unused')
    assert result == repeat and result['runtime'] == 'seatbelt'
    assert (root / 'once.txt').read_text() == 'one\n'
    with pytest.raises(ValueError, match='不能更换命令'):
        await tools.command(rid, 'native', 'once', 'echo changed >> once.txt', {'command_timeout_s': 3}, 'unused')


@pytest.mark.asyncio
async def test_exact_permission_admin_review_scope_and_once(client, local_run, tmp_path):
    actual_mac()
    rid, aid, root = local_run; outside = tmp_path / 'permission.txt'
    command = 'echo once >> ' + shlex.quote(str(outside.resolve()))
    requested = await request_permission(rid, 'native', 'permission-1', {'command': command, 'network': False,
        'paths': [{'path': str(outside.resolve()), 'access': 'write'}], 'reason': 'Write this exact fixture output once'})
    assert requested['status'] == 'pending' and not outside.exists()
    pid = requested['permission_id']
    with pytest.raises(ValueError, match='尚未批准'):
        await invoke('run_approved_command', {'permission_id': pid}, rid, 'native', 'no', {})
    assert client.post('/api/execution/permissions/' + pid + '/review', json={'digest': '0' * 64, 'approved': True}).status_code == 409
    response = client.post('/api/execution/permissions/' + pid + '/review', json={'digest': requested['permission_digest'], 'approved': True})
    assert response.status_code == 200, response.text
    executed = await invoke('run_approved_command', {'permission_id': pid}, rid, 'native', 'yes', {})
    repeated = await invoke('run_approved_command', {'permission_id': pid}, rid, 'native', 'again', {})
    assert executed == repeated and outside.read_text() == 'once\n'
    with pytest.raises(ValueError, match='许可.*不匹配'):
        await native.execute(rid, 'native', 'forged-command', 'echo BAD > bad.txt', {}, paths=[{'path': str(outside.resolve()), 'access': 'write'}], permission_id=pid)


def test_project_new_defaults_and_full_explicit_acknowledgement(client, configured):
    response = client.post('/api/projects', json={'name': 'New local project', 'import_path': 'tiny'})
    assert response.status_code == 201, response.text
    assert response.json()['execution_mode'] == 'local' and response.json()['permission_profile'] == 'ask'
    body = {'name': 'Full fixture', 'import_path': 'tiny', 'permission_profile': 'full'}
    assert client.post('/api/projects', json=body).status_code == 422
    response = client.post('/api/projects', json={**body, 'full_access_confirmed': True})
    assert response.status_code == 201, response.text
    assert 'full_access_confirmed' not in response.json()
    assert client.post('/api/projects', json={**body, 'full_access_confirmed': True, 'execution_mode': 'container'}).status_code == 422


def test_legacy_snapshot_missing_mode_stays_container(client, configured):
    from forge.db import Project
    with session() as s:
        p = s.get(Project, configured['project']['id']); c = dict(p.config); c.pop('execution_mode'); c.pop('permission_profile'); p.config = c
    body = client.post('/api/runs/preview-config', json=run_body(configured)).json()
    assert body['project']['execution_mode'] == 'container' and body['project']['permission_profile'] == 'ask'


@pytest.mark.asyncio
@pytest.mark.parametrize('local_run', ['auto'], indirect=True)
async def test_auto_unknown_review_waits_for_human_and_does_not_execute(client, local_run, monkeypatch):
    rid, aid, root = local_run
    from forge import execution_auto_review
    calls = []
    async def review(*args): calls.append(args); return {'decision': 'ask_user', 'reason': 'Cannot determine safely', 'digest': args[3]}
    monkeypatch.setattr(execution_auto_review, 'assess_request', review)
    request = await request_permission(rid, 'native', 'auto-pending', {'command': 'echo needs-review', 'network': True, 'paths': [], 'reason': 'Need explicit fixture permission'})
    assert request['status'] == 'pending' and calls
    with session() as s:
        assert s.get(ExecutionPermission, request['permission_id']).status == 'pending'
        assert s.scalar(select(func.count()).select_from(NativeExecution)) == 0
