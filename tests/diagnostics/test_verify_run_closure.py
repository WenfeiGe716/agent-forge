"""Verifier fixtures are synthetic evidence, never a claim about a real run."""
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest

spec = importlib.util.spec_from_file_location('closure_audit', Path(__file__).with_name('verify_run_closure.py'))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


@pytest.fixture
def evidence():
    run_id = 'a' * 32
    document = b'# Requirements\n\n## Acceptance\nNormalize titles and pass the contract.\n'
    files = {'docs/requirements.md': document,
             'app.py': b'def normalize_title(value):\n    return " ".join(value.strip().split())\n',
             'tests/test_contract.py': b'def test_contract():\n    assert True\n'}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as zipped:
        for name, raw in files.items(): zipped.writestr(name, raw)
    command_result = {'execution_id': 'test-execution', 'exit_code': 0, 'timed_out': False,
                      'truncated': False, 'output': 'Ran 8 tests in 0.2s\nOK\n'}
    report = {'run_id': run_id, 'checks': {'verification': 'passed', 'tracked_files_modified': False,
              'commands': [{'command': 'python -m unittest discover -s tests', **command_result}]}, 'review': {'verdict': 'pass'}}
    raw_report, patch = json.dumps(report).encode(), b'diff --git a/app.py b/app.py\nnew code\n'
    artifact_digest = hashlib.sha256(patch + raw_report).hexdigest()
    tool = b'print("synthetic shipped tool source")\n'
    digest = hashlib.sha256(tool).hexdigest()
    receipt = {'installed': True, 'package_id': 'forge-text-tools', 'version': '1.0.0', 'sha256': digest}
    metrics = {'path': 'docs/requirements.md', 'characters': len(document), 'lines': len(document.splitlines()), 'words': len(document.split()), 'headings': 2}
    def event(index, kind, **data): return {'id': index, 'kind': kind, 'data': data}
    bundle = {'run': {'id': run_id, 'status': 'completed', 'calls': 9999, 'artifact_digest': artifact_digest,
                       'snapshot': {'agents': {'lead': {}, 'requirements-agent': {}, 'dev': {}, 'reviewer': {}}, 'reviewer_id': 'reviewer'},
                       'plan': {'tasks': [{'key': 'requirements', 'agent_id': 'requirements-agent', 'depends_on': []},
                                          {'key': 'implementation', 'agent_id': 'dev', 'depends_on': ['requirements']}]}},
        'events': [event(1, 'model.completed', agent_id='lead', task_key='plan'),
                   event(2, 'tool.completed', tool='write_file', result={'written': 'docs/requirements.md'}),
                   event(3, 'tool.completed', tool='write_file', result={'written': 'app.py'}),
                   event(4, 'tool.installation.reviewed', installation_id='install', approved=True, digest='approved-digest', actor='human'),
                   event(5, 'tool.installed.invoked', installation_id='install', result=metrics),
                   event(6, 'agent.message.delivered', message_id='msg', from_agent_id='requirements-agent', to_agent_id='dev', task_key='implementation'),
                   event(7, 'model.completed', agent_id='dev', task_key='implementation'),
                   event(8, 'agent.message.delivered', message_id='review-msg', from_agent_id='dev', to_agent_id='reviewer', task_key='review_0'),
                   event(9, 'model.completed', agent_id='reviewer', task_key='review_0'),
                   event(10, 'decision.requested', command_id='accept', phase='acceptance', approved=True, digest=artifact_digest),
                   event(11, 'execution.native.started', execution_id='test-execution', command='python -m unittest discover -s tests')],
        'native': [{'id': 'test-execution', 'task_key': 'checks_0', 'state': 'completed', 'result': command_result},
                   {'id': 'install-execution', 'task_key': 'develop', 'state': 'completed', 'result': {'exit_code': 0, 'output': json.dumps(receipt)}},
                   {'id': 'metrics-execution', 'task_key': 'develop', 'state': 'completed', 'result': {'exit_code': 0, 'output': json.dumps(metrics)}}],
        'installations': [{'id': 'install', 'task_key': 'develop', 'status': 'installed', 'reviewed_by': 'human', 'digest': 'approved-digest',
                           'manifest': {'id': 'forge-text-tools', 'version': '1.0.0', 'network': 'none', 'sha256': digest}, 'result': receipt}],
        'messages': [{'id': 'msg', 'task_key': 'requirements', 'sender_id': 'requirements-agent', 'recipient_id': 'dev', 'delivered_at': '2026-09-10T10:00:00Z'},
                     {'id': 'review-msg', 'task_key': 'implementation', 'sender_id': 'dev', 'recipient_id': 'reviewer', 'delivered_at': '2026-09-10T10:00:01Z'}],
        'outbox': [{'id': 'accept', 'action': 'decide', 'sent': True, 'payload': {'phase': 'acceptance', 'approved': True, 'digest': artifact_digest, 'actor': 'human'}}],
        'baseline_contract': files['tests/test_contract.py']}
    stored = {'artifacts/source.zip': archive.getvalue(), 'artifacts/report.json': raw_report, 'artifacts/changes.patch': patch,
              'tasks/develop/.forge-tools/forge-text-tools/1.0.0/forge_text_tools.py': tool,
              'tasks/develop/docs/requirements.md': document, **{'integration/' + name: raw for name, raw in files.items()}}
    return bundle, stored


