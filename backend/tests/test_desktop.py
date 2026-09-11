"""Desktop capabilities tested only against isolated pytest-owned data and directories."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from forge import desktop, workspace
from forge.config import settings
from forge.db import Event, LoginSession, Project, Run, User, session


RENDER_TOKEN = 'test-desktop-render-token-not-a-real-secret'
CONTROL_TOKEN = 'test-desktop-control-token-not-a-real-secret'
CONTROL_HEADERS = {'X-Forge-Desktop-Control': CONTROL_TOKEN}


@pytest.fixture
def desktop_client(client, monkeypatch):
    monkeypatch.setattr(desktop, 'settings', replace(settings, desktop_token=RENDER_TOKEN, desktop_control_token=CONTROL_TOKEN))
    monkeypatch.setattr(desktop, 'directory_tickets', desktop.DirectoryTickets())
    client.headers['X-Forge-Desktop'] = RENDER_TOKEN
    client.cookies.clear()
    return client


def login(client):
    response = client.post('/api/desktop/session', headers=CONTROL_HEADERS)
    assert response.status_code == 200, response.text
    return response


def authorize(client, source):
    response = client.post('/api/desktop/authorize-directory', json={'path': str(source)}, headers=CONTROL_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


def import_body(grant):
    return {'name': 'Selected desktop project', 'import_path': grant['name'], 'desktop_directory_token': grant['ticket']}


@pytest.mark.parametrize('method,path', [('GET', '/'), ('GET', '/api/health'), ('GET', '/api/openapi.json'),
                                         ('GET', '/api/unknown-route'), ('HEAD', '/'), ('OPTIONS', '/api/projects'),
                                         ('POST', '/api/auth/login')])
def test_desktop_guard_covers_every_http_request(desktop_client, method, path):
    desktop_client.headers.pop('X-Forge-Desktop')
    assert desktop_client.request(method, path).status_code == 403
    assert desktop_client.request(method, path, headers={'X-Forge-Desktop': 'wrong'}).status_code == 403


def test_desktop_guard_rejects_duplicate_tokens_and_spoofed_origin(desktop_client):
    assert desktop_client.get('/api/health', headers=[('X-Forge-Desktop', RENDER_TOKEN), ('X-Forge-Desktop', RENDER_TOKEN)]).status_code == 403
    assert desktop_client.get('/api/health', headers={'Origin': 'https://untrusted.example'}).status_code == 403
    assert desktop_client.get('/api/health', headers={'Origin': 'http://testserver'}).status_code == 200
    desktop_client.headers.pop('X-Forge-Desktop')
    assert desktop_client.get('/api/health', headers={'Origin': 'http://testserver'}).status_code == 403


def test_health_proves_instance_ownership_without_returning_token(desktop_client, monkeypatch):
    response = desktop_client.get('/api/health')
    assert response.status_code == 200
    original = response.json()['desktop_instance']
    assert original == hashlib.sha256(CONTROL_TOKEN.encode()).hexdigest()
    assert RENDER_TOKEN not in response.text and CONTROL_TOKEN not in response.text
    rotated = 'another-test-desktop-control-token'
    monkeypatch.setattr(desktop, 'settings', replace(desktop.settings, desktop_control_token=rotated))
    changed = desktop_client.get('/api/health').json()['desktop_instance']
    assert changed == hashlib.sha256(rotated.encode()).hexdigest() and changed != original


@pytest.mark.parametrize('method,path', [('POST', '/api/desktop/session'), ('POST', '/api/desktop/authorize-directory'), ('GET', '/api/desktop/status')])
def test_control_routes_require_separate_native_authority(desktop_client, method, path):
    assert desktop_client.request(method, path).status_code == 403
    assert desktop_client.request(method, path, headers={'X-Forge-Desktop-Control': RENDER_TOKEN}).status_code == 403
    # Even an exact loopback/same-origin claim never grants browser access to
    # native session creation or a path-taking directory capability endpoint.
    assert desktop_client.request(method, path, headers={**CONTROL_HEADERS, 'Origin': 'http://testserver'}).status_code == 403


def test_desktop_routes_disabled_in_web_mode(client, tmp_path):
    health = client.get('/api/health')
    assert health.status_code == 200 and health.json() == {'ok': True, 'version': '0.14.0'}
    assert client.post('/api/desktop/session', headers=CONTROL_HEADERS).status_code == 404
    assert client.post('/api/desktop/authorize-directory', json={'path': str(tmp_path)}, headers=CONTROL_HEADERS).status_code == 404
    assert client.get('/api/desktop/status', headers=CONTROL_HEADERS).status_code == 404
    response = client.post('/api/projects', json={'name': 'Web rejection', 'import_path': 'example', 'desktop_directory_token': 't' * 32})
    assert response.status_code == 422


def test_session_mints_private_cookie_for_enabled_admin_without_password(desktop_client):
    response = login(desktop_client)
    assert response.json()['role'] == 'admin'
    assert 'password' not in response.text and RENDER_TOKEN not in response.text and CONTROL_TOKEN not in response.text
    cookie = response.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie and 'path=/' in cookie
    assert desktop_client.get('/api/auth/me').status_code == 200
    with session() as s:
        events = list(s.scalars(select(Event).where(Event.kind == 'auth.desktop_login')))
        assert len(events) == 1
        cookie_token = desktop_client.cookies.get('forge_session')
        assert cookie_token not in json.dumps(events[0].data)
        assert s.get(LoginSession, cookie_token) is None
        user = s.scalar(select(User).where(User.role == 'admin'))
        user.enabled = False
    assert desktop_client.post('/api/desktop/session', headers=CONTROL_HEADERS).status_code == 403
    assert desktop_client.get('/api/auth/me').status_code == 401


def test_existing_session_cannot_bypass_desktop_guard(desktop_client):
    login(desktop_client)
    assert desktop_client.get('/api/projects').status_code == 200
    desktop_client.headers.pop('X-Forge-Desktop')
    assert desktop_client.get('/api/projects').status_code == 403
    assert desktop_client.get('/api/auth/me', headers={'Origin': 'http://testserver'}).status_code == 403


def test_native_directory_snapshot_excludes_secrets_and_never_persists_ticket(desktop_client, tmp_path):
    login(desktop_client)
    source = tmp_path / 'source'
    (source / 'src').mkdir(parents=True)
    (source / 'src/main.py').write_text('print("fixture")\n')
    for name in ('.env', '.env.local', 'server.pem', '.npmrc', 'credentials.json'):
        (source / name).write_text('fixture secret')
    for name in ('.ssh', '.codex', '.docker', '.kube', '.local', '.config'):
        (source / name).mkdir()
        (source / name / 'config.json').write_text('fixture secret')
    (source / '.git').mkdir()
    (source / '.git/config').write_text('untrusted git settings')
    (source / '.forgeignore').write_text('ignored.txt\n')
    (source / 'ignored.txt').write_text('ignored')
    grant = authorize(desktop_client, source)
    assert grant['path'] == str(source.resolve()) and grant['name'] == 'source'
    response = desktop_client.post('/api/projects', json=import_body(grant))
    assert response.status_code == 201, response.text
    project = response.json()
    assert project['desktop_source_path'] == str(source.resolve())
    assert grant['ticket'] not in response.text and 'desktop_directory_token' not in project
    copy = settings.data_root / 'projects' / project['id']
    assert workspace.list_files(copy) == ['.forgeignore', 'src/main.py']
    assert (source / 'src/main.py').read_text() == 'print("fixture")\n'
    assert (copy / '.git/config').read_text() != 'untrusted git settings'
    body = {key: value for key, value in project.items() if key not in ('id', 'owner_id', 'base_commit', 'created_at', 'desktop_source_path')}
    body['name'] = 'Renamed desktop project'
    updated = desktop_client.put('/api/projects/' + project['id'], json=body)
    assert updated.status_code == 200, updated.text
    assert updated.json()['desktop_source_path'] == grant['path']
    with session() as s:
        assert 'desktop_directory_token' not in s.get(Project, project['id']).config
        event = s.scalar(select(Event).where(Event.kind == 'project.imported'))
        assert event.data['desktop_source_path'] == grant['path']
        assert grant['ticket'] not in json.dumps(event.data)
    assert desktop_client.post('/api/projects', json=import_body(grant)).status_code == 422


def test_directory_authority_expires_and_is_single_use(desktop_client, tmp_path, monkeypatch):
    moment = [100.0]
    monkeypatch.setattr(desktop, 'time', SimpleNamespace(monotonic=lambda: moment[0]))
    grant = authorize(desktop_client, tmp_path)
    moment[0] += desktop.DIRECTORY_TICKET_TTL
    with pytest.raises(ValueError, match='过期'):
        desktop.directory_tickets.consume(grant['ticket'])
    fresh = authorize(desktop_client, tmp_path)
    assert desktop.directory_tickets.consume(fresh['ticket']).path == tmp_path.resolve()
    with pytest.raises(ValueError, match='已使用'):
        desktop.directory_tickets.consume(fresh['ticket'])


def test_directory_identity_replacement_is_rejected_and_consumes_ticket(desktop_client, tmp_path):
    login(desktop_client)
    source = tmp_path / 'source'
    source.mkdir()
    grant = authorize(desktop_client, source)
    source.rename(tmp_path / 'previous-source')
    source.mkdir()
    (source / 'different.txt').write_text('replacement fixture')
    response = desktop_client.post('/api/projects', json=import_body(grant))
    assert response.status_code == 422 and '变化' in response.text
    assert desktop_client.post('/api/projects', json=import_body(grant)).status_code == 422
    assert list((settings.data_root / 'projects').iterdir()) == []


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'parent_symlink', 'root_symlink', 'ignore_symlink'])
def test_native_snapshot_rejects_links(desktop_client, tmp_path, kind):
    login(desktop_client)
    source = tmp_path / 'source'
    source.mkdir()
    other = tmp_path / 'other'
    other.mkdir()
    (other / 'private.txt').write_text('other fixture')
    grant = authorize(desktop_client, source)
    if kind == 'hardlink': os.link(other / 'private.txt', source / 'linked.txt')
    elif kind == 'parent_symlink': (source / 'linked').symlink_to(other, target_is_directory=True)
    elif kind == 'ignore_symlink': (source / '.forgeignore').symlink_to(other / 'private.txt')
    elif kind == 'root_symlink':
        source.rmdir()
        source.symlink_to(other, target_is_directory=True)
    else: (source / 'linked.txt').symlink_to(other / 'private.txt')
    assert desktop_client.post('/api/projects', json=import_body(grant)).status_code == 422
    assert (other / 'private.txt').read_text() == 'other fixture'
    assert list((settings.data_root / 'projects').iterdir()) == []


def test_absolute_input_cannot_bypass_native_selection(desktop_client, tmp_path):
    login(desktop_client)
    grant = authorize(desktop_client, tmp_path)
    assert desktop_client.post('/api/projects', json={'name': 'Direct path', 'import_path': str(tmp_path)}).status_code == 422
    assert desktop_client.post('/api/projects', json={**import_body(grant), 'import_path': str(tmp_path)}).status_code == 422
    assert desktop_client.post('/api/desktop/authorize-directory', json={'path': '../relative'}, headers=CONTROL_HEADERS).status_code == 422
    link = tmp_path / 'link'
    link.symlink_to(tmp_path, target_is_directory=True)
    assert desktop_client.post('/api/desktop/authorize-directory', json={'path': str(link)}, headers=CONTROL_HEADERS).status_code == 422


def test_directory_selection_rejects_account_and_platform_boundaries(desktop_client, tmp_path, monkeypatch):
    account = tmp_path / 'account'
    data = account / 'application/platform/data'
    imports = account / 'application/platform/imports'
    for path in (data / 'nested', imports / 'concrete-project', account / 'projects/demo', tmp_path / 'external-project'):
        path.mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: account))
    monkeypatch.setattr(desktop, 'settings', replace(desktop.settings, data_root=data, import_root=imports))
    for path in (Path('/'), account, account.parent, data, data.parent, data / 'nested', imports):
        response = desktop_client.post('/api/desktop/authorize-directory', json={'path': str(path)}, headers=CONTROL_HEADERS)
        assert response.status_code == 422, (path, response.text)
    for path in (account / 'projects/demo', imports / 'concrete-project', tmp_path / 'external-project'):
        assert authorize(desktop_client, path)['path'] == str(path.resolve())
    for name in ('.codex', '.docker', '.kube'):
        path = account / name
        path.mkdir()
        response = desktop_client.post('/api/desktop/authorize-directory', json={'path': str(path)}, headers=CONTROL_HEADERS)
        assert response.status_code == 422


def test_file_replaced_after_enumeration_fails_without_copying_link(desktop_client, tmp_path, monkeypatch):
    login(desktop_client)
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'ordinary.txt').write_text('selected fixture')
    other = tmp_path / 'other.txt'
    other.write_text('unselected fixture')
    grant = authorize(desktop_client, source)
    actual_read = workspace._read_regular_at

    def replace_before_read(parent_fd, name, info, maximum, verify):
        if name == 'ordinary.txt':
            (source / name).unlink()
            (source / name).symlink_to(other)
        return actual_read(parent_fd, name, info, maximum, verify)

    monkeypatch.setattr(workspace, '_read_regular_at', replace_before_read)
    assert desktop_client.post('/api/projects', json=import_body(grant)).status_code == 422
    assert other.read_text() == 'unselected fixture'
    assert list((settings.data_root / 'projects').iterdir()) == []


def test_status_counts_every_nonterminal_state(desktop_client):
    login(desktop_client)
    with session() as s:
        for state in ('queued', 'paused', 'running', 'awaiting_acceptance', 'completed', 'cancelled', 'needs_attention'):
            s.add(Run(project_id='fixture-project', owner_id='fixture-owner', title='Fixture', request='Fixture', snapshot={}, status=state))
    response = desktop_client.get('/api/desktop/status', headers=CONTROL_HEADERS)
    assert response.status_code == 200
    assert response.json()['active_runs'] == 4
    assert response.json()['nonterminal_runs'] == {'queued': 1, 'paused': 1, 'running': 1, 'awaiting_acceptance': 1}
    assert response.json()['dispatcher'] == 'disabled (test only)'
    assert response.json()['version']
