"""Start isolated verification services; never reads your .env, DB or projects."""
import json,os,secrets,subprocess,sys,time,socket
from pathlib import Path
from cryptography.fernet import Fernet
ROOT=Path(__file__).resolve().parents[2];STATE=Path(os.environ.get('FORGE_E2E_STATE','/tmp/agent-forge-e2e')).resolve();STATE.mkdir(parents=True,exist_ok=True)
if (STATE/'forge.db').exists() and '--reuse' not in sys.argv:raise SystemExit('验证目录已有数据；换FORGE_E2E_STATE或使用--reuse。不会自动删除。')
if (STATE/'environment.json').exists():env=json.loads((STATE/'environment.json').read_text())
else:
    env={'DATABASE_URL':'sqlite:///'+str(STATE/'forge.db'),'DATA_ROOT':str(STATE/'data'),'IMPORT_ROOT':str(STATE/'imports'),'ENCRYPTION_KEY':Fernet.generate_key().decode(),'ADMIN_PASSWORD':'fixture-'+secrets.token_hex(12),'SANDBOX_TOKEN':secrets.token_hex(24),'SANDBOX_URL':'http://127.0.0.1:8099','ORCHESTRATOR':'local','FORGE_VALIDATION_MODE':'true','ALLOWED_ORIGINS':'http://127.0.0.1:8787,http://localhost:8787','FIXTURE_LOG':str(STATE/'fixture-calls.jsonl'),'DISABLE_DISPATCH':'false','FRONTEND_DIR':str(ROOT/'frontend'/'dist')}
    p=STATE/'environment.json';p.write_text(json.dumps(env,indent=2));p.chmod(0o600)
source=STATE/'imports'/'fixture-project';(source/'tests').mkdir(parents=True,exist_ok=True)
(source/'calc.py').write_text('def add(a, b):\n    return a - b\n')
(source/'tests'/'test_calc.py').write_text('import unittest\nfrom calc import add\nclass TestCalc(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2,3),5)\n')
(source/'.env').write_text('FIXTURE_SECRET=must-not-be-imported\n')
env={**os.environ,**env,'PYTHONPATH':str(ROOT/'backend')+os.pathsep+str(ROOT/'tests'/'e2e')};processes=[]
for port in [8787,8099]:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        try:probe.bind(('127.0.0.1',port))
        except OSError:raise SystemExit(f'端口{port}已使用；拒绝连接其他服务作为验证后端。先停止冲突服务。')
try:
    for name,module,port in [('fixtures','fixture_services:app',8099),('api','forge.api:app',8787)]:
        p=subprocess.Popen([sys.executable,'-m','uvicorn',module,'--host','127.0.0.1','--port',str(port)],env=env,stdout=open(STATE/(name+'.log'),'a'),stderr=subprocess.STDOUT);processes.append(p)
    (STATE/'pids.json').write_text(json.dumps({n:p.pid for n,p in zip(['fixtures','api'],processes)}))
    print('VALIDATION ONLY http://127.0.0.1:8787; credentials: '+str(STATE/'environment.json'),flush=True)
    if '--seed' in sys.argv and not (STATE/'setup.json').exists():
        import httpx
        for _ in range(60):
            try:
                if httpx.get('http://127.0.0.1:8787/api/health',trust_env=False).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.25)
        subprocess.run([sys.executable,str(ROOT/'tests/e2e/seed.py')],env={**env,'FORGE_E2E_STATE':str(STATE)},check=True)
    while all(p.poll() is None for p in processes):time.sleep(.5)
finally:
    for p in processes:p.terminate()
    for p in processes:
        try:p.wait(timeout=10)
        except subprocess.TimeoutExpired:p.kill();p.wait()
