"""v0.6 real local HTTP goal workflow regression; all LLM replies are scripted fixtures."""
from pathlib import Path
import json, os, time, traceback
import httpx
ROOT=Path(__file__).resolve().parents[2]; OUT=ROOT/'docs/evidence-v06';OUT.mkdir(exist_ok=True)
STATE=Path(os.getenv('FORGE_E2E_STATE','/tmp/forge-v06-validation'))
env=json.loads((STATE/'environment.json').read_text());cfg=json.loads((STATE/'setup.json').read_text())
results=[];runids={}
c=httpx.Client(base_url='http://127.0.0.1:8787',headers={'X-Forge-Request':'1'},trust_env=False,timeout=30)
c.post('/api/auth/login',json={'username':'admin','password':env['ADMIN_PASSWORD']}).raise_for_status()
def req(method,path,body=None):
    r=c.request(method,'/api'+path,json=body);r.raise_for_status();return r.json()
def record(name,value=True):
    results.append({'name':name,'status':'passed','detail':value});print('PASS',name,flush=True)
def wait(rid,status,timeout=80):
    start=time.monotonic()
    while time.monotonic()-start<timeout:
        r=req('GET','/runs/'+rid)
        if r['status'] in status:return r
        if r['status'] in ['needs_attention','cancelled','completed'] and r['status'] not in status:raise AssertionError((r['status'],r.get('error'),r.get('report')))
        time.sleep(.15)
    raise TimeoutError((rid,status,r['status']))
participants=[a['id'] for a in cfg['agents'] if a['role'] in ('developer','tester')]
def graph(**kw):
    return {'mode':'discussion','participants':participants,'strategy':'sequential','stop_when':'goal_satisfied','prompt':'核对需求接口与测试边界，明确分歧。','criteria':[{'id':'solution','description':'形成可执行方案并说明关键取舍','evidence_type':'decision'},{'id':'validation','description':'明确验证步骤和风险处理方式','evidence_type':'decision'}],'assessor_id':participants[-1],'stagnation_checks':3,**kw}
def launch(label,g=None,**kw):
    g=g or graph()
    body={'project_id':cfg['project']['id'],'title':label,'request':'修复加法函数，补充边界测试并给出可验证结论。','agent_ids':g['participants'] if g['mode']=='discussion' else [a['id'] for a in cfg['agents']],'orchestration':g,**kw}
    r=req('POST','/runs',body);runids[label]=r['id'];return r['id']
def events(rid):return req('GET','/runs/'+rid+'/events?limit=1000')['events']
def approve(r,phase='acceptance'):
    req('POST',f'/runs/{r["id"]}/decision/{phase}',{'approved':True,'digest':r['artifact_digest' if phase=='acceptance' else 'plan_digest'],'note':'已核对脚本化HTTP夹具目标证据与产物；只验证平台，不代表真实LLM能力。'})
def turns(r):return sorted([t for t in r['tasks'] if t['kind']=='discussion'],key=lambda t:(t['spec']['round'],t['spec']['order']))
def context(r,strategy,n):
    ts=turns(r);assert len(ts)==2*n
    for t in ts:
        s=t['spec'];expected=(s['round']-1)*2+(s['order'] if strategy=='sequential' else 0)
        assert len(s['previous_contributions'])==expected
    return [{'cycle':t['spec']['round'],'prior':len(t['spec']['previous_contributions'])} for t in ts]
def artifacts(rid,prefix,names):
    for name in names:
        rr=c.get(f'/api/runs/{rid}/artifacts/{name}');rr.raise_for_status();(OUT/(prefix+'-'+name)).write_bytes(rr.content)
