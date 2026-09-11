"""Read-only evidence audit for the normalize_title desktop validation task.

Usage: python verify_run_closure.py /absolute/workspace/forge.db RUN_ID
Exit codes: 0 passed, 1 failed, 2 waiting, 3 running/inconsistent snapshot.
Never executes project code, calls a model/network, approves, or polls. File
content and command/model output are inspected locally but never printed.
"""
import argparse
import ast
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = 'docs/requirements.md'
CONTRACT = 'tests/test_contract.py'
LIMIT = 20_000_000
WAITING = {'awaiting_approval', 'awaiting_acceptance', 'awaiting_decision', 'paused'}
FAILED = {'needs_attention', 'failed', 'cancelled'}


def safe_reader(root):
    """Bounded reads beneath this run only, refusing links and special files."""
    def read(relative):
        parts = PurePosixPath(relative).parts
        if not parts or PurePosixPath(relative).is_absolute() or '..' in parts or '\\' in relative:
            return None
        handles = []
        try:
            handles.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            for name in parts[:-1]:
                handles.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=handles[-1]))
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=handles[-1])
            handles.append(fd)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > LIMIT:
                return None
            result = b''
            while len(result) <= before.st_size:
                chunk = os.read(fd, min(1_000_000, before.st_size + 1 - len(result)))
                if not chunk: break
                result += chunk
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return None
            return result if len(result) == before.st_size else None
        except OSError:
            return None
        finally:
            for fd in reversed(handles): os.close(fd)
    return read


def unpack(raw):
    if not raw: return {}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > 30000 or sum(i.file_size for i in infos) > LIMIT:
                return {}
            names = [i.filename for i in infos]
            if len(set(names)) != len(names): return {}
            if any(PurePosixPath(n).is_absolute() or '..' in PurePosixPath(n).parts or '\\' in n for n in names): return {}
            return {i.filename: archive.read(i) for i in infos if not i.is_dir()}
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        return {}


def parsed(value, default=None):
    try: return json.loads(value) if isinstance(value, (str, bytes)) else value
    except (ValueError, TypeError): return default


def successful(result):
    return (isinstance(result, dict) and result.get('exit_code') == 0 and
            not result.get('timed_out') and not result.get('truncated'))


