"""Public tool/API integration against real Seatbelt; temporary projects only."""
from dataclasses import replace
from pathlib import Path
import shlex
import sys
from uuid import uuid4

import pytest

from conftest import run_body
from forge import engine, local_executor as native, tools, workspace


pytestmark = pytest.mark.skipif(
    sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').is_file(),
    reason='Real native tool discovery requires macOS Seatbelt',
)


@pytest.fixture
def native_project(client, configured, monkeypatch):
    monkeypatch.setattr(native, 'settings', replace(native.settings, local_execution_enabled=True))

    async def no_inference(*args, **kwargs):
        raise AssertionError('Native integration must not call a model')

    monkeypatch.setattr(engine.providers, 'complete', no_inference)
    response = client.post('/api/projects', json={
        'name': 'Native dynamic tool fixture', 'import_path': 'tiny',
        'execution_mode': 'local', 'permission_profile': 'ask',
    })
    assert response.status_code == 201, response.text
    response = client.post('/api/runs', json=run_body(configured, project_id=response.json()['id']))
    assert response.status_code == 202, response.text
    rid = response.json()['id']
    prepared = engine.prepare_run({'run_id': rid})
    aid = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    key = 'dynamic-tools'
    engine.create_task(rid, key, 'develop', aid, 'Inspect actual task tools', {})
    return {
        'id': rid, 'key': key, 'root': workspace.task_root(rid, key).resolve(),
        'runtime': prepared['runtime'],
    }


async def invoke(project, name, arguments, operation):
    return await tools.invoke(name, arguments, [name], project['root'], project['id'],
                              project['key'], operation, project['runtime'], 'unused-local-image')


def receipts(client, project):
    response = client.get('/api/runs/' + project['id'] + '/events?limit=1000')
    assert response.status_code == 200, response.text
    return [e['data']['result'] for e in response.json()['events']
            if e['kind'] == 'execution.native.completed']


def assert_native_receipt(receipt, project):
    assert receipt['runtime'] == 'seatbelt'
    assert receipt['execution_mode'] == 'local'
    assert receipt['permission_profile'] == 'ask'
    assert receipt['cwd'] == str(project['root'])
    assert receipt['network'] == 'denied'
    assert receipt['exit_code'] == 0
    assert not receipt['timed_out'] and not receipt['truncated']


@pytest.mark.asyncio
@pytest.mark.parametrize('bin_directory', ['node_modules/.bin', '.forge-tools/bin'])
async def test_arbitrary_cli_discovery_is_native_and_never_runs_candidates(client, native_project, bin_directory):
    project = native_project
    name = 'specialist-tool-' + uuid4().hex[:12]
    missing = 'not-installed-' + uuid4().hex[:12]
    candidate = project['root'] / bin_directory / name
    candidate.parent.mkdir(parents=True)
    marker = project['root'] / 'candidate-was-executed.txt'
    candidate.write_text('#!/bin/sh\nprintf invoked > ' + shlex.quote(str(marker)) + '\n')
    candidate.chmod(0o755)
    (project['root'] / 'package.json').write_text('{"name":"dynamic-fixture"}\n')

    result = await invoke(project, 'inspect_environment', {'executables': [name, missing]}, 'discover-real')

    assert result['executables'] == [
        {'name': name, 'available': True, 'resolved_path': str(candidate), 'version_verified': False},
        {'name': missing, 'available': False, 'resolved_path': None, 'version_verified': False},
    ]
    assert result['execution_mode'] == 'local' and result['cwd'] == str(project['root'])
    assert result['project_manifests'] == ['package.json']
    assert not result['installation_performed'] and not marker.exists()
    recorded = receipts(client, project)
    assert len(recorded) == 1
    assert_native_receipt(recorded[0], project)
    assert recorded[0]['execution_id'] == result['execution_id']
    response = client.get('/api/execution/tools?run_id=' + project['id'])
    assert response.status_code == 200, response.text
    assert response.json()['items'][0]['execution_id'] == result['execution_id']
    assert response.json()['items'][0]['executables'] == result['executables']
    assert client.get('/api/runs/' + project['id']).json()['calls'] == 0


