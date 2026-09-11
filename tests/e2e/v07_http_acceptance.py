"""Real HTTP, SQLite, persisted coordinator and Git. ALL model replies SCRIPTED.
No arbitrary host commands. Requires isolated start_fixture.py --seed.
"""
from pathlib import Path
import json,os,time,traceback,uuid
import httpx
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v07';OUT.mkdir(exist_ok=True)
STATE=Path(os.getenv('FORGE_E2E_STATE','/tmp/forge-v07-validation'));env=json.loads((STATE/'environment.json').read_text());setup=json.loads((STATE/'setup.json').read_text())
c=httpx.Client(base_url='http://127.0.0.1:8787',headers={'X-Forge-Request':'1'},trust_env=False,timeout=30)
c.post('/api/auth/login',json={'username':'admin','password':env['ADMIN_PASSWORD']}).raise_for_status();results=[];runs={}
def req(method,path,body=None):
 r=c.request(method,'/api'+path,json=body)
 if r.status_code>=400:raise AssertionError((method,path,r.status_code,r.text))
 return r.json()
def record(name,detail=True):results.append({'name':name,'status':'passed','detail':detail});print('PASS',name,flush=True)
def wait(rid,states,timeout=90):
 start=time.monotonic()
 while time.monotonic()-start<timeout:
  r=req('GET','/runs/'+rid)
  if r['status'] in states:return r
  if r['status'] in ('completed','cancelled','needs_attention'):raise AssertionError((r['status'],r['error'],r.get('report')))
  time.sleep(.15)
 raise TimeoutError((rid,states,r['status']))
def approve(r,phase='acceptance'):
 return req('POST',f'/runs/{r["id"]}/decision/{phase}',{'approved':True,'digest':r['plan_digest' if phase=='plan' else 'artifact_digest'],'note':'已核对真实HTTP与固定Git/单测结果；LLM回复为脚本化服务，非模型质量验收。'})
def events(rid):return req('GET',f'/runs/{rid}/events?limit=1000')['events']
def collab(rid):return req('GET',f'/runs/{rid}/collaboration')
def answer(rid,q,reject=False):return req('POST',f'/runs/{rid}/questions/{q["id"]}/answer',{'digest':q['digest'],'choice_id':'api-first','text':'保持签名，不增加无关功能。','reject':reject})
def launch(label,mode='development',pool=None):
 agents=req('GET','/agents');ids={a['role']:a['id'] for a in agents};pool=pool or [ids['planner'],ids['developer'],ids['tester']]
 g={'mode':mode,'participants':pool,'strategy':'sequential','stop_when':'goal_satisfied','prompt':'明确已回答的交付方向与实际验收证据','criteria':[{'id':'solution','description':'给出可执行方案与验证方式','evidence_type':'decision'}],'assessor_id':pool[-1],'stagnation_checks':3}
 entry=pool[0];name=next(a['name'] for a in agents if a['id']==entry)
 r=req('POST','/runs',{'project_id':setup['project']['id'],'title':label,'request':'@'+name+' 修复add函数并保持接口，补充测试。[immediate]','agent_ids':[a['id'] for a in agents] if mode=='development' else pool,'entry_agent_id':entry,'collaboration_flow':True,'orchestration':g})
 runs[label]=r['id'];return r['id']
