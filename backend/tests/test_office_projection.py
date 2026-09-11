import json
from sqlalchemy import select,func
from fastapi.testclient import TestClient
from forge.api import app
from forge.db import session,Run,Task,Event,Agent,emit,now
from conftest import run_body


def create_run(client,configured,**kw):
    r=client.post('/api/runs',json=run_body(configured,**kw));assert r.status_code==202,r.text
    return r.json()['id']


def member(client,aid):
    r=client.get('/api/office/state');assert r.status_code==200,r.text
    return next(p for p in r.json()['presence'] if p['agent_id']==aid)


def set_task(rid,aid,state='running',key='code'):
    with session() as s:
        r=s.get(Run,rid);r.status='running'
        t=Task(id=rid+':'+key,run_id=rid,key=key,kind='development',agent_id=aid,title='Code',spec={},status=state)
        s.add(t);s.flush()
        emit(s,rid,'tool.started',{'agent_id':aid,'task_key':key,'tool':'write_file'})


def test_office_requires_login(client):
    c=TestClient(app);assert c.get('/api/office/state').status_code==401


def test_empty_team_has_complete_idle_projection(client):
    data=client.get('/api/office/state').json()
    assert data['complete'] is True and data['read_only_projection'] is True
    assert len(data['presence'])==4 and all(not p['busy'] for p in data['presence'])
    assert data['events']==[]


def test_queued_agents_reserved_not_idle(client,configured):
    create_run(client,configured)
    assert all(p['busy'] and p['phase']=='waiting' for p in client.get('/api/office/state').json()['presence'])


def test_tool_event_controls_phase_and_evidence(client,configured):
    rid=create_run(client,configured);aid=configured['agents'][1]['id'];set_task(rid,aid)
    p=member(client,aid);assert p['phase']=='writing' and p['tool']=='write_file' and len(p['evidence_ids'])==1
    with session() as s:emit(s,rid,'tool.completed',{'agent_id':aid,'task_key':'code','tool':'write_file'})
    assert member(client,aid)['phase']=='thinking'


def test_finished_subtask_remains_reserved_until_run_released(client,configured):
    rid=create_run(client,configured);aid=configured['agents'][1]['id'];set_task(rid,aid)
    with session() as s:s.get(Task,rid+':code').status='done'
    p=member(client,aid);assert p['busy'] and p['phase']=='waiting'


def test_pause_approval_attention_never_idle(client,configured):
    rid=create_run(client,configured);aid=configured['agents'][0]['id']
    for status,phase in [('paused','paused'),('awaiting_approval','approval'),('awaiting_acceptance','approval'),('needs_attention','attention')]:
        with session() as s:s.get(Run,rid).status=status
        p=member(client,aid);assert p['phase']==phase and p['busy']


def test_completed_and_cancelled_release(client,configured):
    rid=create_run(client,configured);aid=configured['agents'][0]['id']
    for status in ['completed','cancelled']:
        with session() as s:r=s.get(Run,rid);r.status=status;r.updated_at=now()
        p=member(client,aid);assert not p['busy'] and p['idle_since']


def test_other_run_holds_agent_even_when_selected_run_finished(client,configured):
    first=create_run(client,configured,title='Finished');second=create_run(client,configured,title='Still working')
    aid=configured['agents'][1]['id'];set_task(second,aid)
    with session() as s:s.get(Run,first).status='completed'
    p=member(client,aid);assert p['busy'] and p['run_id']==second and p['phase']=='writing'


def test_active_tool_takes_precedence_over_other_paused_run(client,configured):
    first=create_run(client,configured,title='Paused');second=create_run(client,configured,title='Active')
    aid=configured['agents'][1]['id'];set_task(second,aid)
    with session() as s:s.get(Run,first).status='paused'
    p=member(client,aid);assert p['phase']=='writing' and p['run_count']==2


def test_old_runs_not_truncated_by_frontend_run_page(client,configured):
    busy=create_run(client,configured);aid=configured['agents'][1]['id'];set_task(busy,aid)
    with session() as s:
        base=s.get(Run,busy)
        for i in range(110):s.add(Run(id=f'finished-{i}',project_id=base.project_id,owner_id=base.owner_id,title='finished',request='x',snapshot=base.snapshot,status='completed'))
    assert member(client,aid)['run_id']==busy


def test_event_cursor_no_duplicate_and_bootstrap_does_not_replay_history(client,configured):
    rid=create_run(client,configured);aid=configured['agents'][1]['id'];set_task(rid,aid)
    first=client.get('/api/office/state').json();assert first['events']==[]
    with session() as s:emit(s,rid,'agent.message',{'from_agent_id':aid,'to_agent_id':configured['agents'][2]['id'],'body':'Actual text','message_id':'m'})
    newer=client.get('/api/office/state',params={'after':first['cursor']}).json()
    assert len(newer['events'])==1 and newer['events'][0]['data']['body']=='Actual text'
    assert client.get('/api/office/state',params={'after':newer['cursor']}).json()['events']==[]


def test_event_pagination_is_explicit(client,configured):
    rid=create_run(client,configured);aid=configured['agents'][1]['id']
    with session() as s:
        for i in range(8):emit(s,rid,'agent.started',{'agent_id':aid,'step':i})
    data=client.get('/api/office/state?after=0&limit=2').json();assert len(data['events'])==2 and data['has_more']


def test_hidden_project_only_returns_coarse_reservation(client,configured):
    rid=create_run(client,configured);aid=configured['agents'][1]['id'];set_task(rid,aid)
    with session() as s:emit(s,rid,'agent.message',{'from_agent_id':aid,'body':'PRIVATE-BODY'})
    u=client.post('/api/users',json={'username':'viewer','password':'viewer-long-password-123','role':'viewer'});assert u.status_code==201,u.text
    with TestClient(app,headers={'X-Forge-Request':'1'}) as viewer:
        r=viewer.post('/api/auth/login',json={'username':'viewer','password':'viewer-long-password-123'});assert r.status_code==200
        data=viewer.get('/api/office/state?after=0').json();text=json.dumps(data)
        assert rid not in text and 'PRIVATE-BODY' not in text
        assert data['events']==[]
        for p in data['presence']:assert p['phase']=='reserved' and p['busy'] and p['run_id'] is None and p['run_count']==0
        assert viewer.put('/api/office/settings',json={'idle_enabled':False}).status_code==403


def test_office_reads_never_emit_task_or_model_events(client,configured):
    create_run(client,configured)
    with session() as s:before=s.scalar(select(func.count()).select_from(Event))
    for _ in range(5):client.get('/api/office/state')
    with session() as s:assert before==s.scalar(select(func.count()).select_from(Event))


def test_visual_settings_persist_validate_and_do_not_touch_agent_runtime(client):
    before=client.get('/api/agents').json()
    r=client.put('/api/office/settings',json={'idle_grace_s':15,'idle_enabled':False,'activities':['drink','stretch']});assert r.status_code==200,r.text
    read=client.get('/api/office/settings').json();assert read['idle_grace_s']==15 and not read['idle_enabled']
    assert client.get('/api/agents').json()==before
    for bad in [{'walk_speed':0},{'idle_grace_s':-1},{'activities':['unknown']},{'activities':['drink','drink']},{'max_model_calls':1000},{'idle_enabled':'true'}]:
        assert client.put('/api/office/settings',json=bad).status_code==422
