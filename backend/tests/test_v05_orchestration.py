import asyncio
import copy
from pathlib import Path
from sqlalchemy import select
import pytest
from conftest import run_body
from forge import engine,providers
from forge.db import session,Run,Task,Step,Event,Project,Agent
from forge.providers import Reply
from forge.workflow_core import DevelopmentFlow
from forge.local_runtime import LocalRuntime
from forge.schema import OrchestrationInput


def graph(cfg,**kw):
    ids=[a['id'] for a in cfg['agents'] if a['role'] in ('developer','tester')]
    return {'mode':'discussion','participants':ids,'rounds':3,'min_rounds':1,'strategy':'sequential','stop_when':'max_rounds','prompt':'讨论输入输出与测试要求',**kw}
def draft(cfg,g):return run_body(cfg,agent_ids=g['participants'] if g['mode']=='discussion' else [a['id'] for a in cfg['agents']],orchestration=g)
def clean(row):return {k:v for k,v in row.items() if k not in ['id','version','created_at','owner_id','base_commit']}

def autonomous_draft(cfg,g,**changes):
    ids=g['participants'] if g['mode']=='discussion' else [a['id'] for a in cfg['agents']]
    return run_body(cfg,agent_ids=ids,entry_agent_id=ids[0],**changes)

def internal_legacy_run(client,cfg,g,**changes):
    """Isolated legacy engine fixture; never enables public fixed-flow creation."""
    body=autonomous_draft(cfg,g,**changes)
    made=client.post('/api/runs',json=body)
    assert made.status_code==202,made.text
    assert made.json()['execution_strategy']=='autonomous'
    from forge.snapshots import freeze_run
    with session() as s:
        run=s.get(Run,made.json()['id'])
        run.snapshot=freeze_run(s,s.get(Project,run.project_id),[s.get(Agent,aid) for aid in body['agent_ids']],
            body.get('runtime',{}),None,OrchestrationInput.model_validate(g).model_dump(),execution_strategy='workflow')
    return made

def test_template_crud_and_version_conflict(client,configured):
    g=graph(configured)
    x=client.post('/api/orchestrations',json={'name':'讨论模板','graph':g});assert x.status_code==201,x.text
    t=x.json();assert t['version']==1
    assert any(a['id']==t['id'] for a in client.get('/api/orchestrations').json())
    x=client.put('/api/orchestrations/'+t['id'],json={'name':'讨论模板2','graph':g,'expected_version':1});assert x.status_code==200
    assert x.json()['version']==2
    assert client.put('/api/orchestrations/'+t['id'],json={'name':'stale','graph':g,'expected_version':1}).status_code==409

def test_templates_remain_editable_but_new_fixed_launch_is_retired(client,configured):
    g=graph(configured);t=client.post('/api/orchestrations',json={'name':'stable','graph':g}).json()
    body=run_body(configured,agent_ids=g['participants'],template_id=t['id'],template_version=1)
    assert client.post('/api/runs',json=body).status_code==422
    client.put('/api/orchestrations/'+t['id'],json={'name':'new','graph':{**g,'rounds':5},'expected_version':1}).raise_for_status()
    saved=next(row for row in client.get('/api/orchestrations').json() if row['id']==t['id'])
    assert saved['version']==2 and saved['name']=='new'
    assert 'rounds' not in saved['graph'] and saved['graph']['stop_when']=='goal_satisfied'
    assert saved['graph']['legacy_stop_migrated']
    assert client.post('/api/runs',json=body).status_code==422
    assert client.post('/api/runs',json={**body,'template_version':2}).status_code==422
    with session() as s: assert not list(s.scalars(select(Run)))

@pytest.mark.parametrize('patch',[{'rounds':0},{'rounds':13},{'rounds':1,'min_rounds':2},{'participants':[]},{'participants':['unknown','other']},{'strategy':'random'}])
def test_invalid_graph_rejected(client,configured,patch):
    g=graph(configured,**patch)
    if patch.get('participants')==['unknown','other']:
        res=client.post('/api/runs',json=autonomous_draft(configured,g))
        assert res.status_code==422 and '不存在' in res.json()['detail']
    else:
        with pytest.raises(ValueError): OrchestrationInput.model_validate(g)

def test_autonomous_subset_no_fixed_roles_or_mandatory_discussion(client,configured):
    g=graph(configured);res=client.post('/api/runs',json=autonomous_draft(configured,g));assert res.status_code==202,res.text
    snap=client.get('/api/runs/'+res.json()['id']).json()['snapshot'];assert set(snap['agents'])==set(g['participants'])
    assert snap['execution_strategy']=='autonomous' and snap['orchestration'] is None and not snap['collaboration_flow']