def test_complete_consistent_evidence_passes_without_mutation_or_text_disclosure(evidence):
    bundle, files = evidence
    before = deepcopy(bundle)
    bundle['run']['request'] = 'SYNTHETIC_SENSITIVE_PROMPT'
    result = audit.evaluate(bundle, files.get)
    assert result['state'] == 'passed' and not result['missing']
    assert len(result['files']) == 3 and all(f['sha256'] for f in result['files'])
    assert 'SYNTHETIC_SENSITIVE_PROMPT' not in json.dumps(result)
    bundle['run'].pop('request')
    assert bundle == before


@pytest.mark.parametrize('missing', ['document', 'code', 'contract', 'changed-contract', 'missing-contract-baseline', 'patch-digest', 'native-test', 'wrong-native-command', 'zero-tests', 'approval', 'unsent-approval', 'wrong-artifact', 'install-approval', 'install-file', 'install-execution', 'tool-invocation', 'message-delivery', 'receiver-model'])
def test_completed_claim_is_not_enough_if_any_required_evidence_is_missing(evidence, missing):
    bundle, files = evidence
    if missing in {'document', 'code', 'contract'}:
        files.pop('integration/' + {'document': 'docs/requirements.md', 'code': 'app.py', 'contract': 'tests/test_contract.py'}[missing])
    elif missing == 'patch-digest': files['artifacts/changes.patch'] += b'tampered'
    elif missing == 'changed-contract': bundle['baseline_contract'] = b'def test_strict_original():\n    assert False\n'
    elif missing == 'missing-contract-baseline': bundle.pop('baseline_contract')
    elif missing == 'native-test': bundle['native'][0]['state'] = 'started'
    elif missing == 'wrong-native-command': bundle['events'][-1]['data']['command'] = 'echo fake-test-result'
    elif missing == 'zero-tests': bundle['native'][0]['result']['output'] = 'Ran 0 tests\nOK\n'
    elif missing == 'approval': bundle['outbox'] = []
    elif missing == 'unsent-approval': bundle['outbox'][0]['sent'] = False
    elif missing == 'wrong-artifact': bundle['outbox'][0]['payload']['digest'] = 'different'
    elif missing == 'install-approval': bundle['installations'][0]['reviewed_by'] = ''
    elif missing == 'install-file': files.pop('tasks/develop/.forge-tools/forge-text-tools/1.0.0/forge_text_tools.py')
    elif missing == 'install-execution': bundle['native'][1]['state'] = 'uncertain'
    elif missing == 'tool-invocation': bundle['native'][2]['result']['exit_code'] = 1
    elif missing == 'message-delivery': bundle['messages'][0]['delivered_at'] = ''
    elif missing == 'receiver-model': bundle['events'] = [e for e in bundle['events'] if e['id'] != 7]
    result = audit.evaluate(bundle, files.get)
    assert result['state'] == 'failed' and result['missing']


