"""Regression for live run 456d7f: real scheduler, SQLite and Git workspaces.

No model or native command is replaced or called. Completed/waiting activity
records seed the durable scheduling boundary captured in the real run.
"""
import pytest

from forge import autonomy, engine, workspace as ws
from forge.collaboration import send_message
from forge.db import Step, Task, session
from test_autonomous_execution import finish, initial, new_run


def assign(rid, key, recipient, operation):
    return autonomy.assign_work(rid, key, operation, {
        'recipient_id': recipient, 'title': '整理交付文档',
        'instruction': '维护 docs/verification.md，并核对真实交付。',
        'acceptance': '文档正确集成，不覆盖其他成员的变更。',
    })['task_key']


def tick(rid, index, answered=()):
    return autonomy.autonomy_tick({'run_id': rid, 'tick': index, 'answered': list(answered)})


def test_approved_activation_keeps_member_until_integrated_before_next_message(client, configured):
    """An answer resumes its existing work, not a new same-member request."""
    rid, lead = new_run(client, configured)
    lead_key = initial(rid)
    worker = next(a['id'] for a in configured['agents'] if a['role'] == 'tester')
    old_key = assign(rid, lead_key, worker, 'original-docs')
    finish(rid, lead_key, 'waiting')
    assert tick(rid, 1)['runnable'] == [{'key': old_key, 'agent_id': worker}]
    root = ws.task_root(rid, old_key)
    ws.write_workspace_file(root, 'docs/verification.md', b'Original work before approval\n', 10000)
    with session() as s:
        old = s.get(Task, rid + ':' + old_key)
        old.status, old.started_at = 'waiting_decision', '2026-09-11T00:00:00+00:00'
        s.add(Step(id=old.id + ':turn:0', state='completed', result={'blocked_on': 'original-permission'}))

    # As in the live trace, the coordinator independently finishes a message
    # asking the same member to continue while its earlier approval is pending.
    sender = 'coordinator_followup'
    engine.create_task(rid, sender, 'autonomous', lead, '核对工作进展', {'coordinator': True})
    new_key = send_message(rid, sender, 'continue-docs', worker, '请继续核对文档，必要时更新。', True)['wake_task_key']
    finish(rid, sender, 'waiting')
    state = tick(rid, 2)
    assert state['runnable'] == []
    assert state['blocked'] == [{'key': old_key, 'request_id': 'original-permission'}]

    state = tick(rid, 3, ['original-permission'])
    assert state['runnable'] == [{'key': old_key, 'agent_id': worker}]
    assert not ws.task_root(rid, new_key).exists()
    # Another observation must still not activate/copy the next workspace.
    assert tick(rid, 4, ['original-permission'])['runnable'] == state['runnable']
    assert ws.read_workspace_file(root, 'docs/verification.md', 10000) == b'Original work before approval\n'
    finish(rid, old_key, deliverables=['docs/verification.md'])
    state = tick(rid, 5, ['original-permission'])
    assert {'key': new_key, 'agent_id': worker} in state['runnable']
    new_root = ws.task_root(rid, new_key)
    assert ws.read_workspace_file(new_root, 'docs/verification.md', 10000) == b'Original work before approval\n'
    ws.write_workspace_file(new_root, 'docs/verification.md', b'Revised after reading original work\n', 10000)
    finish(rid, new_key, deliverables=['docs/verification.md'])
    patch = (ws.run_root(rid) / 'artifacts' / (new_key + '.patch')).read_text()
    assert 'new file mode' not in patch and '--- a/docs/verification.md' in patch
    assert ws.read_workspace_file(ws.run_root(rid) / 'integration', 'docs/verification.md', 10000) == b'Revised after reading original work\n'


def test_parallel_budget_applies_before_copying_unselected_workspaces(client, configured):
    rid, _ = new_run(client, configured, runtime={'max_parallel': 1})
    lead_key = initial(rid)
    workers = [a['id'] for a in configured['agents'] if a['role'] in ('developer', 'tester')]
    keys = [assign(rid, lead_key, worker, 'docs-' + worker) for worker in workers]
    finish(rid, lead_key, 'waiting')
    state = tick(rid, 1)
    assert len(state['runnable']) == 1
    selected = state['runnable'][0]['key']
    deferred = next(key for key in keys if key != selected)
    assert not ws.task_root(rid, deferred).exists()
    ws.write_workspace_file(ws.task_root(rid, selected), 'docs/input.md', b'Published after first dispatch\n', 10000)
    finish(rid, selected, 'waiting', deliverables=['docs/input.md'])
    assert tick(rid, 2)['runnable'][0]['key'] == deferred
    assert ws.read_workspace_file(ws.task_root(rid, deferred), 'docs/input.md', 10000) == b'Published after first dispatch\n'


def test_unprepared_other_member_gets_turn_without_overwriting_active_work(client, configured):
    rid, _ = new_run(client, configured, runtime={'max_parallel': 1})
    lead_key = initial(rid)
    workers = [a['id'] for a in configured['agents'] if a['role'] in ('developer', 'tester')]
    keys = [assign(rid, lead_key, worker, 'fair-' + worker) for worker in workers]
    finish(rid, lead_key, 'waiting')
    selected = tick(rid, 1)['runnable'][0]['key']
    with session() as s:
        task = s.get(Task, rid + ':' + selected)
        task.status, task.started_at = 'running', '2026-09-11T00:00:00+00:00'
    ws.write_workspace_file(ws.task_root(rid, selected), 'docs/in-progress.md', b'Unintegrated personal edits\n', 10000)
    deferred = next(key for key in keys if key != selected)
    assert tick(rid, 2)['runnable'][0]['key'] == deferred
    assert ws.read_workspace_file(ws.task_root(rid, selected), 'docs/in-progress.md', 10000) == b'Unintegrated personal edits\n'
    with pytest.raises(ValueError):
        ws.read_workspace_file(ws.task_root(rid, deferred), 'docs/in-progress.md', 10000)


def test_different_members_still_run_in_parallel_and_true_conflicts_preserve_patch(client, configured):
    rid, _ = new_run(client, configured, runtime={'max_parallel': 2})
    lead_key = initial(rid)
    workers = [a['id'] for a in configured['agents'] if a['role'] in ('developer', 'tester')]
    keys = [assign(rid, lead_key, worker, 'parallel-' + worker) for worker in workers]
    finish(rid, lead_key, 'waiting')
    state = tick(rid, 1)
    assert {item['key'] for item in state['runnable']} == set(keys)
    assert len({item['agent_id'] for item in state['runnable']}) == 2
    for key, content in zip(keys, (b'First genuine edit\n', b'Competing genuine edit\n')):
        ws.write_workspace_file(ws.task_root(rid, key), 'docs/shared.md', content, 10000)
    finish(rid, keys[0], 'waiting', deliverables=['docs/shared.md'])
    with pytest.raises(ValueError, match='冲突'):
        with session() as s: base = s.get(Task, rid + ':' + keys[1]).base_commit
        ws.integrate(rid, keys[1], base)
    assert ws.read_workspace_file(ws.run_root(rid) / 'integration', 'docs/shared.md', 10000) == b'First genuine edit\n'
    assert (ws.run_root(rid) / 'artifacts' / (keys[1] + '.patch')).exists()
