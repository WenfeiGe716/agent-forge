#!/usr/bin/env python3
"""Read-only audit of a live desktop team-autonomy run.

Reads a consistent SQLite transaction and bounded, nonsymlink files. Never
executes generated code, requests a model, approves, or modifies application
data. This is local evidence, not an independent upstream billing receipt.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import zipfile

LIMIT = 100_000_000
CONTRACT = 'tests/test_contract.py'
TERMINAL = {'completed', 'cancelled', 'needs_attention', 'failed'}


def parsed(value, fallback=None):
    try:
        return json.loads(value) if isinstance(value, (str, bytes)) else value
    except (TypeError, ValueError):
        return fallback


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def read_safe(root, relative):
    """Reject links/special files and changes while reading; never print data."""
    path = PurePosixPath(relative)
    if not path.parts or path.is_absolute() or '..' in path.parts or '\\' in relative:
        return None
    handles = []
    try:
        handles.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in path.parts[:-1]:
            handles.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=handles[-1]))
        fd = os.open(path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=handles[-1])
        handles.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > LIMIT:
            return None
        output = bytearray()
        while len(output) <= before.st_size:
            chunk = os.read(fd, min(1_000_000, before.st_size + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
        after = os.fstat(fd)
        unchanged = (before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        return bytes(output) if unchanged and len(output) == before.st_size else None
    except OSError:
        return None
    finally:
        for fd in reversed(handles):
            os.close(fd)


def unzip(raw):
    if not raw:
        return {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            names = [i.filename for i in infos]
            if len(infos) > 30000 or sum(i.file_size for i in infos) > LIMIT or len(set(names)) != len(names):
                return {}
            if any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts or '\\' in n for n in names):
                return {}
            return {i.filename: archive.read(i) for i in infos if not i.is_dir()}
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return {}


def audit(database, run_id, source, expected=None):
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        row = conn.execute('SELECT id,status,snapshot,calls,input_tokens,output_tokens,artifact_digest FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            return {'run_id': run_id, 'state': 'failed', 'missing': ['run_exists']}
        run = dict(row)
        snapshot = parsed(run.pop('snapshot'), {})
        tasks = [dict(r) for r in conn.execute('SELECT key,agent_id,status,spec,result FROM tasks WHERE run_id=?', (run_id,))]
        events = [dict(r) for r in conn.execute('SELECT id,kind,data FROM events WHERE run_id=? ORDER BY id', (run_id,))]
        native = [dict(r) for r in conn.execute('SELECT id,task_key,state,result FROM native_executions WHERE run_id=?', (run_id,))]
        messages = [dict(r) for r in conn.execute('SELECT id,task_key,sender_id,recipient_id,delivered_at FROM agent_messages WHERE run_id=?', (run_id,))]
        approvals = [dict(r) for r in conn.execute('SELECT id,action,payload,sent FROM outbox WHERE run_id=?', (run_id,))]
        pending_decisions = conn.execute("SELECT count(*) FROM decision_requests WHERE run_id=? AND status='pending'", (run_id,)).fetchone()[0]
    for items, fields in ((tasks, ('spec', 'result')), (events, ('data',)), (native, ('result',)), (approvals, ('payload',))):
        for item in items:
            for field in fields:
                item[field] = parsed(item[field], {}) or {}
    root = database.parent / 'data' / 'runs' / run_id
    models = [e for e in events if e['kind'] == 'model.completed']
    tools = [e for e in events if e['kind'] == 'tool.completed']
    native_starts = {e['data'].get('execution_id'): e for e in events if e['kind'] == 'execution.native.started'}
    native_ends = {e['data'].get('execution_id'): e for e in events if e['kind'] == 'execution.native.completed'}
    test_receipts, failures = [], []
    for n in native:
        result = n['result']
        start, end = native_starts.get(n['id']), native_ends.get(n['id'])
        if n['state'] == 'completed' and result.get('exit_code') != 0:
            failures.append({'execution_id': n['id'], 'task_key': n['task_key'], 'exit_code': result.get('exit_code')})
        if not start or not end:
            continue
        command = start['data'].get('command', '')
        if not re.search(r'\b(?:unittest|pytest)\b', command):
            continue
        output = result.get('output', '')
        count = re.search(r'Ran\s+([1-9][0-9]*)\s+tests?\b|\b([1-9][0-9]*)\s+passed\b', output)
        exact = n['state'] == 'completed' and end['data'].get('result') == result and any(
            t['data'].get('result') == result and t['data'].get('task_key') == n['task_key']
            for t in tools if t['data'].get('tool') in ('run_command', 'run_approved_command'))
        success = exact and result.get('exit_code') == 0 and not result.get('timed_out') and not result.get('truncated') and bool(count)
        test_receipts.append({'execution_id': n['id'], 'task_key': n['task_key'], 'started_event_id': start['id'],
                              'passed': success, 'test_count': int(next(g for g in count.groups() if g)) if count else 0,
                              'exit_code': result.get('exit_code'), 'receipt_chain_matches': exact})
    members = set(snapshot.get('agents', {}))
    leader = snapshot.get('entry_agent_id') or snapshot.get('planner_id')
    initiated = {mid: wake for t in tasks for mid, wake in t['spec'].get('message_wake_modes', {}).items()}
    delivered = []
    for m in messages:
        if not m['delivered_at'] or m['sender_id'] == m['recipient_id'] or not {m['sender_id'], m['recipient_id']} <= members:
            continue
        seen = [e for e in events if e['kind'] == 'agent.message.delivered' and e['data'].get('message_id') == m['id'] and e['data'].get('to_agent_id') == m['recipient_id']]
        consumed = any(p['id'] > e['id'] and p['data'].get('task_key') == e['data'].get('task_key') and p['data'].get('agent_id') == m['recipient_id'] for e in seen for p in models)
        if consumed:
            origin = ('agent_help_request' if initiated[m['id']] else 'agent_information') if m['id'] in initiated else 'platform_result_notification'
            delivered.append({'message_id': m['id'], 'from_agent_id': m['sender_id'], 'to_agent_id': m['recipient_id'], 'source_task_key': m['task_key'], 'origin': origin})
    peer_reads = []
    for e in tools:
        d = e['data']; r = d.get('result', {})
        if d.get('tool') != 'read_work_result' or d.get('agent_id') != leader or r.get('error'):
            continue
        path = r.get('path', '')
        peer_reads.append({'task_key': r.get('task_key'), 'path': path, 'sha256': r.get('sha256'),
                           'integration_commit': r.get('integration_commit'),
                           'matches_current_integration': digest(read_safe(root, 'integration/' + path)) == r.get('sha256')})
    baseline = read_safe(source, CONTRACT)
    baseline_sha = digest(baseline)
    copies = {'integration': digest(read_safe(root, 'integration/' + CONTRACT))}
    for t in tasks:
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', t['key']):
            raw = read_safe(root, 'tasks/' + t['key'] + '/' + CONTRACT)
            if raw is not None:
                copies[t['key']] = digest(raw)
    patch, report_raw = read_safe(root, 'artifacts/changes.patch'), read_safe(root, 'artifacts/report.json')
    report = parsed(report_raw, {}) or {}
    exports = unzip(read_safe(root, 'artifacts/source.zip'))
    artifacts = []
    for f in report.get('deliverables', []):
        p = f.get('path', '')
        current = read_safe(root, 'integration/' + p)
        artifacts.append({'path': p, 'sha256': digest(current), 'bytes': len(current) if current is not None else None,
                          'matches_report_and_zip': current is not None and digest(current) == f.get('sha256') and len(current) == f.get('size') and exports.get(p) == current})
    accepted = any(a['action'] == 'decide' and a['sent'] and a['payload'].get('phase') == 'acceptance' and
                   a['payload'].get('approved') is True and a['payload'].get('actor') and a['payload'].get('digest') == run['artifact_digest'] and any(
                       e['kind'] == 'decision.requested' and e['data'].get('command_id') == a['id'] and e['data'].get('approved') is True and e['data'].get('digest') == run['artifact_digest']
                       for e in events) for a in approvals)
    latest_tests = {}
    for receipt in sorted(test_receipts, key=lambda item: item['started_event_id']):
        latest_tests[receipt['task_key']] = receipt
    peer_paths = {r['path'] for r in peer_reads if r['matches_current_integration']}
    checks = {
        'autonomous_snapshot': snapshot.get('execution_strategy') == 'autonomous',
        'model_responses_recorded': bool(models),
        'actual_native_tests_passed': bool(latest_tests) and all(t['passed'] for t in latest_tests.values()),
        'contract_source_matches_expected': bool(baseline) and (not expected or baseline_sha == expected),
        'contract_copies_unchanged': bool(baseline) and all(h == baseline_sha for h in copies.values()),
        'concrete_delegated_work_integrated': any(t['status'] == 'integrated' and t['spec'].get('assignment_type') in ('delegation', 'message') and t['result'].get('outcome') == 'done' for t in tasks),
        'member_message_consumed_by_model': bool(delivered),
        'no_pending_human_decisions': pending_decisions == 0,
        'leader_peer_read_matches_final': bool(artifacts) and all(f['path'] in peer_paths for f in artifacts),
        'published_digest_matches': bool(patch is not None and report_raw and report.get('run_id') == run_id and digest(patch + report_raw) == run['artifact_digest']),
        'deliverables_match_report_and_zip': bool(artifacts) and all(f['matches_report_and_zip'] for f in artifacts),
        'exported_contract_unchanged': bool(baseline) and exports.get(CONTRACT) == baseline,
        'human_acceptance_matches_artifact': accepted,
        'run_completed': run['status'] == 'completed',
    }
    missing = [k for k, v in checks.items() if not v]
    state = 'passed' if not missing else 'failed' if run['status'] in TERMINAL else 'waiting'
    return {'run_id': run_id, 'state': state, 'run_status': run['status'], 'checks': checks, 'missing': missing,
            'technical_evidence_ready': not set(missing) - {'human_acceptance_matches_artifact', 'run_completed'},
            'model': {'calls_reserved': run['calls'], 'completed_responses': len(models), 'input_tokens': run['input_tokens'], 'output_tokens': run['output_tokens'],
                      'providers_models': sorted({str(e['data'].get('provider')) + '/' + str(e['data'].get('model')) for e in models})},
            'native_test_receipts': test_receipts, 'native_nonzero_exits': failures,
            'tool_error_receipts': [{'event_id': e['id'], 'tool': e['data'].get('tool'), 'task_key': e['data'].get('task_key')} for e in tools if e['data'].get('result', {}).get('error')],
            'delivered_messages': delivered, 'leader_peer_reads': peer_reads,
            'message_counts': {'agent_initiated': sum(m['origin'] != 'platform_result_notification' for m in delivered),
                               'platform_result_notifications': sum(m['origin'] == 'platform_result_notification' for m in delivered)},
            'contract_sha256': baseline_sha, 'contract_copy_count': len(copies), 'artifacts': artifacts,
            'limitations': ['Local audit only; upstream response IDs are not persisted.',
                            'Does not execute generated code or independently rerun tests.',
                            'Current file hashes and recorded reads do not prove immutable files at every past instant.',
                            'Nonterminal runs may change after this one-shot snapshot. No approval is performed.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('run_id')
    parser.add_argument('source', type=Path)
    parser.add_argument('--expected-contract-sha256')
    args = parser.parse_args()
    if not args.database.is_absolute() or not args.source.is_absolute() or not re.fullmatch(r'[a-f0-9]{32}', args.run_id):
        parser.error('Absolute database/source paths and an exact 32-character run ID are required')
    if args.expected_contract_sha256 and not re.fullmatch(r'[a-f0-9]{64}', args.expected_contract_sha256):
        parser.error('Expected contract SHA256 must be exactly 64 lowercase hex characters')
    try:
        result = audit(args.database, args.run_id, args.source, args.expected_contract_sha256)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        result = {'run_id': args.run_id, 'state': 'unavailable', 'missing': ['stable_readable_evidence']}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {'passed': 0, 'waiting': 2, 'failed': 1, 'unavailable': 3}[result['state']]


if __name__ == '__main__':
    raise SystemExit(main())