def test_acceptance_wait_is_distinct_from_completed_and_can_report_ready_for_human_review(evidence):
    bundle, files = evidence
    bundle['run']['status'] = 'awaiting_acceptance'
    bundle['outbox'] = []
    result = audit.evaluate(bundle, files.get)
    assert result['state'] == 'waiting' and result['ready_for_human_acceptance']
    assert set(result['missing']) == {'human_acceptance_matches_artifact', 'run_completed'}


@pytest.mark.parametrize('status,expected', [('needs_attention', 'failed'), ('cancelled', 'failed'), ('planning', 'running'), ('awaiting_decision', 'waiting')])
def test_model_calls_never_replace_actual_file_test_install_message_evidence(evidence, status, expected):
    bundle, _ = evidence
    bundle['run']['status'] = status
    result = audit.evaluate(bundle, lambda _: None)
    assert result['state'] == expected
    assert not result['checks']['requirements_file_written_and_exported']
    assert not result['checks']['published_artifact_digest_matches']


def test_regular_file_reader_refuses_links_escapes_and_missing_files(tmp_path):
    root = tmp_path / 'run'; root.mkdir()
    (root / 'regular').write_bytes(b'valid')
    (root / 'linked').symlink_to(root / 'regular')
    reader = audit.safe_reader(root)
    assert reader('regular') == b'valid'
    for path in ('linked', '../regular', '/etc/passwd', 'missing'):
        assert reader(path) is None


@pytest.mark.parametrize('index', [0, 1])
def test_both_planned_member_handoffs_are_required_not_any_one_message(evidence, index):
    bundle, files = evidence
    bundle['messages'].pop(index)
    result = audit.evaluate(bundle, files.get)
    assert result['state'] == 'failed', 'One delivered message does not prove a complete requirements → code → review handoff'


@pytest.mark.parametrize('change', ['wrong-sender', 'wrong-reviewer', 'wrong-source-task', 'wrong-receiver-task', 'missing-plan', 'same-member'])
def test_message_direction_and_task_scope_follow_approved_plan(evidence, change):
    bundle, files = evidence
    if change == 'wrong-sender': bundle['messages'][0]['sender_id'] = 'lead'
    elif change == 'wrong-reviewer': bundle['run']['snapshot']['reviewer_id'] = 'lead'
    elif change == 'wrong-source-task': bundle['messages'][0]['task_key'] = 'plan'
    elif change == 'wrong-receiver-task':
        for event in bundle['events']:
            if event['id'] in (6, 7): event['data']['task_key'] = 'unrelated-task'
    elif change == 'missing-plan': bundle['run'].pop('plan')
    elif change == 'same-member': bundle['run']['plan']['tasks'][0]['agent_id'] = 'dev'
    assert audit.evaluate(bundle, files.get)['state'] == 'failed'


@pytest.mark.parametrize('placement', [0, 1])
def test_superseded_duplicate_does_not_discard_real_installed_approval(evidence, placement):
    bundle, files = evidence
    duplicate = deepcopy(bundle['installations'][0])
    duplicate.update(id='duplicate', status='superseded', reviewed_by='', result={'reused_installation_id': 'install'})
    bundle['installations'].insert(placement, duplicate)
    result = audit.evaluate(bundle, files.get)
    assert result['state'] == 'passed'
    assert result['evidence_counts']['verified_tool_installations'] == 1
    bundle['installations'] = [duplicate]
    assert not audit.evaluate(bundle, files.get)['checks']['approved_offline_tool_installed_and_used']


