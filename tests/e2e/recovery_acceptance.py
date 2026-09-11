"""Actual process stop/restart at approval boundary; separate DB, real HTTP fixtures."""
import json,os,secrets,shutil,subprocess,sys,tempfile,time
from pathlib import Path
import httpx
from cryptography.fernet import Fernet
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence';STATE=Path(tempfile.mkdtemp(prefix='forge-recovery-'));api=None;checks=[]
source=STATE/'imports'/'project';(source/'tests').mkdir(parents=True);(source/'calc.py').write_text('def add(a, b):\n    return a - b\n')
(source/'tests/test_calc.py').write_text('import unittest\nfrom calc import add\nclass TestCalc(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2,3),5)\n')
env={**os.environ,'DATABASE_URL':'sqlite:///'+str(STATE/'forge.db'),'DATA_ROOT':str(STATE/'data'),'IMPORT_ROOT':str(STATE/'imports'),'ENCRYPTION_KEY':Fernet.generate_key().decode(),'ADMIN_PASSWORD':secrets.token_urlsafe(20),'SANDBOX_TOKEN':secrets.token_urlsafe(24),'ORCHESTRATOR':'local','FORGE_VALIDATION_MODE':'true','DISABLE_DISPATCH':'false','PYTHONPATH':str(ROOT/'backend'),'FRONTEND_DIR':str(ROOT/'frontend/dist')}
BASE='http://127.0.0.1:8790'
def start():
    global api
    api=subprocess.Popen([sys.executable,'-m','uvicorn','forge.api:app','--host','127.0.0.1','--port','8790'],env=env,stdout=open(STATE/'api.log','a'),stderr=subprocess.STDOUT)
    for _ in range(80):
        try:
            if httpx.get(BASE+'/api/health',trust_env=False).status_code==200:return
        except httpx.HTTPError:pass
        time.sleep(.1)
    raise RuntimeError('API did not start')
def wait(c,rid,state):
    for _ in range(120):
        d=c.get('/api/runs/'+rid).json()
        if d['status']==state:return d
        if d['status']=='needs_attention':raise RuntimeError(d['error'])
        time.sleep(.1)
    raise RuntimeError(str(d))
try:
    start();c=httpx.Client(base_url=BASE,headers={'X-Forge-Request':'1'},trust_env=False,timeout=15)
    assert httpx.get(BASE+'/api/runs',trust_env=False).status_code==401;checks.append('unauthenticated requests rejected')
    c.post('/api/auth/login',json={'username':'admin','password':env['ADMIN_PASSWORD']}).raise_for_status()
    r=c.post('/api/providers',json={'name':'recovery-fixture','protocol':'openai_chat','base_url':'http://127.0.0.1:8099/openai/v1','external':False});r.raise_for_status();pid=r.json()['id']
    r=c.post('/api/models',json={'name':'recovery-fixture','provider_id':pid,'model_id':'fixture-openai','allowed_parameters':['max_tokens'],'parameters':{'max_tokens':2048}});r.raise_for_status();mid=r.json()['id']
    agents=c.get('/api/agents').json()
    for a in agents:
        body={k:v for k,v in a.items() if k not in ['id','version']};body['model_profile_id']=mid;c.put('/api/agents/'+a['id'],json=body).raise_for_status()
    r=c.post('/api/projects',json={'name':'recovery-project','import_path':'project'});r.raise_for_status();project=r.json()['id']
    body={'project_id':project,'agent_ids':[a['id'] for a in agents],'title':'重启恢复验收','request':'修复加法并补充文档与测试。'}
    r=c.post('/api/runs',json=body);r.raise_for_status();rid=r.json()['id'];d=wait(c,rid,'awaiting_approval');before=d['calls'];digest=d['plan_digest'];assert before==3
    api.terminate();api.wait(timeout=12);start();time.sleep(.8)
    d=wait(c,rid,'awaiting_approval');assert d['calls']==before and d['plan_digest']==digest;checks.append('process restart reuses completed activity journal; no repeat model calls')
    assert c.post(f'/api/runs/{rid}/decision/plan',json={'digest':'stale','approved':True}).status_code==409;checks.append('stale approval rejected after restart')
    c.post(f'/api/runs/{rid}/decision/plan',json={'digest':digest,'approved':False}).raise_for_status();d=wait(c,rid,'cancelled');assert d['calls']==before;checks.append('durable rejection cancels without starting developers')
    # Global call cap, including planning, enforced by real HTTP execution.
    r=c.post('/api/runs',json={**body,'title':'调用预算限制验收','runtime':{'max_model_calls':1}});r.raise_for_status();budget=r.json()['id']
    for _ in range(100):
        d=c.get('/api/runs/'+budget).json()
        if d['status']=='needs_attention':break
        time.sleep(.1)
    assert d['status']=='needs_attention' and d['calls']==1 and '调用次数上限' in d['error'];checks.append('model call hard cap stops run at exactly one call')
    # Native SSE on the backend transport: reconnect cursor yields only later IDs.
    events=c.get(f'/api/runs/{rid}/events?limit=1000').json()['events'];cursor=events[-3]['id'];received=[]
    with c.stream('GET',f'/api/runs/{rid}/stream',headers={'Last-Event-ID':str(cursor)}) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if line.startswith('id: '):received.append(int(line[4:]))
            if len(received)==2:break
    assert received==[e['id'] for e in events[-2:]];checks.append('real HTTP SSE Last-Event-ID resumes without duplicate prior events')
    report={'passed':len(checks),'failed':0,'checks':checks,'restart_boundary':'awaiting_approval; completed operations only','model_calls_before_restart':before,'model_calls_after_restart':before,'temporal_tested':False,'docker_tested':False}
except Exception as e:
    report={'passed':len(checks),'failed':1,'checks':checks,'error':repr(e)};raise
finally:
    if api is not None and api.poll() is None:api.terminate();api.wait(timeout=12)
    (OUT/'recovery-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
