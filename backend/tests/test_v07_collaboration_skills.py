import asyncio,copy,io,json,zipfile
import pytest
from sqlalchemy import select
from forge import autonomy,engine,providers,skills,engagement
from forge.providers import Reply
from forge.db import session,Run,Task,Skill,SkillRevision,Outbox,DecisionRequest,Event,Meeting
from forge.local_runtime import LocalRuntime
from forge.workflow_core import DevelopmentFlow
from forge.schema import DecisionQuestion
from conftest import run_body

BUNDLE={'SKILL.md':'---\nname: requirement-clarification\ndescription: Clarify missing decisions and record acceptance criteria.\nlicense: MIT\n---\nAsk concrete questions with request_decision before choosing a missing policy.\nRead references/checklist.md when required.\n','references/checklist.md':'Check scope, compatibility and acceptance tests.','scripts/check.py':'print("skill-proof")\n'}
def make_skill(client,approved=True):
    made=client.post('/api/skills',json={'files':BUNDLE});assert made.status_code==201,made.text
    item=made.json()
    if approved:
        ok=client.post('/api/skills/'+item['id']+'/review',json={'version':1,'digest':item['digest'],'action':'approve'});assert ok.status_code==200,ok.text
    return item

def bind(client,a,skill,mode='on_demand',allow_scripts=False):
    c={k:v for k,v in a.items() if k not in ('id','version')};c['skills']=[{'skill_id':skill['id'],'version':1,'mode':mode,'allow_scripts':allow_scripts}]
    return client.put('/api/agents/'+a['id'],json=c)

def byrole(c,role):return next(a for a in c['agents'] if a['role']==role)

@pytest.mark.parametrize('name',['../SKILL.md','/SKILL.md','scripts/../evil.py','scripts\\evil.py','a//b.md','.env.txt','assets/font.ttf'])
def test_skill_path_rejection(name):
    with pytest.raises(ValueError):skills.validate_files({**BUNDLE,name:'unsafe'})

def test_skill_revision_approval_and_freeze(client,configured):
    item=make_skill(client,False);a=byrole(configured,'planner');assert bind(client,a,item).status_code==422
    assert client.post('/api/skills/'+item['id']+'/review',json={'version':1,'digest':'0'*64,'action':'approve'}).status_code==409
    client.post('/api/skills/'+item['id']+'/review',json={'version':1,'digest':item['digest'],'action':'approve'})
    assert bind(client,a,item,'always').status_code==200
    made=client.post('/api/runs',json=run_body(configured));assert made.status_code==202,made.text
    snapshot=client.get('/api/runs/'+made.json()['id']).json()['snapshot'];f=snapshot['agents'][a['id']]['skills'][0]
    assert f['version']==1 and f['digest']==item['digest']
    new={**BUNDLE,'references/checklist.md':'Changed later'}
    changed=client.put('/api/skills/'+item['id'],json={'files':new,'expected_version':1});assert changed.status_code==200,changed.text
    assert changed.json()['version']==2 and changed.json()['status']=='draft'
    assert client.put('/api/skills/'+item['id'],json={'files':new,'expected_version':1}).status_code==422
    with session() as s:skills.check_live(s,f)
    client.post('/api/skills/'+item['id']+'/review',json={'version':1,'digest':item['digest'],'action':'revoke'})
    with session() as s:
        with pytest.raises(ValueError):skills.check_live(s,f)

def test_skill_zip_roundtrip_and_bad_zip(client):
    item=make_skill(client);raw=client.get('/api/skills/'+item['id']+'/export').content
    assert skills.import_zip(raw)==BUNDLE
    z=io.BytesIO()
    with zipfile.ZipFile(z,'w') as archive:
        archive.writestr('SKILL.md',BUNDLE['SKILL.md']);archive.writestr('../escape.py','bad')
    assert client.post('/api/skills/import',content=z.getvalue(),headers={'Content-Type':'application/zip'}).status_code==422
    assert client.post('/api/skills/import',content=b'not-zip').status_code==422

