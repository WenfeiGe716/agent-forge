"""Real Git rejection and bounded coordinator recovery; no model is called."""
import pytest

from forge import autonomy, engine, workspace as ws
from forge.collaboration import send_message
from forge.db import Event, Run, Step, Task, session
from sqlalchemy import select
from test_autonomous_execution import finish, initial, new_run
from test_autonomy_activation_order import assign, tick


def conflicting_work(client, configured, contents=(b'First contribution\n', b'Second contribution\n'), conflict_outcome='done', followup=False):
    rid, lead = new_run(client, configured, runtime={'max_parallel': 2})
    leader_key = initial(rid)
    workers = [a['id'] for a in configured['agents'] if a['role'] in ('developer', 'tester')]
    keys = [assign(rid, leader_key, worker, 'conflict-' + worker) for worker in workers]
    finish(rid, leader_key, 'waiting')
    assert len(tick(rid, 1)['runnable']) == 2
    for key, content in zip(keys, contents):
        ws.write_workspace_file(ws.task_root(rid, key), 'docs/shared.md', content, 100000)
    finish(rid, keys[0], deliverables=['docs/shared.md'])
    child = None
    if followup:
        reviewer = next(a['id'] for a in configured['agents'] if a['role'] == 'reviewer')
        child = send_message(rid, keys[1], 'after-my-integration', reviewer, '请在我的成果集成后核对共享文档。', True)['wake_task_key']
    finish(rid, keys[1], conflict_outcome, deliverables=['docs/shared.md'])
    if child: keys.append(child)
    return rid, lead, workers, keys


@pytest.mark.parametrize('conflict_outcome,followup', [('done', False), ('waiting', False), ('waiting', True)])
def test_conflict_goes_to_coordinator_and_requires_read_and_integrated_repair(client, configured, conflict_outcome, followup):
    rid, lead, workers, keys = conflicting_work(client, configured, conflict_outcome=conflict_outcome, followup=followup)
    from forge import work_issues
    issue_key = keys[1]
    state = tick(rid, 2)
    leader_key = next(r['key'] for r in state['runnable'] if r['agent_id'] == lead)
    context = autonomy.task_context(engine.load(rid), next_task(rid, leader_key))
    assert context['work_issues'][0]['kind'] == 'integration_conflict'
    assert context['work_issues'][0]['files'] == ['docs/shared.md']
    with pytest.raises(ValueError, match='问题'):
        finish(rid, leader_key, 'ready', deliverables=['docs/shared.md'])
    review = work_issues.read_work_issue(rid, leader_key, {'task_key': issue_key})
    assert 'Second contribution' in review['patch'] and review['patch_complete']
    files = work_issues.read_work_issue(rid, leader_key, {'task_key': issue_key, 'path': 'docs/shared.md'})
    assert files['current']['content'] == 'First contribution\n'
    assert files['attempted']['content'] == 'Second contribution\n'
    repair = assign(rid, leader_key, workers[1], 'repair-conflict')
    finish(rid, leader_key, 'waiting')
    assert {'key': repair, 'agent_id': workers[1]} in tick(rid, 3)['runnable']
    ws.write_workspace_file(ws.task_root(rid, repair), 'docs/shared.md', b'First contribution\nSecond contribution\n', 10000)
    finish(rid, repair, deliverables=['docs/shared.md'])
    state = tick(rid, 4)
    leader_key = next(r['key'] for r in state['runnable'] if r['agent_id'] == lead)
    arguments = {'task_key': issue_key, 'evidence_task_keys': [repair],
                 'resolution': '已核对两份原始贡献并在新修复任务中保留双方内容，重新读取集成结果确认。'}
    with pytest.raises(ValueError, match='读取'):
        work_issues.resolve_work_issue(rid, leader_key, 'resolve-before-read', arguments)
    work_issues.read_work_issue(rid, leader_key, {'task_key': issue_key})
    work_issues.read_work_issue(rid, leader_key, {'task_key': issue_key, 'path': 'docs/shared.md'})
    assert work_issues.resolve_work_issue(rid, leader_key, 'resolve', arguments)['status'] == 'resolved'
    final_tick = 5
    if followup:
        finish(rid, leader_key, 'waiting')
        assert keys[2] in [r['key'] for r in tick(rid, 5)['runnable']]
        assert ws.read_workspace_file(ws.task_root(rid, keys[2]), 'docs/shared.md', 10000) == b'First contribution\nSecond contribution\n'
        finish(rid, keys[2], notes='已依据新的集成文档核对双方贡献均保留，没有扩大原任务授权。')
        replies = tick(rid, 6)['runnable']
        leader_key = next(r['key'] for r in replies if r['agent_id'] == lead)
        # The reviewer also replies to the original requester; do not bypass
        # that legitimate final activation just because the merge is repaired.
        with pytest.raises(ValueError, match='未完成'):
            finish(rid, leader_key, 'ready', deliverables=['docs/shared.md'])
        for item in replies:
            if item['agent_id'] != lead:
                finish(rid, item['key'], notes='已收到同伴对修复后文档的核对结果，原协作请求已完成，没有后续待处理工作。')
        assert {'key': leader_key, 'agent_id': lead} in tick(rid, 7)['runnable']
        final_tick = 8
    finish(rid, leader_key, 'ready', deliverables=['docs/shared.md'], evidence_task_keys=[repair])
    assert tick(rid, final_tick).get('candidate')
    assert next_task(rid, issue_key).status == 'superseded'
    assert (ws.run_root(rid) / 'artifacts' / (issue_key + '.patch')).exists()
    assert ws.read_workspace_file(ws.task_root(rid, issue_key), 'docs/shared.md', 10000) == b'Second contribution\n'


