"""Stop/restart the isolated fixture processes during a real pending question.
Uses existing seed and preserves database/encryption key. Only controls a verified
start_fixture.py process owned by this acceptance run; never a deployment process.
"""
from pathlib import Path
import os,sys,signal,subprocess,time,json,traceback,socket
import httpx
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v07'
STATE=Path(os.environ['FORGE_E2E_STATE']);PIDFILE=Path(os.environ['FORGE_FIXTURE_PIDFILE'])
env=json.loads((STATE/'environment.json').read_text());setup=json.loads((STATE/'setup.json').read_text());results=[]
c=httpx.Client(base_url='http://127.0.0.1:8787',trust_env=False,timeout=10,headers={'X-Forge-Request':'1'})
def call(m,path,b=None):
 r=c.request(m,'/api'+path,json=b);r.raise_for_status();return r.json()
def login():call('POST','/auth/login',{'username':'admin','password':env['ADMIN_PASSWORD']})
def wait(rid,states):
 for _ in range(900):
  r=call('GET','/runs/'+rid)
  if r['status'] in states:return r
  if r['status']=='needs_attention':raise RuntimeError(r['error'])
  time.sleep(.1)
 raise TimeoutError((rid,r['status']))
def rec(n,d=True):results.append({'name':n,'status':'passed','detail':d});print('PASS',n,flush=True)
def launch(title):
 agents=call('GET','/agents');a=next(a for a in agents if a['role']=='planner')
 return call('POST','/runs',{'project_id':setup['project']['id'],'title':title,'request':'@'+a['name']+' 修复add，先确认兼容性并运行现有测试。[immediate]','entry_agent_id':a['id'],'collaboration_flow':True,'agent_ids':[a['id'] for a in agents]})['id']
try:
 login();rid=launch('v07 人工决策等待期间真实进程重启');before=wait(rid,['awaiting_decision']);q=call('GET','/runs/'+rid+'/collaboration')['decisions'][0]
 rec('重启前问题真实持久化且运行等待用户',{'run_id':rid,'question_id':q['id'],'digest':q['digest'],'model_calls':before['calls']})
 pid=int(PIDFILE.read_text());cmd=Path('/proc/'+str(pid)+'/cmdline').read_bytes().replace(b'\0',b' ').decode();assert 'tests/e2e/start_fixture.py' in cmd, 'Refuse to stop unrelated process'
 os.kill(pid,signal.SIGINT)
 # launcher shutdown keeps SQLite and keys; both ports must be closed before reuse.
 for _ in range(100):
  try:c.get('/api/health');time.sleep(.1)
  except httpx.HTTPError:break
 time.sleep(1)
 for port in (8787,8099):
  for attempt in range(120):
   with socket.socket() as probe:
    probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    try:probe.bind(('127.0.0.1',port));break
    except OSError:time.sleep(.1)
  else:raise RuntimeError('旧验证服务未释放端口'+str(port))
 log=open(OUT/'restart-fixture-output.txt','w')
 p=subprocess.Popen([sys.executable,str(ROOT/'tests/e2e/start_fixture.py'),'--reuse'],cwd=ROOT,env={**os.environ,'FIXTURE_RESPONSE_DELAY':'.15'},stdout=log,stderr=subprocess.STDOUT);PIDFILE.write_text(str(p.pid))
 for _ in range(100):
  try:
   if c.get('/api/health').status_code==200:break
  except httpx.HTTPError:pass
  time.sleep(.1)
 login();after=wait(rid,['awaiting_decision']);qa=call('GET','/runs/'+rid+'/collaboration')['decisions'];assert len(qa)==1 and qa[0]['id']==q['id'] and qa[0]['digest']==q['digest'];assert after['calls']==before['calls'];rec('实际服务重启后恢复同一问题，已完成调用不重复',{'model_calls_before':before['calls'],'model_calls_after':after['calls']})
 call('POST','/runs/'+rid+'/control',{'action':'pause'});paused=wait(rid,['paused']);answer={'digest':q['digest'],'choice_id':'api-first','text':'保持函数签名并保留现有验收'};call('POST',f'/runs/{rid}/questions/{q["id"]}/answer',answer)
 time.sleep(1);current=call('GET','/runs/'+rid);assert current['calls']==before['calls'] and current['status']=='paused';rec('暂停时回答先保存，未恢复前不偷偷继续调用')
 call('POST','/runs/'+rid+'/control',{'action':'resume'});r=wait(rid,['awaiting_approval']);ev=call('GET','/runs/'+rid+'/events?limit=1000')['events'];assert sum(e['kind']=='human.decision.requested' for e in ev)==1;assert any(e['kind']=='human.decision.included' for e in ev);rec('恢复后从原步骤继续，答案进入实际上下文且问题只提出一次')
 call('POST','/runs/'+rid+'/control',{'action':'cancel'});wait(rid,['cancelled']);rec('未批准计划前取消，不启动开发')
 rid2=launch('v07 等待问题时取消');wait(rid2,['awaiting_decision']);q2=call('GET','/runs/'+rid2+'/collaboration')['decisions'][0];call('POST','/runs/'+rid2+'/control',{'action':'cancel'});wait(rid2,['cancelled']);saved=call('GET','/runs/'+rid2+'/collaboration')['decisions'][0];assert saved['status']=='cancelled' and not saved['answer'];rec('取消后未回答问题标记取消，保留原问题不自动选择')
 rejected=c.post(f'/api/runs/{rid2}/questions/{q2["id"]}/answer',json={'digest':q2['digest'],'choice_id':'api-first'});assert rejected.status_code==409;rec('已取消问题拒绝迟到回答，不恢复已关闭运行')
 (OUT/'restart-run-events.json').write_text(json.dumps(ev,ensure_ascii=False,indent=2))
except Exception:
 results.append({'name':'重启验收异常','status':'failed','error':traceback.format_exc()});print(traceback.format_exc(),flush=True)
finally:
 c.close();(OUT/'restart-results.json').write_text(json.dumps({'method':'actual controlled process termination/restart and HTTP, scripted models','passed':sum(x['status']=='passed' for x in results),'failed':sum(x['status']=='failed' for x in results),'results':results},ensure_ascii=False,indent=2))
if any(x['status']=='failed' for x in results):sys.exit(1)
