#!/usr/bin/env python3
"""Live Rust/Codex team acceptance using a saved provider, with no model fixture.

This explicitly makes external model calls. All product writes use a fresh test
database. The saved provider and existing model/agent bindings are read only.
"""
import argparse
from datetime import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import time
import zipfile

import httpx
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('probe', ROOT / 'scripts/probe-configured-provider.py')
PROBE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROBE)
CONTRACT = '''import unittest
from normalize import normalize_title
from unique import unique_labels
class Normalize(unittest.TestCase):
    def test_spaces(self): self.assertEqual(normalize_title("  Hello\\t world\\n"), "Hello world")
    def test_unicode(self): self.assertEqual(normalize_title("  需求　 文档 "), "需求 文档")
    def test_empty(self): self.assertEqual(normalize_title("\\t\\n "), "")
    def test_type(self):
        with self.assertRaises(TypeError): normalize_title(123)
class Unique(unittest.TestCase):
    def test_order(self): self.assertEqual(unique_labels(["B", "a", "B", "A", "a"]), ["B", "a", "A"])
    def test_empty(self): self.assertEqual(unique_labels([]), [])
    def test_item_type(self):
        with self.assertRaises(TypeError): unique_labels(["a", 7])
    def test_container_type(self):
        with self.assertRaises(TypeError): unique_labels("abc")
if __name__ == '__main__': unittest.main()
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def collect_evidence(database, run, lead):
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        bindings = {}
        tasks = []
        for row in db.execute('SELECT key,agent_id,spec,result,status FROM tasks WHERE run_id=?', (run,)):
            spec = json.loads(row['spec'])
            tasks.append({'key': row['key'], 'agent_id': row['agent_id'], 'status': row['status'], 'assignment_type': spec.get('assignment_type'), 'integration': json.loads(row['result']).get('integration')})
            for session in spec.get('runtime_sessions', []):
                bindings[session['connection_id']] = row['agent_id']
        intervals, commands, starts, skill_readers, threads = [], [], {}, set(), set()
        for row in db.execute('SELECT connection_id,kind,payload,created_at FROM forge_runtime_messages WHERE run_id=? ORDER BY rowid', (run,)):
            payload = json.loads(row['payload'])
            worker = bindings.get(row['connection_id'])
            turn = payload.get('turn', {}).get('id') or payload.get('turnId')
            key = (row['connection_id'], turn)
            if row['kind'] == 'turn/started':
                starts[key] = row['created_at']
                threads.add(payload.get('threadId'))
            if row['kind'] == 'turn/completed' and key in starts:
                intervals.append({'worker': worker, 'start': starts[key], 'end': row['created_at'], 'status': payload.get('turn', {}).get('status')})
            item = payload.get('item', {})
            if row['kind'] == 'item/completed' and item.get('type') == 'commandExecution':
                command = {'worker': worker, 'command': item.get('command', ''), 'cwd': item.get('cwd'), 'exit_code': item.get('exitCode')}
                commands.append(command)
                if item.get('exitCode') == 0 and 'FORGE_LIVE_SKILL_EVIDENCE' in item.get('aggregatedOutput', ''):
                    skill_readers.add(worker)
        overlap = []
        for i, left in enumerate(intervals):
            for right in intervals[i + 1:]:
                if left['worker'] == right['worker'] or lead in (left['worker'], right['worker']):
                    continue
                seconds = (datetime.fromisoformat(min(left['end'], right['end'])) - datetime.fromisoformat(max(left['start'], right['start']))).total_seconds()
                if seconds > 0:
                    overlap.append({'workers': [left['worker'], right['worker']], 'seconds': round(seconds, 3)})
        messages = [dict(row) for row in db.execute('SELECT sender_id,recipient_id,delivered_at FROM agent_messages WHERE run_id=?', (run,))]
        return {'tasks': tasks, 'turn_intervals': intervals, 'overlaps': overlap, 'native_commands': commands,
                'native_skill_readers': sorted(x for x in skill_readers if x), 'codex_threads': len(threads - {None}),
                'messages': messages, 'member_reply_activations': sum(task['assignment_type'] == 'message' for task in tasks)}


def verify(directory, provider_name, model, database=None, config=None):
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    result = {'ok': False, 'owner': 'rust', 'runtime': 'unmodified-codex', 'provider': provider_name, 'model': model,
              'real_provider_used': True, 'real_model_inference_verified': False, 'model_fixture_used': False, 'stage': 'provider_probe'}
    process = None
    try:
        probe = PROBE.probe(provider_name, model, database, config)
        (directory / 'provider-probe.json').write_text(json.dumps(probe, ensure_ascii=False, indent=2) + '\n')
        if not probe['ok']:
            result['reason'] = 'Configured provider failed live streaming tool-call verification; team inference was not started.'
            result['probe'] = probe
            return result
        result['real_model_inference_verified'] = True
        _, provider, key = PROBE.configured_provider(provider_name, database, config)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        password = secrets.token_urlsafe(24)
        binary = ROOT / '.local/runtimes/codex/bin/codex'
        env = {k: v for k, v in os.environ.items() if not k.startswith('FORGE_')}
        env.update(ADMIN_PASSWORD=password, ENCRYPTION_KEY=Fernet.generate_key().decode(),
                   FORGE_CODEX_BINARY=str(binary), SECURE_COOKIE='false',
                   FORGE_OPENCODEX_ENTRY=str(ROOT / 'runtime/opencodex-gateway.ts'),
                   FORGE_BUN_BINARY=str(ROOT / '.local/build/gateway-toolchain/node_modules/@oven/bun-darwin-aarch64/bin/bun'))
        data = directory / 'data'
        command = [str(ROOT / 'rust/target/debug/forge-server'), '--listen', f'127.0.0.1:{port}', '--product-db', str(directory / 'product.sqlite3'),
                   '--rust-db', str(directory / 'runs.sqlite3'), '--data-root', str(data)]
        with (directory / 'server.log').open('w') as log, httpx.Client(base_url=f'http://127.0.0.1:{port}/api', timeout=40, trust_env=False, headers={'X-Forge-Request': '1'}) as client:
            process = subprocess.Popen(command, env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL)

            def request(path, body=None):
                response = client.get(path) if body is None else client.post(path, json=body)
                if response.status_code >= 300:
                    raise RuntimeError(f'Forge HTTP {response.status_code}: {path}')
                return response.json()

            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError('Rust host exited before readiness')
                try:
                    request('/health')
                    break
                except httpx.TransportError:
                    time.sleep(0.1)
            request('/auth/login', {'username': 'admin', 'password': password})
            saved = request('/providers', {'name': provider_name + ' live test', 'base_url': provider['base_url'], 'protocol': provider['protocol'],
                            'protocol_config': provider.get('protocol_config', {}), 'api_key': key, 'external': True})
            key = ''
            profile = request('/models', {'name': model + ' verified live', 'provider_id': saved['id'], 'model_id': model, 'supports_tools': True,
                              'parameters': {}, 'allowed_parameters': []})
            skill = request('/skills', {'files': {'SKILL.md': '---\nname: live-contract-check\ndescription: Verify the original immutable contract with native tools\n---\nRead references/check.md with a native command before doing your work. Run the assigned original tests and report actual exit codes.\n',
                        'references/check.md': 'FORGE_LIVE_SKILL_EVIDENCE: Never alter test_contract.py. Use PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_contract -v. Inspect real files and report real evidence.\n'}})
            request(f"/skills/{skill['id']}/review", {'version': 1, 'digest': skill['digest'], 'action': 'approve'})
            members = []
            for name, instruction in [
                ('Lead', 'Inspect team capabilities, then delegate normalize.py to Normalizer and unique.py to Uniqueness in the same turn. The assignments are independent and must run concurrently. Yield waiting. After their results are integrated, read_work_result for all tasks, inspect files and run all eight original tests, then submit ready with evidence_task_keys.'),
                ('Normalizer', 'Implement only normalize.py. normalize_title accepts strings only, raises TypeError otherwise, collapses any Unicode whitespace to one ASCII space, trims ends and preserves text. First ask Uniqueness directly with request_reply=true whether whitespace-only input should return an empty string; submit waiting to await the bounded reply, then resume and implement. Use native tools and run test_contract.Normalize.'),
                ('Uniqueness', 'For your main assignment implement only unique.py: unique_labels accepts a list of strings only, raises TypeError for other containers or non-string items, returns case-sensitive distinct strings in their original order. Use native tools and run test_contract.Unique. For bounded teammate questions, inspect_team, read_inbox, send a direct reply: whitespace-only normalization returns the empty string. Finish the reply task with done and substantive notes; do not duplicate your main assignment.'),
            ]:
                members.append(request('/agents', {'name': name, 'role': 'developer', 'model_profile_id': profile['id'], 'instructions': instruction,
                    'skills': [{'skill_id': skill['id'], 'version': 1, 'mode': 'always', 'allow_scripts': False, 'parameters': {}}]}))
            project = request('/projects', {'name': 'Zhikou Rust parallel live validation', 'blank': True, 'execution_mode': 'local',
                'permission_profile': 'full', 'full_access_confirmed': True, 'allow_external_models': True,
                'instructions': 'Only modify this public disposable test project. Never weaken or change test_contract.py. Every member must read its approved Skill reference with a native tool. Do not add dependencies or network calls.',
                'runtime': {'task_timeout_s': 600, 'call_timeout_s': 90, 'command_timeout_s': 30, 'max_model_calls': 20, 'max_parallel': 2, 'max_tasks': 8}})
            source = data / 'projects' / project['id']
            (source / 'normalize.py').write_text('def normalize_title(text):\n    raise NotImplementedError\n')
            (source / 'unique.py').write_text('def unique_labels(labels):\n    raise NotImplementedError\n')
            (source / 'test_contract.py').write_text(CONTRACT)
            check_env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
            baseline = subprocess.run(['python3', '-m', 'unittest', 'test_contract', '-v'], cwd=source, env=check_env, capture_output=True, text=True, timeout=30)
            (directory / 'baseline-tests.log').write_text(baseline.stdout + baseline.stderr)
            assert baseline.returncode != 0, 'Acceptance must start with failing implementation stubs'
            run = request('/runs', {'project_id': project['id'], 'title': 'Live model, parallel members and bounded peer reply',
                'entry_agent_id': members[0]['id'], 'agent_ids': [member['id'] for member in members],
                'request': 'Complete both independent modules normalize.py and unique.py using the eight unchanged tests in test_contract.py as acceptance. Inspect member capabilities and delegate both at once. Normalizer must request clarification from Uniqueness directly, yield waiting, and resume on the reply. Each member must use the approved Skill and native file/command tools. Review integrated results and run all tests before nominating delivery.'})
            result.update(stage='team_run', run_id=run['id'], codex_binary_sha256=digest(binary.read_bytes()))
            deadline = time.monotonic() + 630
            while time.monotonic() < deadline:
                detail = request('/runs/' + run['id'])
                if detail['status'] in ('awaiting_acceptance', 'needs_attention', 'cancelled'):
                    break
                time.sleep(0.5)
            evidence = collect_evidence(directory / 'product.sqlite3', run['id'], members[0]['id'])
            result.update(evidence)
            result['status'] = detail['status']
            assert detail['status'] == 'awaiting_acceptance', 'Run did not reach delivery acceptance'
            archive = client.get(f"/runs/{run['id']}/artifacts/source.zip")
            archive.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
                assert bundle.read('test_contract.py') == CONTRACT.encode(), 'Fixed tests changed'
            workspace = data / 'runs' / run['id'] / 'workspace'
            assert (source / 'normalize.py').read_text().endswith('raise NotImplementedError\n'), 'Imported source was modified'
            assert (workspace / 'test_contract.py').read_text() == CONTRACT
            check = subprocess.run(['python3', '-m', 'unittest', 'test_contract', '-v'], cwd=workspace, env=check_env, capture_output=True, text=True, timeout=30)
            (directory / 'final-tests.log').write_text(check.stdout + check.stderr)
            assert check.returncode == 0 and 'Ran 8 tests' in check.stderr, 'Final independent contract verification failed'
            assert evidence['overlaps'], 'No actual overlap between member turns'
            worker_directories = {member['id']: {item['cwd'] for item in evidence['native_commands'] if item['worker'] == member['id'] and item['cwd']} for member in members[1:]}
            assert all(worker_directories.values()), 'Missing member native command directories'
            assert not (worker_directories[members[1]['id']] & worker_directories[members[2]['id']]), 'Members used a shared command directory'
            assert all(str(workspace) not in directories for directories in worker_directories.values()), 'A member wrote directly into integration workspace'
            result['member_working_directories'] = {worker: sorted(directories) for worker, directories in worker_directories.items()}
            assert len(evidence['native_skill_readers']) == 3, 'All members must show native Skill evidence'
            assert evidence['member_reply_activations'] >= 1, 'No bounded peer reply was exercised'
            peer = [m for m in evidence['messages'] if members[0]['id'] not in (m['sender_id'], m['recipient_id'])]
            assert len(peer) >= 2 and all(m['delivered_at'] for m in peer), 'Bidirectional peer delivery is incomplete'
            result.update(ok=True, stage='verified', contract_tests_passed=8, fixed_contract_sha256=digest(CONTRACT.encode()), artifact_zip_sha256=digest(archive.content))
            # This is an explicitly requested disposable acceptance test.
            accepted = request(f"/runs/{run['id']}/decision/acceptance", {'digest': detail['artifact_digest'], 'approved': True})
            assert accepted['status'] == 'completed'
            result['status'] = accepted['status']
        return result
    except Exception as error:
        result.update(ok=False, error_type=type(error).__name__, reason=str(error)[:300])
        return result
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        (directory / 'verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', default='智蔻')
    parser.add_argument('--model', required=True)
    parser.add_argument('--database', type=Path, help='Saved provider database; defaults to .local/product.sqlite3')
    parser.add_argument('--config', type=Path, help='Local configuration holding that database encryption key')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    outcome = verify(args.output.resolve(), args.provider, args.model, args.database, args.config)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    raise SystemExit(0 if outcome['ok'] else 1)
