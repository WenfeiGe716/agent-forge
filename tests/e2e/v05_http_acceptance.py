"""Real local HTTP API + durable local workflow + scripted dual-protocol providers.
No real LLM or Docker claim. Fixed verified fixture only for unittest execution.
"""
from pathlib import Path
import json,os,time,uuid,traceback
import httpx
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v05';STATE=Path(os.getenv('FORGE_E2E_STATE','/tmp/forge-v05-e2e'));env=json.loads((STATE/'environment.json').read_text());cfg=json.loads((STATE/'setup.json').read_text());results=[];runids={}
c=httpx.Client(base_url='http://127.0.0.1:8787',headers={'X-Forge-Request':'1'},trust_env=False,timeout=30)
c.post('/api/auth/login',json={'username':'admin','password':env['ADMIN_PASSWORD']}).raise_for_status()
def req(method,path,body=None):
    r=c.request(method,'/api'+path,json=body);r.raise_for_status();return r.json()
def test(name,fn):
    value=fn();results.append({'name':name,'status':'passed','detail':value});print('PASS',name,flush=True);return value
def wait(rid,status,timeout=65):
    start=time.monotonic()
    while time.monotonic()-start<timeout:
        r=req('GET','/runs/'+rid)
        if r['status'] in status:return r
        if r['status'] in ['needs_attention','cancelled','completed'] and r['status'] not in status:raise AssertionError(f'Unexpected {r["status"]}: {r.get("error")}')
        time.sleep(.15)
    raise TimeoutError((rid,status,r['status']))
participants=[a['id'] for a in cfg['agents'] if a['role'] in ('developer','tester')]
def graph(**kw):return {'mode':'discussion','participants':participants,'rounds':3,'min_rounds':1,'strategy':'sequential','stop_when':'max_rounds','prompt':'核对需求接口与测试边界，明确分歧。',**kw}
def launch(label,g,**kw):
    body={'project_id':cfg['project']['id'],'title':label,'request':'修复加法函数，补充边界测试并给出可验证结论。','agent_ids':g['participants'] if g['mode']=='discussion' else [a['id'] for a in cfg['agents']],'orchestration':g,**kw}
    r=req('POST','/runs',body);runids[label]=r['id'];return r['id']
def events(rid):return req('GET','/runs/'+rid+'/events?limit=1000')['events']
def approve(r,phase='acceptance'):
    return req('POST',f'/runs/{r["id"]}/decision/{phase}',{'approved':True,'digest':r['artifact_digest' if phase=='acceptance' else 'plan_digest'],'note':'固定夹具功能验收：已核对实际HTTP、逐轮记录及产物；不代表真实LLM质量。'})
def assert_context(r,strategy,n):
    turns=sorted([t for t in r['tasks'] if t['kind']=='discussion'],key=lambda t:(t['spec']['round'],t['spec']['order']))
    assert len(turns)==n*2 and all(t['status']=='done' for t in turns)
    for t in turns:
        spec=t['spec'];expect=(spec['round']-1)*2+(spec['order'] if strategy=='sequential' else 0)
        assert len(spec['previous_contributions'])==expect,(spec,expect)
    return [{'round':t['spec']['round'],'order':t['spec']['order'],'prior':len(t['spec']['previous_contributions'])} for t in turns]
