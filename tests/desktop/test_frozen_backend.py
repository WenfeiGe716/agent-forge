"""Real frozen executable smoke test; no provider requests, production data or GUI mocks."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import httpx
import pytest

BINARY = Path(os.environ.get('FORGE_DESKTOP_TEST_BINARY', str(Path(__file__).resolve().parents[2] / 'desktop/release/Agent Forge-darwin-arm64/Agent Forge.app/Contents/Resources/backend/forge-server')))


def assert_protocol_change_guard(client):
    """Prove the shipped binary, not only source imports, contains the guard."""
    connection = {'name': 'Packaged protocol regression', 'protocol': 'openai_chat',
                  'base_url': 'https://example.invalid/v1', 'external': True}
    response = client.post('/api/providers', json=connection)
    assert response.status_code == 201
    provider_id = response.json()['id']
    model = {'name': 'Legacy output-limit fixture', 'provider_id': provider_id,
             'model_id': 'fixture-only', 'allowed_parameters': ['max_tokens'],
             'parameters': {'max_tokens': 4096}, 'supports_tools': False}
    created = client.post('/api/models', json=model)
    assert created.status_code == 201
    model_id = created.json()['id']
    original = client.get('/api/providers').json()
    rejected = client.put('/api/providers/' + provider_id,
                          json={**connection, 'protocol': 'openai_responses'})
    assert rejected.status_code in (400, 409, 422), 'packaged backend accepted incompatible protocol'
    detail = rejected.json()['detail']
    for label in (model['name'], model['model_id'], model_id):
        assert label in detail
    assert 'protocol' in detail.lower()
    assert client.get('/api/providers').json() == original
    assert client.get('/api/models').json()[0] == created.json()
    # Both protocols accept an explicitly empty set. No silent migration needed.
    assert client.put('/api/models/' + model_id,
                      json={**model, 'allowed_parameters': [], 'parameters': {}}).status_code == 200
    assert client.put('/api/providers/' + provider_id,
                      json={**connection, 'protocol': 'openai_responses'}).status_code == 200


@pytest.mark.skipif(not BINARY.is_file(), reason='build the bundled backend first')
def test_frozen_process_auth_directory_snapshot_and_clean_shutdown(tmp_path):
    dest = tmp_path / 'workspace'
    prep = subprocess.run([str(BINARY), '--desktop'], input=json.dumps({'action': 'prepare', 'data_dir': str(dest)})+'\n', text=True, capture_output=True, timeout=30)
    assert prep.returncode == 0, 'frozen preparation failed'
    prepared = json.loads(prep.stdout)
    staging = Path(prepared['staging_dir'])
    # This test exercises the subprocess protocol, not macOS keychain encryption.
    (staging / 'secrets.enc').write_bytes(b'test-protocol-sentinel')
    staging.rename(dest)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port = sock.getsockname()[1]
    desktop_token, control_token = 'test-desktop-'+'a'*40, 'test-control-'+'b'*40
    payload = {'action':'serve','data_dir':str(dest),'port':port,'desktop_token':desktop_token,'control_token':control_token,'secrets':prepared['secrets']}
    child = subprocess.Popen([str(BINARY), '--desktop'], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    child.stdin.write(json.dumps(payload)+'\n'); child.stdin.close()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}', headers={'X-Forge-Desktop':desktop_token,'X-Forge-Request':'1'}, timeout=3) as client:
            for _ in range(100):
                assert child.poll() is None, 'frozen backend exited before readiness'
                try:
                    health = client.get('/api/health')
                    if health.status_code == 200: break
                except httpx.HTTPError: pass
                time.sleep(0.1)
            else: pytest.fail('frozen backend did not become ready')
            assert health.json()['desktop_instance'] == hashlib.sha256(control_token.encode()).hexdigest()
            assert client.get('/').status_code == 200
            assert '<script src="/app.js"' in client.get('/').text
            assert client.get('/app.js').status_code == 200
            assert httpx.get(f'http://127.0.0.1:{port}/api/health').status_code == 403
            assert client.post('/api/desktop/session').status_code == 403
            control = {'X-Forge-Desktop-Control':control_token}
            assert client.post('/api/desktop/session',headers=control).status_code == 200
            assert client.get('/api/auth/me').json()['role'] == 'admin'
            assert isinstance(client.get('/api/agents').json(), list)
            assert_protocol_change_guard(client)
            source = tmp_path / 'selected-project'
            source.mkdir(); (source / 'README.md').write_text('A real imported document')
            (source / '.env').write_text('TEST_ONLY_SECRET=excluded')
            grant = client.post('/api/desktop/authorize-directory',headers=control,json={'path':str(source)}).json()
            response = client.post('/api/projects',json={'name':'Desktop smoke project','import_path':grant['name'],'desktop_directory_token':grant['ticket'],'execution_mode':'local','permission_profile':'ask'})
            assert response.status_code == 201, response.json()
            project = response.json()
            assert project['desktop_source_path'] == str(source)
            files = dest / 'data/projects' / project['id']
            assert (files / 'README.md').read_text() == 'A real imported document'
            assert not (files / '.env').exists()
            assert client.get('/api/desktop/status',headers=control).json()['active_runs'] == 0
    finally:
        child.terminate()
        try: child.wait(timeout=10)
        except subprocess.TimeoutExpired: child.kill();child.wait();pytest.fail('frozen backend failed graceful stop')