def test_fixed_development_graph_is_retired_not_a_role_requirement(client,configured):
    g=graph(configured,mode='development')
    res=client.post('/api/runs',json={**draft(configured,g),'agent_ids':g['participants']})
    assert res.status_code==422 and '固定编排' in res.text

def test_disabled_agent_model_and_archived_project(client,configured):
    g=graph(configured);a=next(a for a in configured['agents'] if a['id']==g['participants'][0]);body=clean(a)
    request=autonomous_draft(configured,g)
    assert client.post('/api/runs/preview-config',json=request).status_code==200
    client.put('/api/agents/'+a['id'],json={**body,'enabled':False}).raise_for_status()
    rejected=client.post('/api/runs',json=request)
    assert rejected.status_code==422 and '停用' in rejected.json()['detail']
    client.put('/api/agents/'+a['id'],json={**body,'enabled':True}).raise_for_status()
    m=next(m for m in client.get('/api/models').json() if m['id']==a['model_profile_id']);mb=clean(m)
    client.put('/api/models/'+m['id'],json={**mb,'enabled':False}).raise_for_status()
    rejected=client.post('/api/runs',json=request)
    assert rejected.status_code==422 and '模型' in rejected.json()['detail'] and '停用' in rejected.json()['detail']
    client.put('/api/models/'+m['id'],json={**mb,'enabled':True}).raise_for_status()
    p=configured['project'];client.put('/api/projects/'+p['id'],json={**clean(p),'archived':True}).raise_for_status()
    rejected=client.post('/api/runs',json=request)
    assert rejected.status_code==422 and '归档' in rejected.json()['detail']
    with session() as s: assert not list(s.scalars(select(Run)))

def test_reporting_hierarchy_cycle_and_reception(client,configured):
    a,b=configured['agents'][:2]
    x=client.put('/api/agents/'+a['id'],json={**clean(a),'reports_to':b['id'],'peer_reception':'stand'});assert x.status_code==200,x.text
    assert x.json()['peer_reception']=='stand'
    assert client.put('/api/agents/'+b['id'],json={**clean(b),'reports_to':a['id']}).status_code==422
    assert client.put('/api/agents/'+a['id'],json={**clean(a),'reports_to':a['id']}).status_code==422
    assert client.put('/api/agents/'+a['id'],json={**clean(a),'reports_to':'unknown'}).status_code==422

@pytest.mark.asyncio
@pytest.mark.parametrize('strategy',['sequential','parallel'])
async def test_internal_legacy_multiround_flow_context_and_artifact(client,configured,monkeypatch,strategy):
    g=graph(configured,strategy=strategy,rounds=3);made=internal_legacy_run(client,configured,g)
    rid=made.json()['id'];seen=[]
    async def complete(conn,model,messages,tools,key,timeout):
        import json
        ctx=json.loads(messages[1]['content']);spec=ctx['task'];
        if 'transcript_digest' in spec:
            return goal_reply(spec, spec['cycle']>=3)
        seen.append(copy.deepcopy(spec));assert {t['name'] for t in tools}=={'list_files','read_file','send_message','finish','request_decision'}
        return Reply('explicit scripted fixture',[{'id':'finish','name':'finish','arguments':{'summary':f'Round {spec["round"]} speaker {spec["order"]}', 'notes':'Test fixture', 'ready':True}}],10,5)
    monkeypatch.setattr(providers,'complete',complete)
    runtime=LocalRuntime(rid,asyncio.Semaphore(3));flow=DevelopmentFlow(runtime)
    async def wait(predicate):
        # Auto approve solely inside this test. Production requires API approval.
        with session() as s:r=s.get(Run,rid);digest=r.artifact_digest
        flow.decide({'command_id':'test-approved','phase':'acceptance','digest':digest,'approved':True});assert predicate()
    runtime.wait_condition=wait
    outcome=await flow.run(rid);assert outcome['status']=='completed',client.get('/api/runs/'+rid).json()
    assert len(seen)==6
    for spec in seen:
        expected=(spec['round']-1)*2+(spec['order'] if strategy=='sequential' else 0)
        assert len(spec['previous_contributions'])==expected
    detail=client.get('/api/runs/'+rid).json();assert detail['calls']==9
    assert detail['report']['checks']['verification']=='unverified'
    assert detail['report']['discussion']['rounds_completed']==3
    assert len(detail['report']['discussion']['turns'])==6
    for file in ['discussion.json','discussion.md','report.json']:assert client.get(f'/api/runs/{rid}/artifacts/{file}').status_code==200
    assert configured['source'].joinpath('calc.py').read_text().endswith('a - b\n')

