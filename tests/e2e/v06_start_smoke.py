"""Smoke-check real start-local.py in a fresh checkout; never replace existing .local/."""
from pathlib import Path
import subprocess,time,json,hashlib,socket
import httpx
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v06';results=[]
if (ROOT/'.local').exists():raise SystemExit('本检查需要未初始化的源码副本；不会清理或覆盖现有.local。')
port=8793
with socket.socket() as probe:probe.bind(('127.0.0.1',port))
log=open(OUT/'normal-start-output.txt','w');process=subprocess.Popen([str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/start-local.py'),'--port',str(port)],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
def record(name,detail=True):results.append({'name':name,'status':'passed','detail':detail})
try:
 with httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,headers={'X-Forge-Request':'1'},timeout=10) as client:
  for _ in range(100):
   try:
    if client.get('/api/health').status_code==200:break
   except httpx.HTTPError:pass
   if process.poll() is not None:raise RuntimeError('服务启动失败，见日志')
   time.sleep(.1)
  health=client.get('/api/health').json();assert health['version']=='0.6.0';record('真实启动命令提供v0.6健康接口',health)
  r=client.get('/api/runs');assert r.status_code==401;record('未登录不能访问运行列表')
  config=json.loads((ROOT/'.local/config.json').read_text());client.post('/api/auth/login',json={'username':'admin','password':config['ADMIN_PASSWORD']}).raise_for_status();record('随机初始化管理员凭据可登录；凭据不写入报告')
  cfg=client.get('/api/settings').json();assert cfg['orchestrator']=='local' and cfg['validation_mode'] is False;record('使用本地编排且明确关闭脚本化验证模式')
  assert client.get('/api/providers').json()==[] and client.get('/api/models').json()==[];record('正常平台无预置虚假供应商和模型')
  r=client.get('/');r.raise_for_status();assert r.content==(ROOT/'frontend/dist/index.html').read_bytes();record('HTTP首页为最终构建产物的真实字节',{'sha256':hashlib.sha256(r.content).hexdigest()})
except Exception as e:
 results.append({'name':'启动验证异常','status':'failed','error':repr(e)})
finally:
 process.terminate()
 try:process.wait(timeout=10)
 except subprocess.TimeoutExpired:process.kill();process.wait()
 log.close();(OUT/'normal-start-results.json').write_text(json.dumps({'method':'actual subprocess + direct HTTP, no model or sandbox invocation','passed':sum(x['status']=='passed' for x in results),'failed':sum(x['status']=='failed' for x in results),'results':results},ensure_ascii=False,indent=2))
print(json.dumps({'passed':sum(x['status']=='passed' for x in results),'failed':sum(x['status']=='failed' for x in results)}))
if any(x['status']=='failed' for x in results):raise SystemExit(1)
