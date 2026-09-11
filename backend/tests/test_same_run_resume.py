"""A failed autonomous run can be resumed without creating a second run."""
from forge.db import Run, Task, session

from conftest import run_body


def test_needs_attention_resume_reopens_the_same_run(client, configured):
    created = client.post('/api/runs', json=run_body(configured)).json()
    run_id = created['id']
    with session() as s:
        run = s.get(Run, run_id)
        run.status = 'needs_attention'
        run.error = '成员执行失败'
        leader = run.snapshot['entry_agent_id']
        s.add(Task(id=f'{run_id}:interrupted', run_id=run_id, key='interrupted',
                   kind='autonomous', agent_id=leader, title='中断的成员工作',
                   status='needs_attention', spec={}, result={}))

    response = client.post(f'/api/runs/{run_id}/control', json={'action': 'resume'})
    assert response.status_code == 202, response.text
    detail = client.get(f'/api/runs/{run_id}').json()
    assert detail['id'] == run_id
    assert detail['status'] == 'queued'
    with session() as s:
        task = s.get(Task, f'{run_id}:interrupted')
        assert task.status == 'failed' and task.result['outcome'] == 'blocked'
    assert any(command['action'] == 'control' and command['payload']['action'] == 'resume'
               for command in detail['commands'])
    assert any(event['kind'] == 'run.resumed' for event in client.get(f'/api/runs/{run_id}/events').json()['events'])