try:
    record('真实本地API与验证模式启动',req('GET','/health'))
    g=graph();tpl=req('POST','/orchestrations',{'name':'目标验收HTTP模板 '+str(int(time.time())),'graph':g})
    body={'project_id':cfg['project']['id'],'title':'HTTP目标证据收敛','request':'核对需求接口、边界和验证方案','agent_ids':participants,'template_id':tpl['id'],'template_version':tpl['version']}
    rid=req('POST','/runs',body)['id'];runids['goal-sequential']=rid
    newer={**g,'criteria':[dict(x) for x in g['criteria']]};newer['criteria'][0]['description']='模板修改后的新目标，不应修改正在执行的快照'
    req('PUT','/orchestrations/'+tpl['id'],{'name':tpl['name'],'graph':newer,'expected_version':1})
    r=wait(rid,['awaiting_acceptance']);assert r['calls']==6,r['calls'];assert 'rounds' not in r['snapshot']['orchestration'];assert r['snapshot']['orchestration']['criteria'][0]['description']==g['criteria'][0]['description']
    record('目标快照无固定轮次且模板更新不污染已启动任务',context(r,'sequential',2))
    es=events(rid);checks=[e['data'] for e in es if e['kind']=='orchestration.goal.assessed'];assert [x['outcome'] for x in checks]==['continue','goal_satisfied'],checks
    assert all(t['result']['ready'] for t in turns(r));assert turns(r)[2]['spec']['previous_goal_checkpoint']['outcome']=='continue'
    record('全体ready不能提前终止；未满足目标反馈到下一次发言',{'checkpoints':checks,'calls':r['calls']})
    assert r['report']['goal_checkpoint']['met_count']==2;assert len([t for t in r['tasks'] if t['kind']=='goal_review'])==2
    record('单独复核步骤验证目标覆盖与发言来源后才进入待验收',r['report']['goal_checkpoint'])
    stale=c.post('/api/runs',json=body);assert stale.status_code==409;record('过期模板版本拒绝创建',stale.status_code)
    assert len({e['data'].get('model') for e in es if e['kind']=='model.completed'})==2;record('同一目标讨论实际使用双协议供应商连接')
    artifacts(rid,'goal-sequential',['discussion.md','discussion.json','report.json']);assert c.get(f'/api/runs/{rid}/artifacts/changes.patch').status_code==404
    (OUT/'goal-sequential-events.json').write_text(json.dumps(es,ensure_ascii=False,indent=2));(OUT/'goal-sequential-run.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
    record('只读讨论有实际纪要与证据，不伪造代码补丁',r['report']['checks'])
    approve(r);wait(rid,['completed']);record('目标达成只生成候选，仍需人工接受才完成')
    rid=launch('HTTP并行目标讨论',graph(strategy='parallel'));r=wait(rid,['awaiting_acceptance']);assert r['calls']==6
    record('目标驱动的并行输入仍隔离同一批成员结果',context(r,'parallel',2));approve(r);wait(rid,['completed'])
    rid=launch('HTTP目标一次满足',request='[immediate] 目标和证据第一次就满足');r=wait(rid,['awaiting_acceptance']);assert r['calls']==3 and len(turns(r))==2
    record('第一次目标满足即可候选收敛，无最低轮数',{'calls':r['calls'],'cycle':r['report']['goal_checkpoint']['cycle']});approve(r);wait(rid,['completed'])
    rid=launch('HTTP目标无进展',request='[stalled] 缺少方案证据，持续无进展');r=wait(rid,['needs_attention']);assert r['report']['discussion']['stop_reason']=='stalled';assert r['report']['goal_checkpoint']['outcome']=='stalled'
    artifacts(rid,'goal-stalled',['discussion.md','report.json']);record('无进展保护保存部分结果并转人工，不记成功',{'calls':r['calls'],'status':r['status'],'stop_reason':r['report']['discussion']['stop_reason']})
    rid=launch('HTTP目标调用预算',runtime={'max_model_calls':2});r=wait(rid,['needs_attention']);assert r['calls']==2 and r['report']['discussion']['stop_reason']=='budget_exhausted'
    record('预算耗尽不是完成条件且已完成发言被保存',{'calls':r['calls'],'stop_reason':r['report']['discussion']['stop_reason']})
    rid=launch('HTTP目标需要人工',request='[human] [immediate] 权限问题需要人工决定');r=wait(rid,['needs_attention']);assert r['report']['goal_checkpoint']['outcome']=='needs_human';record('复核指出需要人工时不继续自动完成')
    rid=launch('HTTP文件证据核验',graph(criteria=[{'id':'source','description':'结合实际calc.py文件指出问题并给出修复方案','evidence_type':'code_reference'}]));r=wait(rid,['awaiting_acceptance']);row=r['report']['goal_checkpoint']['criteria'][0];assert row['file_receipts'] and all(len(x['sha256'])==64 for x in row['file_receipts']) and not row['errors'];assert r['report']['checks']['verification']=='unverified'
    record('源文件证据有真实read_file回执与SHA，不充当测试通过',{'calls':r['calls'],'receipts':row['file_receipts'],'checks':r['report']['checks']});approve(r);wait(rid,['completed'])
    rid=launch('HTTP目标后开发',graph(mode='development',strategy='parallel'));r=wait(rid,['awaiting_approval']);plan=next(t for t in r['tasks'] if t['key']=='plan');assert len(plan['spec']['discussion']['turns'])==4 and plan['spec']['discussion']['stop_reason']=='goal_satisfied'
    record('目标达成后的讨论传入开发计划且继续等待人工批准');approve(r,'plan');r=wait(rid,['awaiting_acceptance']);assert r['report']['checks']['verification']=='passed';assert r['report']['goal_checkpoint']['outcome']=='goal_satisfied';assert r['calls']==22,r['calls']
    record('目标讨论接通真实Git与固定测试且保留目标检查点',{'calls':r['calls'],'checks':r['report']['checks']});artifacts(rid,'goal-development',['changes.patch','report.json','source.zip'])
    (OUT/'goal-development-events.json').write_text(json.dumps(events(rid),ensure_ascii=False,indent=2));assert (STATE/'imports/fixture-project/calc.py').read_text()=='def add(a, b):\n    return a - b\n';approve(r);wait(rid,['completed']);record('开发成果经人工验收，原项目未被修改')
except Exception:
    results.append({'name':'HTTP目标验收异常','status':'failed','error':traceback.format_exc()});print(traceback.format_exc(),flush=True)
finally:
    (OUT/'http-results.json').write_text(json.dumps({'method':'real HTTP/SQLite/local workflows/Git/fixed unittest; SCRIPTED model replies','passed':sum(x['status']=='passed' for x in results),'failed':sum(x['status']=='failed' for x in results),'run_ids':runids,'results':results},ensure_ascii=False,indent=2));c.close()
if any(x['status']=='failed' for x in results):raise SystemExit(1)