def metrics_history(bundle, files):
    """One successful write, one real metrics response, then an explanatory append."""
    original = files['tasks/develop/docs/requirements.md'].decode()
    metrics = next(event['data']['result'] for event in bundle['events'] if event['kind'] == 'tool.installed.invoked')
    prior_write = next(event for event in bundle['events'] if event['kind'] == 'tool.completed' and event['data']['result'].get('written') == audit.DOCUMENT)
    prior_write['data'].update(task_key='develop', result={'written': audit.DOCUMENT, 'bytes': len(original.encode())})
    appendix = '\n## Tool metrics\n' + json.dumps(metrics) + '\nMeasured before this metrics record was appended.\n'
    files['tasks/develop/docs/requirements.md'] += appendix.encode()
    bundle['tasks'] = [{'key': 'develop', 'messages': [
        {'role': 'assistant', 'calls': [{'id': 'write', 'name': 'write_file', 'arguments': {'path': audit.DOCUMENT, 'content': original}}]},
        {'role': 'tool', 'call_id': 'write', 'content': json.dumps({'written': audit.DOCUMENT, 'bytes': len(original.encode())})},
        {'role': 'assistant', 'calls': [{'id': 'metrics', 'name': 'run_installed_tool', 'arguments': {
            'installation_id': 'install', 'operation': 'text_metrics', 'path': audit.DOCUMENT}}]},
        {'role': 'tool', 'call_id': 'metrics', 'content': json.dumps({'installation_id': 'install', 'status': 'completed', 'result': metrics})},
        {'role': 'assistant', 'calls': [{'id': 'append', 'name': 'write_file', 'arguments': {'path': audit.DOCUMENT, 'content': original + appendix}}]},
        {'role': 'tool', 'call_id': 'append', 'content': json.dumps({'written': audit.DOCUMENT, 'bytes': len((original + appendix).encode())})},
    ]}]
    return original, metrics


def test_metrics_use_recorded_pre_append_file_not_only_latest_document(evidence):
    bundle, files = evidence
    metrics_history(bundle, files)
    result = audit.evaluate(bundle, files.get)
    assert result['checks']['approved_offline_tool_installed_and_used'], 'Metrics may describe the document before its metrics appendix'


@pytest.mark.parametrize('change', ['failed-write', 'later-write', 'wrong-call', 'wrong-installation', 'wrong-result', 'modified-prefix', 'unexplained-append', 'missing-history'])
def test_metrics_history_requires_successful_prior_exact_write_and_real_receipts(evidence, change):
    bundle, files = evidence
    original, _ = metrics_history(bundle, files)
    messages = bundle['tasks'][0]['messages']
    if change == 'failed-write': messages[1]['content'] = json.dumps({'error': 'Fixture write denied'})
    elif change == 'later-write': bundle['tasks'][0]['messages'] = messages[2:4] + messages[:2] + messages[4:]
    elif change == 'wrong-call': messages[1]['call_id'] = 'other-write'
    elif change == 'wrong-installation': messages[2]['calls'][0]['arguments']['installation_id'] = 'other-install'
    elif change == 'wrong-result': messages[3]['content'] = json.dumps({'installation_id': 'install', 'status': 'completed', 'result': {'characters': 999}})
    elif change == 'modified-prefix': files['tasks/develop/docs/requirements.md'] = b'TAMPERED\n' + files['tasks/develop/docs/requirements.md']
    elif change == 'unexplained-append': files['tasks/develop/docs/requirements.md'] = original.encode() + b'\nDifferent scope and unrelated new requirements.\n'
    elif change == 'missing-history': bundle.pop('tasks')
    assert not audit.evaluate(bundle, files.get)['checks']['approved_offline_tool_installed_and_used']


def test_bundle_loader_decodes_plan_and_task_message_evidence():
    with sqlite3.connect(':memory:') as connection:
        connection.execute('CREATE TABLE runs (id TEXT, plan TEXT)')
        connection.execute('CREATE TABLE tasks (id TEXT, run_id TEXT, key TEXT, messages TEXT)')
        plan = {'tasks': [{'key': 'requirements', 'agent_id': 'analyst'}]}
        history = [{'role': 'tool', 'call_id': 'fixture', 'content': '{}'}]
        connection.execute('INSERT INTO runs VALUES (?, ?)', ('a' * 32, json.dumps(plan)))
        connection.execute('INSERT INTO tasks VALUES (?, ?, ?, ?)', ('task', 'a' * 32, 'requirements', json.dumps(history)))
        result = audit.load_bundle(connection, 'a' * 32)
    assert result['run']['plan'] == plan
    assert result['tasks'][0]['messages'] == history
