"""Prompt/context contract tests; live model behavior is validated separately."""
import json

from forge import engine, tools
from forge.db import Run, Task, session
from test_autonomous_execution import initial, new_run


def test_current_goal_and_project_constraints_are_distinct_and_conflicts_require_clarification(client, configured):
    rid, _ = new_run(client, configured, request='开发一个页面小游戏')
    key = initial(rid)
    with session() as s:
        run, task = s.get(Run, rid), s.get(Task, rid + ':' + key)
        snapshot = json.loads(json.dumps(run.snapshot))
        snapshot['project']['instructions'] = '仅限标签规范化测试，不得新增游戏。'
        run.snapshot = snapshot
        messages = engine.initial_messages(run, task, snapshot['agents'][task.agent_id])
    context = json.loads(messages[1]['content'])
    intent = context['task_intent']
    assert intent['current_request'] == '开发一个页面小游戏'
    assert intent['project_constraints'] == '仅限标签规范化测试，不得新增游戏。'
    assert intent['conflict_action'] == 'request_decision_before_work'
    assert intent['repository_role'] == 'context_not_replacement_goal'
    assert 'Do not silently replace the current request' in messages[0]['content']
    assert 'A clarification answer does not edit frozen permissions' in messages[0]['content']


def test_native_permission_guidance_does_not_ask_to_reauthorize_the_task_directory():
    description = tools.DEFINITIONS['request_execution_permission']['description']
    assert 'paths=[]' in description
    assert 'task workspace' in description
    assert 'absolute external regular file' in description