def functions(raw):
    try: return [n.name for n in ast.walk(ast.parse(raw)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    except (SyntaxError, ValueError, TypeError): return []


def text_metrics(raw):
    try:
        text = raw.decode('utf-8')
        return {'path': DOCUMENT, 'characters': len(text), 'lines': len(text.splitlines()),
                'words': len(re.findall(r'\S+', text)),
                'headings': sum(bool(re.match(r'^#{1,6}\s', line)) for line in text.splitlines())}
    except (AttributeError, UnicodeError):
        return None


def historical_metrics_documents(task, installation_id):
    """Reconstruct successful tool order, never execute or trust assistant prose.

    A metrics call must follow a matching successful write receipt in the same
    task history. Commands may change files, so they invalidate the known text.
    """
    calls, document, observed = {}, None, []
    for message in task.get('messages', []):
        if not isinstance(message, dict): continue
        if message.get('role') == 'assistant':
            calls = {call.get('id'): call for call in message.get('calls', []) if isinstance(call, dict)}
        elif message.get('role') == 'tool':
            call = calls.pop(message.get('call_id'), None)
            result = parsed(message.get('content'), {})
            if not call or not isinstance(result, dict): continue
            arguments = call.get('arguments', {})
            if not isinstance(arguments, dict): continue
            name = call.get('name')
            if name in ('run_command', 'run_approved_command'):
                document = None
            elif name == 'delete_file' and arguments.get('path') == DOCUMENT and result.get('deleted') == DOCUMENT:
                document = None
            elif name == 'write_file' and arguments.get('path') == DOCUMENT:
                content = arguments.get('content')
                if (isinstance(content, str) and 'error' not in result and result.get('written') == DOCUMENT
                        and result.get('bytes') == len(content.encode('utf-8'))):
                    document = content.encode('utf-8')
            elif (name == 'run_installed_tool' and arguments.get('installation_id') == installation_id
                  and arguments.get('operation') == 'text_metrics' and arguments.get('path') == DOCUMENT
                  and result.get('installation_id') == installation_id and result.get('status') == 'completed'
                  and document and text_metrics(document) == result.get('result')):
                observed.append(document)
    return observed


def metrics_appendix_preserves_document(current, original, metrics):
    if current == original: return True
    if not current or not current.startswith(original): return False
    try: appendix = current[len(original):].decode('utf-8')
    except UnicodeError: return False
    # A general content change is not accepted as a metrics-only appendix.
    return bool(re.search(r'metrics|统计|度量|characters', appendix, re.I) and all(
        re.search(r'(?<!\d)' + str(metrics[field]) + r'(?!\d)', appendix)
        for field in ('characters', 'lines', 'words', 'headings')))


def evaluate(bundle, read):
    run = bundle.get('run')
    if not run: return {'state': 'failed', 'missing': ['run_exists'], 'checks': {'run_exists': False}}
    events, native = bundle['events'], bundle['native']
    snapshot = run.get('snapshot') or {}
    agents = set(snapshot.get('agents', {}))
    native_by_id = {r['id']: r for r in native}
    def native_result(result):
        row = native_by_id.get(result.get('execution_id'))
        started = any(e['kind'] == 'execution.native.started' and e['data'].get('execution_id') == result.get('execution_id')
                      and e['data'].get('command') == result.get('command') for e in events)
        return bool(row and started and row.get('state') == 'completed' and successful(row.get('result')) and row['result'] == {k: v for k, v in result.items() if k != 'command'})
    def event_rows(kind): return [e for e in events if e['kind'] == kind]
    exports = unpack(read('artifacts/source.zip'))
    patch, report_bytes = read('artifacts/changes.patch'), read('artifacts/report.json')
    report = parsed(report_bytes, {}) or {}
    intact = bool(patch is not None and report_bytes and report.get('run_id') == run['id'] and
                  hashlib.sha256(patch + report_bytes).hexdigest() == run.get('artifact_digest'))
    current_files = {name: raw for name, raw in exports.items() if read('integration/' + name) == raw}
    code_paths = [name for name, raw in current_files.items() if name.endswith('.py') and
                  not name.startswith('tests/') and 'normalize_title' in functions(raw)]
    writes = {e['data'].get('result', {}).get('written') for e in event_rows('tool.completed')
              if e['data'].get('tool') == 'write_file' and not e['data'].get('result', {}).get('error')}
    document = current_files.get(DOCUMENT, b'')
    check_report = report.get('checks', {})
    commands = check_report.get('commands', [])
    tested = bool(commands and all(successful(c) and native_result(c) for c in commands) and
                  any(re.search(r'Ran\s+[1-9][0-9]*\s+tests?\b|\b[1-9][0-9]*\s+passed\b', c.get('output', '')) for c in commands))
    delivered = []
    for message in bundle['messages']:
        if (not message.get('delivered_at') or message['sender_id'] == message['recipient_id'] or
                not {message['sender_id'], message['recipient_id']} <= agents): continue
        receipts = [e for e in event_rows('agent.message.delivered') if e['data'].get('message_id') == message['id']
                    and e['data'].get('to_agent_id') == message['recipient_id']]
        if any(any(m['id'] > e['id'] and m['data'].get('agent_id') == message['recipient_id']
                   and m['data'].get('task_key') == e['data'].get('task_key') for m in event_rows('model.completed')) for e in receipts):
            delivered.append(message)
    # This verifier is for the approved normalize_title example. Task keys are
    # its contract; member identities come from the plan, never display names or
    # generic execution permission roles (both authors can be "developer").
    plan = run.get('plan') or {}
    items = plan.get('tasks', []) if isinstance(plan, dict) else []
    assignments = {item.get('key'): item for item in items if isinstance(item, dict)}
    requirements = assignments.get('requirements', {}).get('agent_id')
    developer = assignments.get('implementation', {}).get('agent_id')
    reviewer = snapshot.get('reviewer_id')
    resolved = (len(assignments) == len(items) and len({requirements, developer, reviewer}) == 3
                and {requirements, developer, reviewer} <= agents
                and 'requirements' in assignments.get('implementation', {}).get('depends_on', []))
    def handoff(sender, recipient, source_key, destination):
        if not resolved: return False
        return any(message.get('sender_id') == sender and message.get('recipient_id') == recipient
                   and message.get('task_key') == source_key and any(
                       event['data'].get('message_id') == message['id']
                       and event['data'].get('from_agent_id') == sender
                       and event['data'].get('to_agent_id') == recipient
                       and destination(event['data'].get('task_key', ''))
                       and any(model['id'] > event['id'] and model['data'].get('agent_id') == recipient
                               and model['data'].get('task_key') == event['data'].get('task_key')
                               for model in event_rows('model.completed'))
                       for event in event_rows('agent.message.delivered')) for message in delivered)
    requirements_handoff = handoff(requirements, developer, 'requirements', lambda key: key == 'implementation')
    review_handoff = handoff(developer, reviewer, 'implementation', lambda key: bool(re.fullmatch(r'review_[0-9]+', key)))
    installations = []
    for row in bundle['installations']:
        manifest, result = row.get('manifest', {}), row.get('result', {})
        if (manifest.get('id'), manifest.get('version'), manifest.get('network')) != ('forge-text-tools', '1.0.0', 'none'): continue
        key = row.get('task_key', '')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', key): continue
        tool_file = read(f'tasks/{key}/.forge-tools/forge-text-tools/1.0.0/forge_text_tools.py')
        reviewed = any(e['data'].get('installation_id') == row['id'] and e['data'].get('approved') is True
                       and e['data'].get('digest') == row.get('digest') and e['data'].get('actor') == row.get('reviewed_by')
                       for e in event_rows('tool.installation.reviewed'))
        installed = (row.get('status') == 'installed' and bool(row.get('reviewed_by')) and reviewed and tool_file and
                     hashlib.sha256(tool_file).hexdigest() == manifest.get('sha256') == result.get('sha256') and
                     result.get('installed') is True and result.get('package_id') == manifest['id'] and result.get('version') == '1.0.0')
        install_execution = any(r.get('task_key') == key and r.get('state') == 'completed' and successful(r.get('result'))
                                and parsed(r['result'].get('output')) == result for r in native)
        metrics_events = [e for e in event_rows('tool.installed.invoked') if e['data'].get('installation_id') == row['id']
                          and e['data'].get('result', {}).get('path') == DOCUMENT]
        metrics_ok = False
        raw = read(f'tasks/{key}/{DOCUMENT}')
        historical = [document for task in bundle.get('tasks', []) if task.get('key') == key
                      and (not row.get('agent_id') or task.get('agent_id') == row['agent_id'])
                      for document in historical_metrics_documents(task, row['id'])]
        for metrics_event in metrics_events:
            metrics = metrics_event['data'].get('result')
            native_receipt = any(r.get('task_key') == key and r.get('state') == 'completed'
                and successful(r.get('result')) and parsed(r['result'].get('output')) == metrics for r in native)
            if not native_receipt: continue
            if raw and text_metrics(raw) == metrics:
                metrics_ok = True
            for previous in historical:
                prior_write = any(event['id'] < metrics_event['id'] and event['data'].get('task_key') == key
                    and event['data'].get('tool') == 'write_file'
                    and event['data'].get('result', {}).get('written') == DOCUMENT
                    and event['data'].get('result', {}).get('bytes') == len(previous)
                    and not event['data'].get('result', {}).get('error') for event in event_rows('tool.completed'))
                if (text_metrics(previous) == metrics and prior_write
                        and metrics_appendix_preserves_document(raw, previous, metrics)):
                    metrics_ok = True
        if installed and install_execution and metrics_ok: installations.append(row['id'])
    approved = [row for row in bundle['outbox'] if row.get('action') == 'decide' and row.get('sent') and
                row.get('payload', {}).get('phase') == 'acceptance' and row['payload'].get('approved') is True and
                row['payload'].get('digest') == run.get('artifact_digest') and row['payload'].get('actor') and
                any(e['data'].get('command_id') == row['id'] and e['data'].get('phase') == 'acceptance' and
                    e['data'].get('approved') is True and e['data'].get('digest') == run.get('artifact_digest') for e in event_rows('decision.requested'))]
    checks = {
        'real_model_response_recorded': bool(event_rows('model.completed')),
        'published_artifact_digest_matches': intact,
        'requirements_file_written_and_exported': len(document) >= 30 and DOCUMENT in writes,
        'normalize_title_code_written_and_exported': bool(code_paths) and any(p in writes for p in code_paths),
        'contract_test_file_exported': any(n.startswith('test_') for n in functions(current_files.get(CONTRACT))),
        'predefined_contract_unchanged': bool(current_files.get(CONTRACT)) and current_files.get(CONTRACT) == bundle.get('baseline_contract'),
        'nonempty_native_tests_passed': check_report.get('verification') == 'passed' and check_report.get('tracked_files_modified') is False and tested,
        'approved_offline_tool_installed_and_used': bool(installations),
        'member_message_delivered_into_completed_model_call': bool(delivered),
        'planned_handoff_members_resolved': bool(resolved),
        'requirements_to_engineer_message_delivered': requirements_handoff,
        'engineer_to_reviewer_message_delivered': review_handoff,
        'independent_review_passed': report.get('review', {}).get('verdict') == 'pass',
        'human_acceptance_matches_artifact': bool(approved),
        'run_completed': run.get('status') == 'completed',
    }
    missing = [key for key, value in checks.items() if not value]
    status = run.get('status', 'unknown')
    state = 'passed' if not missing else 'failed' if status in FAILED or status == 'completed' else 'waiting' if status in WAITING else 'running'
    return {'run_id': run['id'], 'state': state, 'run_status': status, 'checks': checks, 'missing': missing,
            'ready_for_human_acceptance': status == 'awaiting_acceptance' and not set(missing) - {'human_acceptance_matches_artifact', 'run_completed'},
            'evidence_counts': {'native_executions': len(native), 'verified_tool_installations': len(installations), 'verified_delivered_messages': len(delivered),
                                'required_handoffs_verified': int(requirements_handoff) + int(review_handoff)},
            'files': [{'path': p, 'bytes': len(current_files[p]), 'sha256': hashlib.sha256(current_files[p]).hexdigest()}
                      for p in [DOCUMENT, CONTRACT, *code_paths] if p in current_files],
            'limitations': ['Static file presence and recorded results are not a new independent test execution.', 'No credentials, prompts, document contents, command outputs, or message bodies are printed.']}


def load_bundle(connection, run_id):
    connection.row_factory = sqlite3.Row
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    def rows(table):
        if table not in tables: return []
        records = connection.execute(f'SELECT * FROM {table} WHERE ' + ('id' if table == 'runs' else 'run_id') + ' = ?', (run_id,))
        output = []
        for record in records:
            item = dict(record)
            for key in ('snapshot', 'plan', 'report', 'data', 'result', 'manifest', 'payload', 'messages'):
                if key in item: item[key] = parsed(item[key], {}) or {}
            output.append(item)
        return output
    run = rows('runs')
    return {'run': run[0] if run else None, 'events': rows('events'), 'native': rows('native_executions'),
            'messages': rows('agent_messages'), 'installations': rows('tool_installations'), 'outbox': rows('outbox'), 'tasks': rows('tasks')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('run_id')
    parser.add_argument('--data-root', type=Path, help='Defaults to the desktop database sibling data directory')
    args = parser.parse_args()
    if (not args.database.is_absolute() or not re.fullmatch(r'[a-f0-9]{32}', args.run_id) or
            args.data_root is not None and not args.data_root.is_absolute()):
        parser.error('Use an absolute database path and exact 32-character run ID')
    sys.path.insert(0, str(ROOT / 'desktop'))
    from data_store import _snapshot_database
    try:
        with tempfile.TemporaryDirectory(prefix='forge-closure-audit-') as temporary:
            database, verify = _snapshot_database(args.database, Path(temporary))
            with sqlite3.connect(database) as connection:
                connection.execute('PRAGMA query_only=ON')
                bundle = load_bundle(connection, args.run_id)
            data_root = args.data_root or args.database.parent / 'data'
            project_id = (bundle.get('run') or {}).get('project_id', '')
            bundle['baseline_contract'] = (safe_reader(data_root / 'projects' / project_id)(CONTRACT)
                                           if re.fullmatch(r'[a-f0-9]{32}', project_id) else None)
            run_root = data_root / 'runs' / args.run_id
            result = evaluate(bundle, safe_reader(run_root))
            verify()
    except (OSError, ValueError, sqlite3.Error):
        result = {'run_id': args.run_id, 'state': 'running', 'missing': ['stable_read_only_snapshot'],
                  'note': 'Evidence was unavailable or changed during this one-shot read. No success conclusion; retry explicitly.'}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {'passed': 0, 'failed': 1, 'waiting': 2, 'running': 3}[result['state']]


if __name__ == '__main__':
    raise SystemExit(main())
