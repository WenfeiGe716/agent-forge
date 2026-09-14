#!/usr/bin/env python3
"""Start the pinned source gateway and verify loopback health/auth, without a model call.

On macOS, an OS-level deny rule additionally protects personal Codex/OpenCodex
directories. All test config and logs live in a disposable Forge directory.
"""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
BUN = ROOT / '.local/build/gateway-toolchain/node_modules/@oven/bun-darwin-aarch64/bin/bun'


def main() -> int:
    resources = os.environ.get('FORGE_TEST_GATEWAY_RESOURCES')
    packaged = Path(resources).resolve() if resources else None
    bun = packaged / 'bun' if packaged else BUN
    entry = packaged / 'gateway/runtime/opencodex-gateway.ts' if packaged else ROOT / 'runtime/opencodex-gateway.ts'
    if sys.platform != 'darwin' or not bun.is_file():
        print(json.dumps({'ok': False, 'error': 'This smoke test requires the project-owned macOS Bun runtime.'}))
        return 1
    protected = [Path.home() / name for name in ('.codex', '.opencodex', '.agents')]
    rules = ' '.join(f'(subpath {json.dumps(str(path))})' for path in protected)
    profile = f'(version 1) (allow default) (deny file-read* file-write* {rules})'
    with tempfile.TemporaryDirectory(prefix='agent-forge-gateway-') as temp:
        root = Path(temp)
        gateway_home = root / 'opencodex'
        gateway_home.mkdir(mode=0o700)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        config = {
            'hostname': '127.0.0.1', 'port': port, 'runtimeRole': 'hub',
            'clientIntegrations': {'codex': False, 'grok': False},
            'codexAutoStart': False, 'codexShimAutoRestore': False,
            'openaiProviderTierVersion': 2,
            'unauthenticatedLoopbackListener': {'enabled': False},
            'defaultProvider': 'forge-healthcheck',
            'providers': {
                'openai': {'disabled': True, 'adapter': 'openai-responses', 'baseUrl': 'http://127.0.0.1:1/v1',
                           'allowPrivateNetwork': True},
                'forge-healthcheck': {'adapter': 'openai-responses', 'baseUrl': 'http://127.0.0.1:1/v1',
                                      'apiKey': '${FORGE_PROVIDER_SMOKE_KEY}', 'allowPrivateNetwork': True},
            },
        }
        (gateway_home / 'config.json').write_text(json.dumps(config), encoding='utf8')
        token = secrets.token_urlsafe(32)
        environment = {key: value for key, value in os.environ.items()
                       if key in {'PATH', 'LANG', 'TMPDIR', 'HOME', 'USER'}}
        environment.update(FORGE_RUNTIME_DATA_ROOT=str(root), FORGE_OPENCODEX_API_AUTH_TOKEN=token,
                           FORGE_PROVIDER_SMOKE_KEY='health-only-no-model-request')
        if packaged:
            environment['FORGE_GATEWAY_SOURCE_ROOT'] = str(packaged / 'gateway/source')
        command = ['/usr/bin/sandbox-exec', '-p', profile, str(bun), str(entry)]
        with (root / 'gateway.log').open('w+') as log:
            process = subprocess.Popen(command, env=environment, cwd=root, stdout=log, stderr=log)
            try:
                opener = build_opener(ProxyHandler({}))
                deadline = time.monotonic() + 40
                health = None
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        with opener.open(f'http://127.0.0.1:{port}/healthz', timeout=1) as response:
                            health = json.load(response)
                        break
                    except (URLError, TimeoutError, ConnectionError):
                        time.sleep(0.2)
                if not health or health.get('pid') != process.pid or health.get('service') != 'opencodex':
                    raise RuntimeError('Gateway did not return its own healthy process identity.')
                try:
                    opener.open(f'http://127.0.0.1:{port}/v1/models', timeout=10).close()
                    raise RuntimeError('Unauthenticated model catalog request was not rejected.')
                except HTTPError as exc:
                    if exc.code != 401:
                        raise RuntimeError(f'Unexpected catalog auth status: {exc.code}') from exc
                print(json.dumps({'ok': True, 'service': 'opencodex', 'version': health.get('version'),
                                  'health_verified': True, 'unauthenticated_catalog_status': 401,
                                  'personal_directories_denied_by_os': True, 'model_requests_sent': 0}))
                return 0
            except Exception as exc:
                log.flush()
                log.seek(0)
                tail = log.read()[-4000:].replace(token, '[redacted]')
                print(json.dumps({'ok': False, 'error': str(exc), 'diagnostic': tail}))
                return 1
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)


if __name__ == '__main__':
    raise SystemExit(main())