def next_task(rid, key):
    with session() as s:
        return s.get(Task, rid + ':' + key)


def test_unresolved_coordinator_blocker_is_bounded_then_escalates(client, configured):
    rid, lead = new_run(client, configured)
    key = initial(rid)
    finish(rid, key, 'blocked', summary='输入条件不足，尚未找到当前权限范围内的解决方式。')
    for attempt in range(1, 4):
        state = tick(rid, attempt)
        assert 'stop' not in state
        key = state['runnable'][0]['key']
        assert state['runnable'][0]['agent_id'] == lead
        issues = autonomy.task_context(engine.load(rid), next_task(rid, key))['work_issues']
        assert len(issues) == 1 and issues[0]['attempts'] == attempt
        finish(rid, key, 'blocked', summary='已核对当前资料，仍无法在现有约束内提供可验证结果。')
    stopped = tick(rid, 4)
    assert stopped['runnable'] == [] and '3 次' in stopped['stop']['reason']
    with session() as s:
        events = list(s.scalars(select(Event).where(Event.run_id == rid, Event.kind == 'team.issue.escalated')))
        assert len(events) == 1 and events[0].data['status'] == 'escalated'
        assert events[0].data['escalation_reason']
    assert engine.load(rid).calls == 0  # No model, permission or command was silently replayed.


def test_issue_reader_respects_run_read_permission_and_communication(client, configured):
    from forge import work_issues
    import json
    rid, lead, _, keys = conflicting_work(client, configured)
    leader_key = tick(rid, 2)['runnable'][0]['key']
    other_run, _ = new_run(client, configured)
    other_key = initial(other_run)
    with pytest.raises(ValueError, match='本次运行'):
        work_issues.read_work_issue(other_run, other_key, {'task_key': keys[1]})
    with pytest.raises(ValueError, match='相对文件'):
        work_issues.read_work_issue(rid, leader_key, {'task_key': keys[1], 'path': '../forge.db'})
    with session() as s:
        run = s.get(Run, rid)
        snapshot = json.loads(json.dumps(run.snapshot))
        snapshot['agents'][lead]['tools'].remove('read_file')
        run.snapshot = snapshot
    with pytest.raises(ValueError, match='读取权限'):
        work_issues.read_work_issue(rid, leader_key, {'task_key': keys[1]})
    with session() as s:
        run = s.get(Run, rid)
        snapshot = json.loads(json.dumps(run.snapshot))
        snapshot['agents'][lead]['tools'].append('read_file')
        sender = next_task(rid, keys[1]).agent_id
        snapshot['agents'][sender]['communication'] = {'scope': 'custom', 'recipients': []}
        run.snapshot = snapshot
    with pytest.raises(ValueError, match='通信'):
        work_issues.read_work_issue(rid, leader_key, {'task_key': keys[1]})


def test_original_patch_mutation_is_not_silently_accepted(client, configured):
    from forge import work_issues
    rid, _, _, keys = conflicting_work(client, configured)
    leader_key = tick(rid, 2)['runnable'][0]['key']
    artifact = ws.run_root(rid) / 'artifacts' / (keys[1] + '.patch')
    artifact.write_bytes(artifact.read_bytes() + b'\nchanged after rejection\n')
    with pytest.raises(ValueError, match='补丁已变化'):
        work_issues.read_work_issue(rid, leader_key, {'task_key': keys[1]})