@pytest.mark.asyncio
async def test_internal_legacy_goal_stop_ignores_round_minimum_and_ready(client,configured,monkeypatch):
    g=graph(configured,rounds=6,min_rounds=2,stop_when='unanimous_ready');r=internal_legacy_run(client,configured,g).json();rid=r['id']
    async def complete(conn,model,messages,*args):
        import json
        spec=json.loads(messages[1]['content'])['task']
        if 'transcript_digest' in spec:return goal_reply(spec, True)
        return Reply('script',[{'id':'end','name':'finish','arguments':{'summary':'有依据的候选结论','notes':'fixture','ready':False,'open_issues':[]}}],1,1)
    monkeypatch.setattr(providers,'complete',complete)
    flow=DevelopmentFlow(LocalRuntime(rid,asyncio.Semaphore(2)));flow.run_id=rid;flow.started=flow.runtime.start_time();flow.settings=await flow.call('prepare_run',{})
    result=await flow.discuss(g);assert result['rounds_completed']==1;assert result['stop_reason']=='goal_satisfied';assert len(result['turns'])==2
    assert client.get('/api/runs/'+rid).json()['calls']==3
    # A separately configured hard limit must stop the discussion too.
    other=internal_legacy_run(client,configured,g,runtime={'max_model_calls':1}).json()['id'];out=await DevelopmentFlow(LocalRuntime(other,asyncio.Semaphore(2))).run(other)
    assert out['status']=='needs_attention';assert client.get('/api/runs/'+other).json()['calls']==1

@pytest.mark.asyncio
async def test_internal_legacy_discussion_write_tool_denied(client,configured,monkeypatch):
    g=graph(configured);rid=internal_legacy_run(client,configured,g).json()['id'];engine.prepare_run({'run_id':rid});engine.prepare_discussion({'run_id':rid,'round':1,'order':0})
    async def bad(*args):return Reply('fixture write attempt',[{'id':'bad','name':'write_file','arguments':{'path':'calc.py','content':'malicious'}}],1,1)
    monkeypatch.setattr(providers,'complete',bad)
    out=await engine.turn({'run_id':rid,'key':'discussion_1_0','index':0});assert not out['done']
    with session() as s:
        task=s.get(Task,engine.task_id(rid,'discussion_1_0'));assert '工具不在' in task.messages[-1]['content']
    from forge import workspace
    assert (workspace.task_root(rid,'discussion_1_0')/'calc.py').read_text().endswith('a - b\n')

def test_viewer_cannot_modify_templates(client,configured):
    u=client.post('/api/users',json={'username':'reader','password':'long-viewer-password','role':'viewer'});assert u.status_code==201
    client.post('/api/auth/logout');client.post('/api/auth/login',json={'username':'reader','password':'long-viewer-password'})
    assert client.post('/api/orchestrations',json={'name':'denied','graph':graph(configured)}).status_code==403

@pytest.mark.asyncio
async def test_internal_legacy_parallel_messages_wait_for_next_round(client,configured,monkeypatch):
    from forge.collaboration import send_message,pending_messages
    g=graph(configured,strategy='parallel');rid=internal_legacy_run(client,configured,g).json()['id']
    engine.prepare_run({'run_id':rid})
    for order in (0,1):engine.prepare_discussion({'run_id':rid,'round':1,'order':order})
    recipient=g['participants'][1]
    send_message(rid,'discussion_1_0','deferred-message',recipient,'只应在下一轮加入上下文')
    seen=[]
    async def complete(conn,model,messages,*args):
        seen.append(copy.deepcopy(messages))
        return Reply('fixture',[{'id':'finish','name':'finish','arguments':{'summary':'固定结论','notes':'fixture','ready':True}}],1,1)
    monkeypatch.setattr(providers,'complete',complete)
    await engine.turn({'run_id':rid,'key':'discussion_1_1','index':0})
    assert not any('collaboration_messages' in str(m.get('content','')) for m in seen[-1])
    assert len(pending_messages(rid,recipient))==1
    await engine.turn({'run_id':rid,'key':'discussion_1_0','index':0})
    engine.prepare_discussion({'run_id':rid,'round':2,'order':1})
    await engine.turn({'run_id':rid,'key':'discussion_2_1','index':0})
    assert any('collaboration_messages' in str(m.get('content','')) for m in seen[-1])
    assert len(pending_messages(rid,recipient))==0


def goal_reply(spec, met=True, **patch):
    evidence=[{'task_key':t['key'],'explanation':'引用实际记录的观点，脚本测试依据'} for t in spec['transcript'][-2:]]
    result={'summary':'依据记录逐项核查目标；这不是实际模型推理', 'transcript_digest':spec['transcript_digest'],
        'assessments':[{'criterion_id':c['id'],'status':'met' if met else 'unmet','rationale':'已具有所需依据' if met else '尚需解决明确缺项','evidence':evidence if met else []} for c in spec['criteria']], 'open_issues':[], 'needs_human':False, **patch}
    return Reply('SCRIPTED GOAL REVIEW',[{'id':'goal-finish','name':'finish','arguments':result}],10,5)
