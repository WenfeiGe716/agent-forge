"""Goal-driven discussion checkpoints. Success is never inferred from iteration count.

References prove provenance, not semantic truth. The assessor is a model and the
accepted checkpoint is only eligible for manual acceptance (or development planning).
"""
import hashlib
import json
from sqlalchemy import select
from .db import session, Task, Run, emit
from .schema import GoalReview, OrchestrationInput
from . import workspace as ws
from .engagement import effective_graph


def prepare_goal_review(data):
    from .engine import load, discussion_transcript, create_task, digest
    run = load(data['run_id'])
    graph = OrchestrationInput.model_validate(effective_graph(run)).model_dump()
    cycle = data['cycle']
    transcript = [t for t in discussion_transcript(run.id) if t['round'] <= cycle]
    evidence_digest = digest({'request': run.request, 'criteria': graph['criteria'], 'transcript': transcript})
    assessor = graph['assessor_id'] or graph['participants'][-1]
    key = f'goal_review_{cycle}'
    result = create_task(run.id, key, 'goal_review', assessor, f'目标证据复核 · 检查点 {cycle}', {
        'description': graph['prompt'], 'meeting_id':run.id+':meeting', 'cycle': cycle, 'criteria': graph['criteria'],
        'transcript': transcript, 'transcript_digest': evidence_digest,
        'previous_goal_checkpoint': data.get('checkpoint', {}),
        'evidence_rule': 'decision needs completed contribution references from at least 2 different participants; code_reference additionally needs an exact read_file path+sha256 receipt. Never infer tests passed from a source read.'})
    with session() as s:
        emit(s, run.id, 'orchestration.goal_review.prepared', {'task_key': key, 'agent_id': assessor, 'cycle': cycle, 'transcript_digest': evidence_digest})
    return {**result, 'agent_id': assessor, 'transcript_digest': evidence_digest}


def _read_receipts(task):
    """Return only tool-role results corresponding to server-executed read_file calls."""
    requested = {}
    receipts = set()
    for message in task.messages or []:
        if message.get('role') == 'assistant':
            for call in message.get('calls', []):
                if call.get('name') == 'read_file':
                    requested[call['id']] = call.get('arguments', {}).get('path')
        elif message.get('role') == 'tool' and message.get('call_id') in requested:
            try:
                output = json.loads(message['content'])
                path = requested[message['call_id']]
                if output.get('path') == path and output.get('sha256') and 'error' not in output:
                    # Content hash must match the actual returned text too.
                    if hashlib.sha256(output['content'].encode()).hexdigest() == output['sha256']:
                        receipts.add((path, output['sha256']))
            except (KeyError, ValueError, TypeError):
                continue
    return receipts