def test_skill_frontmatter_bounds():
    with pytest.raises(ValueError):skills.validate_files({'SKILL.md':'---\nname: Bad--Name\ndescription: bad\n---\nbody'})
    with pytest.raises(ValueError):skills.validate_files({'SKILL.md':'---\nname: a\ndescription: &ref value\n---\nbody'})
    data=skills.validate_files({'SKILL.md':'---\nname: sample\ndescription: >\n  hello\n  there\nmetadata:\n  author: team\n---\nBody'})[1]
    assert data['description']=='hello there'

@pytest.mark.asyncio
async def test_skills_load_audit_and_script_grants(client,configured,monkeypatch):
    item=make_skill(client);a=byrole(configured,'developer');assert bind(client,a,item).status_code==200
    rid=client.post('/api/runs',json=run_body(configured)).json()['id'];engine.prepare_run({'run_id':rid});engine.create_task(rid,'skilltest','develop',a['id'],'Skill test',{})
    with session() as s:run=s.get(Run,rid);rt=run.snapshot['runtime']
    root=engine.ws.task_root(rid,'skilltest')
    output=await skills.invoke('activate_skill',{'name':'requirement-clarification'},rid,'skilltest','activate',root,rt,'fake')
    assert output['version']==1 and 'request_decision' in output['instructions']
    res=await skills.invoke('read_skill_resource',{'name':'requirement-clarification','path':'references/checklist.md'},rid,'skilltest','read',root,rt,'fake')
    assert res['content']==BUNDLE['references/checklist.md']
    with pytest.raises(ValueError):await skills.invoke('run_skill_script',{'name':'requirement-clarification','path':'scripts/check.py','arguments':[]},rid,'skilltest','no',root,rt,'fake')
    # Explicit grant remains confined to tools.command, never subprocess on host.
    with session() as s:
        run=s.get(Run,rid);snap=copy.deepcopy(run.snapshot);snap['agents'][a['id']]['skills'][0]['allow_scripts']=True;run.snapshot=snap
    seen=[]
    async def command(*args):seen.append(args);return {'exit_code':0,'stdout':'skill-proof','stderr':''}
    monkeypatch.setattr(engine.tools,'command',command)
    out=await skills.invoke('run_skill_script',{'name':'requirement-clarification','path':'scripts/check.py','arguments':[';bad']},rid,'skilltest','yes',root,rt,'fake')
    assert out['exit_code']==0 and len(seen)==1 and "';bad'" in seen[0][3]
    with pytest.raises(ValueError):await skills.invoke('activate_skill',{'name':'unassigned'},rid,'skilltest','x',root,rt,'fake')

def test_exact_directed_resolution_and_permission(client,configured):
    a=byrole(configured,'planner');b=byrole(configured,'developer')
    assert client.post('/api/commands/resolve',json={'text':'@'+a['name']+' 修复功能'}).json()['entry_agent_id']==a['id']
    assert client.post('/api/commands/resolve',json={'text':'@Missing 修复功能'}).status_code==422
    assert client.post('/api/commands/resolve',json={'text':'@'+a['name']+' 修复','entry_agent_id':b['id']}).status_code==422
    frozen={a['id']:{'communication':{'scope':'custom','recipients':[]}},b['id']:{}}
    assert not engagement.can_communicate(frozen,a['id'],b['id'])
    frozen[a['id']]={'communication':{'scope':'department'},'department':'dev'};frozen[b['id']]={'department':'dev'}
    assert engagement.can_communicate(frozen,a['id'],b['id'])
    frozen[a['id']]['department']='';frozen[b['id']]['department']='';assert not engagement.can_communicate(frozen,a['id'],b['id'])

