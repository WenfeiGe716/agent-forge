import json
import pytest
from sqlalchemy import select,func
from fastapi.testclient import TestClient
from forge.api import app
from forge import autonomy,engine
from forge.db import session,Run,Task,Event,Agent,emit,now
from conftest import run_body


def create_run(client,configured,**kw):
    r=client.post('/api/runs',json=run_body(configured,**kw));assert r.status_code==202,r.text
    return r.json()['id']


def member(client,aid):
    r=client.get('/api/office/state');assert r.status_code==200,r.text
    return next(p for p in r.json()['presence'] if p['agent_id']==aid)


def set_task(rid,aid,state='running'):
    """Create a real autonomous assignment; only its event/state is simulated."""
    engine.prepare_run({'run_id':rid})
    initial=autonomy.autonomy_tick({'run_id':rid})['runnable'][0]
    key=initial['key']
    if initial['agent_id']!=aid:
        assigned=autonomy.assign_work(rid,key,'office-fixture-'+aid,{
            'recipient_id':aid,'title':'Work on source','instruction':'Inspect and update the required source',
            'acceptance':'Actual changes and evidence are recorded'})
        with session() as s:
            run=s.get(Run,rid);task=s.get(Task,rid+':'+key)
            task.result=engine.finish_result(run,task,run.snapshot['agents'][task.agent_id],
                {'summary':'Waiting for assigned work','outcome':'waiting'})
            task.status='done'
        engine.integrate_task({'run_id':rid,'key':key})
        ready=autonomy.autonomy_tick({'run_id':rid})['runnable']
        assert ready==[{'key':assigned['task_key'],'agent_id':aid}]
        key=assigned['task_key']
    with session() as s:
        r=s.get(Run,rid);r.status='running'
        t=s.get(Task,rid+':'+key);assert t.kind=='autonomous';t.status=state
        emit(s,rid,'tool.started',{'agent_id':aid,'task_key':key,'tool':'write_file'})
    return key


def test_office_requires_login(client):
    c=TestClient(app);assert c.get('/api/office/state').status_code==401


def test_empty_team_has_complete_idle_projection(client):
    data=client.get('/api/office/state').json()
    assert data['complete'] is True and data['read_only_projection'] is True
    assert len(data['presence'])==4 and all(not p['busy'] for p in data['presence'])
    assert data['events']==[]


def test_queued_leader_reserved_while_unassigned_pool_members_stay_idle(client,configured):
    rid=create_run(client,configured)
    leader=client.get('/api/runs/'+rid).json()['entry_agent_id']
    for p in client.get('/api/office/state').json()['presence']:
        if p['agent_id']==leader: assert p['busy'] and p['phase']=='waiting'
        else: assert not p['busy'] and p['phase']=='idle' and p['run_count']==0


def test_tool_event_controls_phase_and_evidence(client,configured):
    rid=create_run(client,configured);aid=configured['agents'][1]['id'];key=set_task(rid,aid)
    p=member(client,aid);assert p['phase']=='writing' and p['tool']=='write_file' and len(p['evidence_ids'])==1
    with session() as s:emit(s,rid,'tool.completed',{'agent_id':aid,'task_key':key,'tool':'write_file'})
    assert member(client,aid)['phase']=='thinking'


def test_finished_member_released_but_unresolved_waiting_owner_remains_reserved(client,configured):
    rid=create_run(client,configured);aid=configured['agents'][1]['id'];key=set_task(rid,aid)
    with session() as s:
        task=s.get(Task,rid+':'+key);task.status='done';task.result={'outcome':'done','summary':'Finished own assignment'}
    p=member(client,aid);assert not p['busy'] and p['phase']=='idle'
    leader=client.get('/api/runs/'+rid).json()['entry_agent_id']
    owner=member(client,leader);assert owner['busy'] and owner['phase']=='waiting'


@pytest.mark.parametrize('status', ['conflicted', 'superseded'])
def test_preserved_conflict_activation_releases_member_but_other_run_still_counts(client, configured, status):
    rid=create_run(client,configured);aid=configured['agents'][1]['id'];key=set_task(rid,aid)
    with session() as s:
        task=s.get(Task,rid+':'+key)
        task.status=status
        task.result={'outcome':'waiting','summary':'Original preserved work, now handled by coordinator'}
    assert not member(client,aid)['busy']
    other=create_run(client,configured,title='Different real work')
    set_task(other,aid)
    presence=member(client,aid)
    assert presence['busy'] and presence['run_id']==other and presence['run_count']==1


def test_pause_approval_attention_never_idle(client,configured):
    rid=create_run(client,configured);aid=configured['agents'][0]['id']
    set_task(rid,aid)
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
    aid=configured['agents'][1]['id'];set_task(first,aid);set_task(second,aid)
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
    leader=client.get('/api/runs/'+rid).json()['entry_agent_id']
    with session() as s:emit(s,rid,'agent.message',{'from_agent_id':aid,'body':'PRIVATE-BODY'})
    u=client.post('/api/users',json={'username':'viewer','password':'viewer-long-password-123','role':'viewer'});assert u.status_code==201,u.text
    with TestClient(app,headers={'X-Forge-Request':'1'}) as viewer:
        r=viewer.post('/api/auth/login',json={'username':'viewer','password':'viewer-long-password-123'});assert r.status_code==200
        data=viewer.get('/api/office/state?after=0').json();text=json.dumps(data)
        assert rid not in text and 'PRIVATE-BODY' not in text
        assert data['events']==[]
        for p in data['presence']:
            assert p['run_id'] is None and p['run_count']==0 and not p['evidence_ids'] and p['task_key'] is None
            if p['agent_id'] in {leader,aid}: assert p['phase']=='reserved' and p['busy']
            else: assert p['phase']=='idle' and not p['busy']
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