@pytest.mark.asyncio
async def test_redetection_observes_new_task_local_tool_without_fixed_catalog(client, native_project):
    project = native_project
    name = 'new-capability-' + uuid4().hex[:12]
    candidate = project['root'] / '.forge-tools/bin' / name
    marker = project['root'] / 'new-tool-was-executed.txt'
    before = await invoke(project, 'inspect_environment', {'executables': [name]}, 'discover-before-install')
    assert before['executables'][0]['available'] is False

    # A benign task-local CLI is installed by the real command adapter, offline.
    source = '#!/bin/sh\nprintf invoked > ' + shlex.quote(str(marker)) + '\n'
    install = ('from pathlib import Path; '
               f'p=Path({str(candidate.relative_to(project["root"]))!r}); '
               'p.parent.mkdir(parents=True,exist_ok=True); '
               f'p.write_text({source!r}); p.chmod(0o755)')
    installed = await invoke(project, 'run_command',
        {'command': 'python3 -I -c ' + shlex.quote(install)}, 'install-task-local-fixture')
    assert_native_receipt(installed, project)
    assert candidate.is_file()

    after = await invoke(project, 'inspect_environment', {'executables': [name]}, 'discover-after-install')
    assert after['executables'] == [
        {'name': name, 'available': True, 'resolved_path': str(candidate), 'version_verified': False},
    ]
    assert after['execution_id'] != before['execution_id']
    assert not after['installation_performed'] and not marker.exists()
    recorded = receipts(client, project)
    assert len(recorded) == 3
    for receipt in recorded:
        assert_native_receipt(receipt, project)
    response = client.get('/api/execution/tools?run_id=' + project['id'])
    assert response.status_code == 200, response.text
    assert [row['executables'][0]['available'] for row in response.json()['items']] == [True, False]
    assert client.get('/api/runs/' + project['id']).json()['calls'] == 0


@pytest.mark.asyncio
async def test_approved_offline_example_really_installs_and_runs_through_native_adapter(client, native_project):
    project = native_project
    document = '# Requirements\n\n## Acceptance\nActual native document.\n'
    written = await invoke(project, 'write_file', {'path': 'requirements.md', 'content': document}, 'write-document')
    assert written['written'] == 'requirements.md'
    requested = await invoke(project, 'request_tool_installation', {
        'package_id': 'forge-text-tools', 'version': '1.0.0',
        'reason': 'Inspect the real document with the reviewed offline fixture',
    }, 'request-offline-package')
    iid = requested['installation_id']
    reference = {'installation_id': iid}
    assert requested['installation_status'] == 'pending'
    with pytest.raises(ValueError, match='尚未获管理员批准'):
        await invoke(project, 'install_approved_tool', reference, 'unapproved-install')
    assert not receipts(client, project)
    assert not (project['root'] / '.forge-tools').exists()

    response = client.get('/api/tool-installations?run_id=' + project['id'])
    assert response.status_code == 200, response.text
    row = next(item for item in response.json()['items'] if item['id'] == iid)
    catalog = client.get('/api/tool-packages')
    assert catalog.status_code == 200, catalog.text
    manifest = next(item for item in catalog.json()['items'] if item['id'] == 'forge-text-tools')
    assert row['manifest'] == manifest
    assert row['execution_mode'] == 'local' and row['permission_profile'] == 'ask'
    assert Path(row['cwd']).resolve() == project['root']
    assert manifest['version'] == '1.0.0' and manifest['network'] == 'none'
    assert manifest['files'] and manifest['install_command']
    approved = client.post('/api/tool-installations/' + iid + '/review', json={
        'digest': row['digest'], 'approved': True,
        'comment': 'Reviewed the immutable bundled source, version, and task-only scope',
    })
    assert approved.status_code == 200, approved.text
    assert approved.json()['status'] == 'approved' and approved.json()['reviewed_by']
    assert not receipts(client, project)

    installed = await invoke(project, 'install_approved_tool', reference, 'native-install-package')
    assert installed['status'] == 'installed'
    assert installed['result']['sha256'] == manifest['sha256']
    assert (project['root'] / '.forge-tools/forge-text-tools/1.0.0/forge_text_tools.py').is_file()
    result = await invoke(project, 'run_installed_tool', {
        **reference, 'operation': 'text_metrics', 'path': 'requirements.md',
    }, 'native-call-package')
    assert result['status'] == 'completed'
    assert result['result']['headings'] == 2
    assert result['result']['characters'] == len(document)

    recorded = receipts(client, project)
    assert len(recorded) == 2
    assert recorded[0]['execution_id'] != recorded[1]['execution_id']
    for receipt in recorded:
        assert_native_receipt(receipt, project)
    response = client.get('/api/tool-installations?run_id=' + project['id'])
    assert response.status_code == 200, response.text
    persisted = next(item for item in response.json()['items'] if item['id'] == iid)
    assert persisted['status'] == 'installed' and persisted['result'] == installed['result']
    events = client.get('/api/runs/' + project['id'] + '/events?limit=1000').json()['events']
    kinds = {event['kind'] for event in events}
    assert {'tool.installation.requested', 'tool.installation.reviewed',
            'tool.installation.completed', 'tool.installed.invoked'} <= kinds
    assert client.get('/api/runs/' + project['id']).json()['calls'] == 0