try:
    health=req('GET','/health');test('实际API启动v0.5并使用本地持久化编排',lambda:health)
    atlas=next(a for a in cfg['agents'] if a['role']=='planner');nova=next(a for a in cfg['agents'] if a['role']=='developer')
    for a in cfg['agents']:
        b={k:v for k,v in a.items() if k not in ['id','version']};b.update(reports_to=None if a['id']==atlas['id'] else atlas['id'],peer_reception='sit');req('PUT','/agents/'+a['id'],b)
    test('成员领导关系与接待方式在数据库保存',lambda:[{k:a.get(k) for k in ('name','reports_to','peer_reception')} for a in req('GET','/agents')])
    g=graph();tpl=req('POST','/orchestrations',{'name':'HTTP 双Agent接力3轮','graph':g})
    body={'project_id':cfg['project']['id'],'title':'HTTP串行接力','request':'讨论接口与验收条件','agent_ids':participants,'template_id':tpl['id'],'template_version':tpl['version']}
    rid=req('POST','/runs',body)['id'];runids['sequential']=rid
    tpl2=req('PUT','/orchestrations/'+tpl['id'],{'name':tpl['name'],'graph':{**g,'rounds':5},'expected_version':1})
    r=wait(rid,['awaiting_acceptance']);assert r['calls']==6 and r['snapshot']['orchestration']['rounds']==3 and r['snapshot']['orchestration_template']['version']==1
    test('模板更新后已启动运行仍按原快照执行3轮6次真实HTTP请求',lambda:assert_context(r,'sequential',3))
    stale=c.post('/api/runs',json=body);assert stale.status_code==409;test('过期模板版本不能启动新运行',lambda:{'status':stale.status_code})
    es=events(rid);protocols={e['data'].get('model') for e in es if e['kind']=='model.completed'};assert len(protocols)==2
    test('同一讨论使用两个不同协议连接的模型',lambda:sorted(protocols))
    for name in ['discussion.md','discussion.json','report.json']:
        artifact=c.get(f'/api/runs/{rid}/artifacts/{name}');artifact.raise_for_status();(OUT/('http-sequential-'+name)).write_bytes(artifact.content)
    assert c.get(f'/api/runs/{rid}/artifacts/changes.patch').status_code==404
    test('只读讨论生成实际纪要与JSON，不生成伪造代码补丁',lambda:r['report']['checks'])
    approve(r);r=wait(rid,['completed']);test('讨论成果必须人工接受后才完成',lambda:r['status'])
    rid=launch('HTTP并行3轮',graph(strategy='parallel'));r=wait(rid,['awaiting_acceptance']);assert r['calls']==6
    test('并行轮间屏障：同轮只看到上一轮及更早的全部结果',lambda:assert_context(r,'parallel',3));approve(r);wait(rid,['completed'])
    rid=launch('HTTP一致就绪提前结束',graph(rounds=5,min_rounds=2,stop_when='unanimous_ready'));r=wait(rid,['awaiting_acceptance']);assert r['calls']==4 and r['report']['discussion']['stop_reason']=='unanimous_ready'
    test('至少2轮且全体ready后提前结束，不跑满5轮',lambda:r['report']['discussion']['rounds_completed']);approve(r);wait(rid,['completed'])
    rid=launch('HTTP讨论预算限制',graph(),runtime={'max_model_calls':1});r=wait(rid,['needs_attention']);assert r['calls']==1
    test('多轮讨论也遵守总调用次数硬上限',lambda:{'calls':r['calls'],'error':r['error']})
    rid=launch('HTTP暂停与取消',graph(rounds=12));wait(rid,['discussing']);req('POST','/runs/'+rid+'/control',{'action':'pause'});r=wait(rid,['paused']);n=r['calls'];time.sleep(1.5);assert req('GET','/runs/'+rid)['calls']==n
    test('讨论暂停后无新增模型请求',lambda:{'calls_while_paused':n});req('POST','/runs/'+rid+'/control',{'action':'resume'});wait(rid,['discussing']);req('POST','/runs/'+rid+'/control',{'action':'cancel'});r=wait(rid,['cancelled']);test('讨论可恢复并在执行边界取消',lambda:r['status'])
    rid=launch('HTTP讨论后完整开发',graph(mode='development',rounds=2,strategy='parallel'));r=wait(rid,['awaiting_approval']);assert len(r['plan']['tasks'])==3
    plan=next(t for t in r['tasks'] if t['key']=='plan');assert len(plan['spec']['discussion']['turns'])==4
    test('讨论结果传入负责人规划，开发仍须人工审批',lambda:{'discussion_turns':4,'planned_tasks':len(r['plan']['tasks'])});approve(r,'plan');r=wait(rid,['awaiting_acceptance']);assert r['report']['checks']['verification']=='passed'
    test('开发前讨论与原Git修改/测试/独立审查链路集成',lambda:{'calls':r['calls'],'checks':r['report']['checks']})
    for name in ['changes.patch','report.json','source.zip']:
        out=c.get(f'/api/runs/{rid}/artifacts/{name}');out.raise_for_status();(OUT/('http-development-'+name)).write_bytes(out.content)
    (OUT/'http-development-events.json').write_text(json.dumps(events(rid),ensure_ascii=False,indent=2))
    source=STATE/'imports/fixture-project/calc.py';assert source.read_text()=='def add(a, b):\n    return a - b\n'
    test('原始导入目录不被修改且真实成果可读取',lambda:{'source_unchanged':True,'patch_bytes':(OUT/'http-development-changes.patch').stat().st_size})
    approve(r);wait(rid,['completed']);test('完整开发成果也经人工验收后完成',lambda:True)
except Exception as e:
    results.append({'name':'HTTP验收异常','status':'failed','error':traceback.format_exc()});print(traceback.format_exc(),flush=True)
finally:
    (OUT/'http-results.json').write_text(json.dumps({'providers':'scripted HTTP, no real LLM','runtime':'real local workflow + SQLite + actual Git/fixed unittest','passed':sum(r['status']=='passed' for r in results),'failed':sum(r['status']=='failed' for r in results),'run_ids':runids,'results':results},ensure_ascii=False,indent=2));(STATE/'v05-http-runs.json').write_text(json.dumps(runids));c.close()
if any(r['status']=='failed' for r in results):raise SystemExit(1)
