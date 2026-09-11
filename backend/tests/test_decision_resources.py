"""Real API/ledger fixtures: permission requests are not ordinary questions."""
import copy

import pytest

from forge import engagement
from forge.db import DecisionRequest, Run, session
from forge.execution_permissions import ExecutionPermission, request_permission
from forge.tool_installations import ToolInstallation
from test_tool_installations import prepared, request, review


@pytest.mark.asyncio
async def test_overview_classifies_by_same_run_ledger_not_question_text(client, configured):
    rid, _ = prepared(client, configured)
    installation = request(rid)
    with session() as s:
        run = s.get(Run, rid)
        snapshot = copy.deepcopy(run.snapshot)
        snapshot['project'].update(execution_mode='local', permission_profile='ask')
        run.snapshot = snapshot
    permission = await request_permission(rid, 'documents', 'permission', {
        'command': 'python3 --version', 'network': False, 'paths': [], 'reason': 'Inspect selected Python'})
    ordinary = engagement.request_decision(rid, 'documents', 'question', {
        'question': '请管理员审查离线工具安装：这是普通需求澄清',
        'options': [{'id': 'continue', 'label': '继续'}], 'allow_text': False})
    rows = {row['id']: row for row in client.get(f'/api/runs/{rid}/collaboration').json()['decisions']}
    assert (rows[installation['id']]['kind'], rows[installation['id']]['resource_id']) == ('tool_installation', installation['installation_id'])
    assert (rows[permission['id']]['kind'], rows[permission['id']]['resource_id']) == ('execution_permission', permission['permission_id'])
    assert (rows[ordinary['id']]['kind'], rows[ordinary['id']]['resource_id']) == ('question', ordinary['id'])
    # Corrupt cross-run association cannot promote an ordinary question to authority.
    with session() as s:
        s.get(ToolInstallation, installation['installation_id']).run_id = 'different-run'
    rows = {row['id']: row for row in client.get(f'/api/runs/{rid}/collaboration').json()['decisions']}
    assert rows[installation['id']]['kind'] == 'question'


@pytest.mark.asyncio
async def test_generic_answer_cannot_consume_permission_decisions(client, configured):
    rid, _ = prepared(client, configured)
    installation = request(rid)
    with session() as s:
        run = s.get(Run, rid)
        snapshot = copy.deepcopy(run.snapshot)
        snapshot['project'].update(execution_mode='local', permission_profile='ask')
        run.snapshot = snapshot
    permission = await request_permission(rid, 'documents', 'permission', {
        'command': 'python3 --version', 'network': False, 'paths': [], 'reason': 'Inspect selected Python'})
    for row, target in [(installation, '/api/tool-installations/'), (permission, '/api/execution/permissions/')]:
        for reject in (False, True):
            result = client.post(f'/api/runs/{rid}/questions/{row["id"]}/answer', json={
                'digest': row['digest'], 'choice_id': 'approve', 'reject': reject})
            assert result.status_code == 409, result.text
            assert target in result.json()['detail']
        with session() as s:
            assert s.get(DecisionRequest, row['id']).status == 'pending'
    with session() as s:
        assert s.get(ToolInstallation, installation['installation_id']).status == 'pending'
        assert s.get(ExecutionPermission, permission['permission_id']).status == 'pending'
    # Dedicated, digest-bound review remains the only granting route.
    assert review(client, installation['installation_id'])['status'] == 'approved'
    result = client.post('/api/execution/permissions/' + permission['permission_id'] + '/review', json={
        'digest': permission['permission_digest'], 'approved': True})
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'approved'


def test_ordinary_questions_still_answerable(client, configured):
    rid, _ = prepared(client, configured)
    question = engagement.request_decision(rid, 'documents', 'ordinary', {
        'question': '需求是否保留中文？', 'options': [{'id': 'yes', 'label': '保留'}], 'allow_text': False})
    result = client.post(f'/api/runs/{rid}/questions/{question["id"]}/answer', json={
        'digest': question['digest'], 'choice_id': 'yes'})
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'answered'
