"""Extract a release into a new temporary directory; run its documented launcher.
Requires this process to have httpx. No existing deployment is touched.
"""
import hashlib,json,os,signal,subprocess,sys,tempfile,time,zipfile
from pathlib import Path
import httpx
archive=Path(sys.argv[1]).resolve();out=Path(sys.argv[2]).resolve();temp=Path(tempfile.mkdtemp(prefix='forge-store-release-'));checks=[];process=None
with zipfile.ZipFile(archive) as z:
 for i in z.infolist():
  target=(temp/i.filename).resolve()
  if not target.is_relative_to(temp):raise ValueError('Unsafe archive path')
 z.extractall(temp)
root=temp/'agent-forge';assert not (root/'.local').exists() and not (root/'.venv').exists()
log=temp/'private-launcher.log';client=httpx.Client(base_url='http://127.0.0.1:8798',trust_env=False,timeout=8)
try:
 with log.open('w') as f:
  process=subprocess.Popen(['bash','start.sh','--port','8798','--no-auto-sandbox'],cwd=root,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
 for _ in range(250):
  try:
   if client.get('/api/health').status_code==200:break
  except httpx.HTTPError:pass
  if process.poll() is not None:raise RuntimeError('Launcher exited; see local-only log '+str(log))
  time.sleep(.2)
 else:raise TimeoutError('Release startup')
 assert (root/'.venv/bin/python').exists();checks.append('New archive automatically installs offline dependencies and starts')
 assert client.get('/api/health').json()['version']=='0.10.1';checks.append('Actual API reports 0.10.1')
 assert client.get('/api/skill-store/items').status_code==401;checks.append('Skill Store requires authentication')
 config=json.loads((root/'.local/config.json').read_text())
 response=client.post('/api/auth/login',json={'username':'admin','password':config['ADMIN_PASSWORD']},headers={'X-Forge-Request':'1'});response.raise_for_status();checks.append('Generated administrator login works')
 items=client.get('/api/skill-store/items').json();assert items['total']==3;checks.append('Three bundled skill entries available in installed archive')
 sources=client.get('/api/skill-store/sources').json();agency=next(s for s in sources if s['id']=='agency-agents');assert agency['count']==0 and not agency['last_sync'];checks.append('Upstream is honestly unsynced, not a fabricated roster')
 for path in ('skills','runs','models','providers','projects'):assert client.get('/api/'+path).json()==[]
 checks.append('No auto-install, binding, model or fictional business data')
 assert client.get('/').content==(root/'frontend/dist/index.html').read_bytes();checks.append('Server provides exact built production frontend')
 code_paths=['backend/forge/api.py','backend/forge/skill_store.py','backend/forge/skill_store_api.py','frontend/dist/index.html','start.sh','scripts/start-local.py']
 out.parent.mkdir(parents=True,exist_ok=True);out.write_text(json.dumps({'passed':len(checks),'failed':0,'checks':checks,'tested_archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'runtime_file_hashes':{p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in code_paths},'method':'Actual bash start.sh from a fresh extracted ZIP; no existing deployment or model calls'},ensure_ascii=False,indent=2))
 print(json.dumps({'passed':len(checks),'failed':0}))
finally:
 if process and process.poll() is None:
  os.killpg(process.pid,signal.SIGTERM)
  try:process.wait(timeout=20)
  except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()
 client.close()
 # Temp directory contains login keys: remove, do not publish.
 import shutil
 shutil.rmtree(temp)