@pytest.mark.asyncio
async def test_question_answer_resume_without_repeat_and_replay(client,configured,monkeypatch):
    a=byrole(configured,'planner');body=run_body(configured,entry_agent_id=a['id'])
    rid=client.post('/api/runs',json=body).json()['id'];engine.prepare_run({'run_id':rid});calls=[]
    task_key=autonomy.autonomy_tick({'run_id':rid})['runnable'][0]['key']
    async def complete(conn,model,messages,definitions,key,timeout):
        calls.append(copy.deepcopy(messages))
        if len(calls)==1:return Reply('需要确认兼容策略',[{'id':'ask','name':'request_decision','arguments':{'question':'采用兼容还是严格校验？','context':'影响旧调用方','options':[{'id':'compat','label':'保持兼容'},{'id':'strict','label':'严格拒绝'}],'allow_text':True}}],10,5)
        assert any('保持兼容' in m.get('content','') for m in messages)
        return Reply('收到选择，记录约束',[{'id':'finish','name':'finish','arguments':{
            'summary':'已记录用户选择的兼容策略，本次确认步骤结束。','outcome':'done',
            'notes':'保持兼容，不改变函数签名；尚未实施或宣称完成修复。'}}],10,5)
    monkeypatch.setattr(providers,'complete',complete)
    first=await engine.turn({'run_id':rid,'key':task_key,'index':0});assert first['blocked_on'] and not first['done']
    req=client.get('/api/runs/'+rid+'/collaboration').json()['decisions'][0]
    assert client.post(f'/api/runs/{rid}/questions/{req["id"]}/answer',json={'digest':'0'*64,'choice_id':'compat'}).status_code==409
    invalid=client.post(f'/api/runs/{rid}/questions/{req["id"]}/answer',json={'digest':req['digest'],'choice_id':'bad'});assert invalid.status_code==422
    answer={'digest':req['digest'],'choice_id':'compat','text':'保持兼容，不改变函数签名'}
    one=client.post(f'/api/runs/{rid}/questions/{req["id"]}/answer',json=answer);assert one.status_code==200,one.text
    assert client.post(f'/api/runs/{rid}/questions/{req["id"]}/answer',json=answer).status_code==200
    assert client.post(f'/api/runs/{rid}/questions/{req["id"]}/answer',json={**answer,'choice_id':'strict'}).status_code==409
    # Re-read of the persisted old activity/turn does not call model again.
    replay=await engine.turn({'run_id':rid,'key':task_key,'index':0});assert replay==first and len(calls)==1
    resumed=autonomy.autonomy_tick({'run_id':rid,'answered':[req['id']]})
    assert resumed['runnable']==[{'key':task_key,'agent_id':a['id']}]
    second=await engine.turn({'run_id':rid,'key':task_key,'index':1});assert second['done'] and len(calls)==2
    overview=client.get('/api/runs/'+rid+'/collaboration').json();assert overview['meetings']==[]
    assert client.get('/api/runs/'+rid).json()['status']!='completed'
    trace=client.get('/api/runs/'+rid+'/trace').json()['items'];assert any(e['kind']=='human.decision.included' for e in trace)
    assert len([e for e in trace if e['kind']=='human.decision.requested'])==1
    public=client.get('/api/runs/'+rid+'/sessions/'+task_key).json();assert all(x['role'] in ('tool','assistant') for x in public['items'])
    assert 'anthropic_native' not in json.dumps(public)

@pytest.mark.asyncio
async def test_flow_blocks_one_task_not_other(client,configured,monkeypatch):
    a=byrole(configured,'planner');b=byrole(configured,'developer')
    flow=DevelopmentFlow(None);flow.run_id='fake';flow.settings={'runtime':{'max_parallel':2,'task_timeout_s':100},'agents':{a['id']:{'runtime':{'task_timeout_s':100,'call_timeout_s':5,'command_timeout_s':2}},b['id']:{'runtime':{'task_timeout_s':100,'call_timeout_s':5,'command_timeout_s':2}}}}
    calls=[]
    class RT:
        def now(self):
            from datetime import datetime,timezone
            return datetime(2026,1,1,tzinfo=timezone.utc)
        async def wait_condition(self,predicate):
            assert ('b',0) in calls
            flow.respond({'command_id':'signal','request_id':'r','rejected':False});assert predicate()
        async def execute_activity(self,name,args,timeout):
            if name=='set_stage':return {}
            calls.append((args['key'],args['index']))
            if args['key']=='a' and args['index']==0:return {'done':False,'blocked_on':'r','elapsed_s':0,'result':{}}
            return {'done':True,'elapsed_s':0,'result':{'summary':'ok'}}
    flow.runtime=RT();flow.started=flow.runtime.now()
    got=await flow.execute_tasks([('a',a['id']),('b',b['id'])]);assert set(got)=={'a','b'} and calls.count(('b',0))==1 and calls.count(('a',0))==1