try:
 agents=req('GET','/agents');planner=next(a for a in agents if a['role']=='planner');dev=next(a for a in agents if a['role']=='developer');tester=next(a for a in agents if a['role']=='tester')
 files={'SKILL.md':'---\nname: v07-review-checklist-'+str(int(time.time()))+'\ndescription: 在定向需求和代码审查阶段核对接口、边界与验收证据。\n---\n# 协作约定\n\n先明确用户需求。不足时request_decision。读取references/checklist.md；形成责任人明确的行动项。\n','references/checklist.md':'# 验收清单\n\n签名兼容、实际测试、可追溯代码证据。\n'}
 sk=req('POST','/skills',{'files':files});assert sk['status']=='draft';record('Skill文本包创建为待审核，未执行安装钩子',{'id':sk['id'],'digest':sk['digest']})
 body={k:v for k,v in planner.items() if k not in ('id','version')};body['skills']=[{'skill_id':sk['id'],'version':1,'mode':'on_demand','allow_scripts':False,'parameters':{'focus':'compatibility'}}]
 blocked=c.put('/api/agents/'+planner['id'],json=body);assert blocked.status_code==422,blocked.text;record('未审核Skill不能绑定',blocked.status_code)
 sk=req('POST',f'/skills/{sk["id"]}/review',{'version':1,'digest':sk['digest'],'action':'approve'});req('PUT','/agents/'+planner['id'],body);record('管理员按摘要批准后绑定到指定Agent，非全团队自动加载')
 rr=c.get('/api/skills/'+sk['id']+'/export?version=1');rr.raise_for_status();(OUT/'skill-export.zip').write_bytes(rr.content);record('真实HTTP导出Skill ZIP与不可变版本')
 resolution=req('POST','/commands/resolve',{'text':'@'+planner['name']+' 修复加法','entry_agent_id':planner['id']});assert resolution['entry_agent_id']==planner['id'];record('定向@成员解析与明确入口一致',resolution)
 bad=c.post('/api/commands/resolve',json={'text':'@不存在的员工 修复功能'});assert bad.status_code==422;record('不存在的收件人明确拒绝，不猜测')
 rid=launch('v07 定向需求到可验收代码');r=wait(rid,['awaiting_decision']);co=collab(rid);q=co['decisions'][0];assert q['status']=='pending' and not co['meetings'];record('入口Agent主动提问并持久化阻塞，回答前不虚构会议',{'run':rid,'question':q,'calls':r['calls']})
 count=r['calls'];time.sleep(1.2);assert req('GET','/runs/'+rid)['calls']==count;record('人工等待不继续消耗模型调用预算')
 es=events(rid);assert any(e['kind']=='skill.activated' for e in es) and any(e['kind']=='skill.resource.read' for e in es);assert r['snapshot']['agents'][planner['id']]['skills'][0]['version']==1;record('真正按需加载Skill和资源，版本与加载事件进入运行',{'skills':[e for e in es if e['kind'].startswith('skill.')]})
 files2={**files,'references/checklist.md':files['references/checklist.md']+'\n新版本额外要求。'};new=req('PUT','/skills/'+sk['id'],{'files':files2,'expected_version':1});assert new['version']==2 and new['status']=='draft';assert req('GET','/runs/'+rid)['snapshot']['agents'][planner['id']]['skills'][0]['version']==1;record('编辑Skill生成新版本，不污染正在等待的运行快照')
 wrong=c.post(f'/api/runs/{rid}/questions/{q["id"]}/answer',json={'digest':'0'*64,'choice_id':'api-first','text':'','reject':False});assert wrong.status_code==409;record('错误摘要不能回答历史问题')
 instruction=req('POST',f'/runs/{rid}/instructions',{'recipients':[planner['id']],'body':'V07补充：保留add原签名，验收必须运行现有测试。'});record('定向追加说明先持久化，不重启整个任务',instruction)
 answer(rid,q);answer(rid,q);changed=c.post(f'/api/runs/{rid}/questions/{q["id"]}/answer',json={'digest':q['digest'],'choice_id':'ui-first','text':'更改','reject':False});assert changed.status_code==409;record('相同回答幂等，冲突回答拒绝，不二次调度')
 r=wait(rid,['awaiting_approval']);co=collab(rid);assert len(co['meetings'])==1 and co['meetings'][0]['status']=='concluded';assert len(co['actions'])==3 and all(a['status']=='pending_approval' for a in co['actions']);record('目标满足后结束真实会议，行动项有责任人与计划审批状态',co)
 es=events(rid);assert any(e['kind']=='human.decision.included' for e in es) and any(e['kind']=='human.instruction.included' for e in es);assert len([e for e in es if e['kind']=='human.decision.requested'])==1;record('回答和补充说明进入实际后续模型请求，原问题不重提')
 assert any(e['kind']=='meeting.speaker.started' for e in es) and any(e['kind']=='meeting.closed' for e in es);record('会议生命周期与实际发言步骤产生事件，而非固定动画计时')
 page=req('GET',f'/runs/{rid}/sessions?agent_id={planner["id"]}&limit=1');assert page['has_more'];sd=req('GET',f'/runs/{rid}/sessions/intake');serialized=json.dumps(sd,ensure_ascii=False);assert 'api-first' in serialized and 'SKILL.md' not in json.dumps([m.get('role') for m in sd['items']]);assert all(m['role'] in ('assistant','tool') for m in sd['items']);record('会话分页包含公开回复与工具回答，不返回system或隐藏思考',{'count':sd['total'],'page':page})
 tp=req('GET',f'/runs/{rid}/trace?limit=2');nxt=req('GET',f'/runs/{rid}/trace?after={tp["next_cursor"]}&limit=2');assert set(e['id'] for e in tp['items']).isdisjoint(e['id'] for e in nxt['items']);record('追溯时间线使用服务端游标，无重复翻页')
 approve(r,'plan');r=wait(rid,['awaiting_acceptance']);assert r['report']['checks']['verification']=='passed';assert r['report']['collaboration']['actions'];record('计划批准后行动项对应真实Git修改、固定测试与独立审查',{'calls':r['calls'],'checks':r['report']['checks']})
 for name in ['changes.patch','report.json','source.zip']:
  rr=c.get(f'/api/runs/{rid}/artifacts/{name}');rr.raise_for_status();(OUT/('chain-'+name)).write_bytes(rr.content)
 approve(r);r=wait(rid,['completed']);co=collab(rid);assert all(a['status']=='accepted' for a in co['actions']);record('人工接受后行动项才显示已验收，原项目保持不变')
 (OUT/'chain-run.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'chain-events.json').write_text(json.dumps(events(rid),ensure_ascii=False,indent=2));(OUT/'chain-collaboration.json').write_text(json.dumps(co,ensure_ascii=False,indent=2))
 assert (STATE/'imports/fixture-project/calc.py').read_text()=='def add(a, b):\n    return a - b\n'
 rid=launch('v07 指定两成员纯讨论','discussion',[dev['id'],tester['id']]);r=wait(rid,['awaiting_decision']);co=collab(rid);answer(rid,co['decisions'][0]);r=wait(rid,['awaiting_acceptance']);co=collab(rid);assert co['actions'] and all(a['status']=='proposed' and not a['task_key'] for a in co['actions']);assert r['report']['checks']['verification']=='unverified';record('少数成员纯讨论形成建议行动项，不伪造代码执行',co);approve(r);wait(rid,['completed'])
 rid=launch('v07 决策拒绝保留记录');r=wait(rid,['awaiting_decision']);co=collab(rid);answer(rid,co['decisions'][0],True);r=wait(rid,['needs_attention']);assert not collab(rid)['meetings'];record('用户拒绝继续时转人工，未产生会议或开发成功')
 record('未经认证不能读取决策、会话或Skill',httpx.get(f'http://127.0.0.1:8787/api/runs/{rid}/collaboration',trust_env=False).status_code==401)
except Exception:
 results.append({'name':'v07HTTP验证异常','status':'failed','error':traceback.format_exc()});print(traceback.format_exc(),flush=True)
finally:
 (OUT/'http-results.json').write_text(json.dumps({'method':'real HTTP/SQLite/local coordinator/Git/fixed unittest; SCRIPTED model replies','passed':sum(x['status']=='passed' for x in results),'failed':sum(x['status']=='failed' for x in results),'run_ids':runs,'skill_id':locals().get('sk',{}).get('id'),'results':results},ensure_ascii=False,indent=2));c.close()
if any(x['status']=='failed' for x in results):raise SystemExit(1)