def test_partial_patch_read_cannot_close_issue(client, configured):
    from forge import work_issues
    rid, _, workers, keys = conflicting_work(client, configured, (b'First\n', b'Second contribution\n' * 1400))
    leader_key = tick(rid, 2)['runnable'][0]['key']
    repair = assign(rid, leader_key, workers[1], 'fix-long-patch')
    finish(rid, leader_key, 'waiting')
    tick(rid, 3)
    ws.write_workspace_file(ws.task_root(rid, repair), 'docs/shared.md', b'Both contributions reviewed\n', 10000)
    finish(rid, repair, deliverables=['docs/shared.md'])
    leader_key = tick(rid, 4)['runnable'][0]['key']
    first = work_issues.read_work_issue(rid, leader_key, {'task_key': keys[1]})
    assert not first['patch_complete'] and len(first['patch'].encode()) <= 16000
    work_issues.read_work_issue(rid, leader_key, {'task_key': keys[1], 'path': 'docs/shared.md'})
    args = {'task_key': keys[1], 'evidence_task_keys': [repair], 'resolution': '已检查两个版本并在当前集成版本生成修复文件，依据实际文件核验。'}
    with pytest.raises(ValueError, match='完整读取'):
        work_issues.resolve_work_issue(rid, leader_key, 'too-early', args)
    page = first
    while not page['patch_complete']:
        page = work_issues.read_work_issue(rid, leader_key, {'task_key': keys[1], 'offset': page['next_offset']})
    assert work_issues.resolve_work_issue(rid, leader_key, 'after-pages', args)['status'] == 'resolved'


def test_post_apply_commit_failure_is_not_auto_recovered(client, configured):
    rid, _ = new_run(client, configured)
    key = initial(rid)
    ws.write_workspace_file(ws.task_root(rid, key), 'docs/real.md', b'Real staged change\n', 10000)
    integration = ws.run_root(rid) / 'integration'
    branch = ws.git(integration, 'symbolic-ref', '--short', 'HEAD').stdout.decode().strip()
    lock = integration / '.git/refs/heads' / (branch + '.lock')
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b'')
    with pytest.raises(ValueError):
        finish(rid, key, deliverables=['docs/real.md'])
    assert not next_task(rid, key).spec.get('work_issue')
    assert ws.read_workspace_file(integration, 'docs/real.md', 10000) == b'Real staged change\n'


def test_member_blocked_can_be_resolved_by_actual_new_work(client, configured):
    from forge import work_issues
    rid, lead = new_run(client, configured)
    key = initial(rid)
    worker = next(a['id'] for a in configured['agents'] if a['role'] == 'developer')
    blocked = assign(rid, key, worker, 'missing-document')
    finish(rid, key, 'waiting')
    tick(rid, 1)
    finish(rid, blocked, 'blocked', summary='尚未形成资料文档，请负责人协调已有信息整理。')
    leader_key = tick(rid, 2)['runnable'][0]['key']
    assert work_issues.read_work_issue(rid, leader_key, {'task_key': blocked})['kind'] == 'member_blocked'
    repair = assign(rid, leader_key, worker, 'produce-document')
    finish(rid, leader_key, 'waiting')
    tick(rid, 3)
    ws.write_workspace_file(ws.task_root(rid, repair), 'docs/source.md', b'Actual in-scope source material\n', 10000)
    finish(rid, repair, deliverables=['docs/source.md'])
    leader_key = tick(rid, 4)['runnable'][0]['key']
    work_issues.read_work_issue(rid, leader_key, {'task_key': blocked})
    result = work_issues.resolve_work_issue(rid, leader_key, 'close-member-blocker', {
        'task_key': blocked, 'evidence_task_keys': [repair],
        'resolution': '已由原团队成员整理已有范围内的资料，实际文件已集成并核对，不需要扩大权限。'})
    assert result['status'] == 'resolved'
    assert next_task(rid, blocked).result['outcome'] == 'blocked'  # Original history stays truthful.
    finish(rid, leader_key, 'ready', deliverables=['docs/source.md'], evidence_task_keys=[repair])
    assert tick(rid, 5).get('candidate')


def test_recovery_still_waits_for_explicit_permission_without_new_activation(client, configured):
    rid, lead = new_run(client, configured)
    original = initial(rid)
    finish(rid, original, 'blocked', summary='需要负责人核对现有权限与可行替代方案。')
    leader_key = tick(rid, 1)['runnable'][0]['key']
    with session() as s:
        task = s.get(Task, rid + ':' + leader_key)
        task.status, task.started_at = 'waiting_decision', '2026-09-11T00:00:00+00:00'
        s.add(Step(id=task.id + ':turn:0', state='completed', result={'blocked_on': 'explicit-human-permission'}))
    state = tick(rid, 2)
    assert state['runnable'] == [] and state['blocked'] == [{'key': leader_key, 'request_id': 'explicit-human-permission'}]
    assert tick(rid, 3)['blocked'] == state['blocked']
    assert tick(rid, 4, ['explicit-human-permission'])['runnable'] == [{'key': leader_key, 'agent_id': lead}]
    with session() as s:
        assert len(list(s.scalars(select(Task).where(Task.run_id == rid)))) == 2