def test_instruction_trace_permissions_and_pagination(client,configured):
    rid=client.post('/api/runs',json=run_body(configured)).json()['id'];a=configured['agents'][0]['id'];url='/api/runs/'+rid+'/instructions'
    body={'recipients':[a],'body':'保留原接口兼容性'}
    assert client.post(url,json={'recipients':['missing'],'body':'x'}).status_code==422
    one=client.post(url,json=body,headers={'Idempotency-Key':'same'});two=client.post(url,json=body,headers={'Idempotency-Key':'same'});assert one.json()['id']==two.json()['id']
    events=client.get('/api/runs/'+rid+'/trace?limit=1').json();assert events['has_more']
    next_page=client.get(f'/api/runs/{rid}/trace?after={events["next_cursor"]}').json();assert all(x['id']>events['next_cursor'] for x in next_page['items'])
    client.post('/api/auth/logout')
    assert client.get('/api/runs/'+rid+'/collaboration').status_code==401
    assert client.post(url,json=body).status_code==401


@pytest.mark.parametrize('state,expected',[('cancelled','cancelled'),('needs_attention','interrupted')])
def test_pending_question_closes_without_fake_answer(client,configured,state,expected):
    rid=client.post('/api/runs',json=run_body(configured)).json()['id']
    engine.prepare_run({'run_id':rid})
    task_key=autonomy.autonomy_tick({'run_id':rid})['runnable'][0]['key']
    q=engagement.request_decision(rid,task_key,'stop-test',{'question':'确认依赖范围？','context':'待确认','options':[],'allow_text':True})
    engine.set_stage({'run_id':rid,'status':state})
    saved=client.get('/api/runs/'+rid+'/collaboration').json()['decisions'][0]
    assert saved['status']==expected and not saved['answer']
    r=client.post(f'/api/runs/{rid}/questions/{q["id"]}/answer',json={'digest':q['digest'],'text':'继续'})
    assert r.status_code==409
    with session() as s:
        assert not list(s.scalars(select(Outbox).where(Outbox.run_id==rid,Outbox.action=='respond')))

def test_valid_skill_zip_import_api_and_department_ambiguity(client,configured):
    z=io.BytesIO();files={**BUNDLE,'SKILL.md':BUNDLE['SKILL.md'].replace('requirement-clarification','imported-checklist')}
    with zipfile.ZipFile(z,'w',zipfile.ZIP_DEFLATED) as bundle:
        for path,text in files.items():bundle.writestr('imported-checklist/'+path,text)
    result=client.post('/api/skills/import',content=z.getvalue(),headers={'Content-Type':'application/zip'})
    assert result.status_code==201 and result.json()['status']=='draft'
    assert client.get('/api/skills/'+result.json()['id']).json()['files']==files
    # Explicit department references may fan out, but must not guess an entry
    # where several ordinary members exist and no unique planner is in the group.
    members=[byrole(configured,'developer'),byrole(configured,'tester')]
    for a in members:
        data={k:v for k,v in a.items() if k not in ('id','version')};data['department']='质量小组'
        assert client.put('/api/agents/'+a['id'],json=data).status_code==200
    assert client.post('/api/commands/resolve',json={'text':'@{质量小组} 分析回归问题'}).status_code==422
    good=client.post('/api/commands/resolve',json={'text':'@{质量小组} 分析回归问题','entry_agent_id':members[0]['id']})
    assert good.status_code==200 and set(good.json()['recipients'])=={a['id'] for a in members}