def assess_goal(data):
    from .engine import load, task_id, digest
    run = load(data['run_id'])
    cycle = data['cycle']
    graph = OrchestrationInput.model_validate(effective_graph(run)).model_dump()
    with session() as s:
        review_task = s.get(Task, task_id(run.id, f'goal_review_{cycle}'))
        if not review_task or review_task.status != 'done':
            raise ValueError('目标复核尚未完成')
        submission = GoalReview.model_validate(review_task.result).model_dump()
        transcript = review_task.spec['transcript']
        tasks = {t.key: t for t in s.scalars(select(Task).where(Task.run_id == run.id, Task.status == 'done'))}
    expected = digest({'request': run.request, 'criteria': graph['criteria'], 'transcript': transcript})
    invalid = []
    if submission['transcript_digest'] != expected:
        invalid.append('复核引用的输入摘要已过期或不匹配')
    ids = [a['criterion_id'] for a in submission['assessments']]
    if len(set(ids)) != len(ids) or set(ids) != {c['id'] for c in graph['criteria']}:
        invalid.append('目标验收项缺失、重复或包含未授权的新目标')
    by_id = {a['criterion_id']: a for a in submission['assessments']}
    allowed = {t['key'] for t in transcript} | {review_task.key}
    current = [t for t in transcript if t['round'] == cycle]
    blockers = sorted({str(x).strip()[:1000] for t in current for x in t.get('open_issues', []) if str(x).strip()} |
                      {str(x).strip()[:1000] for x in submission['open_issues'] if str(x).strip()})
    rows = []
    for criterion in graph['criteria']:
        claim = by_id.get(criterion['id'], {})
        errors = []
        authors = set()
        file_refs = []
        evidence = claim.get('evidence', [])
        for reference in evidence:
            task = tasks.get(reference['task_key'])
            if reference['task_key'] not in allowed or not task:
                errors.append('证据任务不属于当前已完成的讨论输入'); continue
            if task.kind == 'discussion':
                authors.add(task.agent_id)
            if reference['path']:
                pair = (reference['path'], reference['sha256'])
                if pair not in _read_receipts(task):
                    errors.append('文件证据没有对应的真实读取回执'); continue
                try:
                    path = ws.safe_path(ws.task_root(run.id, task.key), reference['path'])
                    actual = hashlib.sha256(path.read_bytes()).hexdigest()
                    if actual != reference['sha256']:
                        errors.append('文件内容与读取时的摘要不一致'); continue
                    file_refs.append({'task_key': task.key, 'path': reference['path'], 'sha256': actual})
                except (OSError, ValueError):
                    errors.append('证据文件已不可读')
        if claim.get('status') == 'met':
            if len(authors) < 2:
                errors.append('决策依据至少要关联两位参与者的实际发言')
            if criterion['evidence_type'] == 'code_reference' and not file_refs:
                errors.append('该验收项要求可核验的源文件读取证据')
        status = claim.get('status', 'unmet')
        if errors or invalid:
            status = 'unmet'
        rows.append({**criterion, 'status': status, 'rationale': claim.get('rationale', ''),
                     'evidence': evidence, 'file_receipts': file_refs, 'errors': errors})
    met = [r['id'] for r in rows if r['status'] == 'met']
    progress_key = digest({'met': sorted(met), 'files': sorted({(f['path'], f['sha256']) for r in rows for f in r['file_receipts']})})
    previous = data.get('checkpoint') or {}
    # Only criterion/evidence state counts as progress; rephrased prose cannot reset the counter.
    seen = list(previous.get('seen_progress', []))
    best = previous.get('best_met_count', -1)
    progressed = len(met) > best or (progress_key not in seen and len(met) >= best)
    stale = 0 if progressed else previous.get('stagnation_count', 0) + 1
    if progress_key not in seen:
        seen.append(progress_key)
    if invalid:
        blockers.extend(invalid)
    outcome = 'continue'
    if submission['needs_human']:
        outcome = 'needs_human'
    elif not blockers and not invalid and all(r['status'] == 'met' for r in rows):
        outcome = 'goal_satisfied'
    elif stale >= graph['stagnation_checks']:
        outcome = 'stalled'
    checkpoint = {'cycle': cycle, 'outcome': outcome, 'summary': submission['summary'],
                  'transcript_digest': expected, 'assessor_id': review_task.agent_id,
                  'criteria': rows, 'open_issues': blockers, 'met_count': len(met),
                  'total_count': len(rows), 'stagnation_count': stale,
                  'best_met_count': max(best, len(met)), 'seen_progress': seen[-100:],
                  'semantic_verification': 'model_assessed; human acceptance required',
                  'code_tests': 'not_executed_in_read_only_discussion'}
    with session() as s:
        row = s.get(Run, run.id)
        row.report = {**(row.report or {}), 'goal_checkpoint': checkpoint}
        emit(s, run.id, 'orchestration.goal.assessed', {'cycle': cycle, 'outcome': outcome,
             'met_count': len(met), 'total_count': len(rows), 'stagnation_count': stale,
             'open_issues': blockers, 'task_key': review_task.key, 'agent_id': review_task.agent_id,
             'transcript_digest': expected, 'summary': submission['summary']})
    return checkpoint


def checkpoint_interrupted(data):
    """Preserve a readable partial transcript; never set accepted/completed on a limit."""
    from .engine import load, discussion_transcript, finish_discussion, publish_discussion
    run = load(data['run_id'])
    turns = discussion_transcript(run.id)
    if not turns:
        return {'saved': False}
    checkpoint = (run.report or {}).get('goal_checkpoint', {})
    checkpoint = {**checkpoint, 'outcome': data['reason'], 'interruption': data.get('error', '')}
    discussion = finish_discussion({'run_id': run.id, 'rounds_completed': max(t['round'] for t in turns),
                                    'stop_reason': data['reason'], 'checkpoint': checkpoint})
    artifact = publish_discussion({'run_id': run.id, 'discussion': discussion})
    return {'saved': True, **artifact}
