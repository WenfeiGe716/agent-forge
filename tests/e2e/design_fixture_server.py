"""Temporary, isolated browser fixture server. No inference and no dispatch.

Keep running while inspecting the printed local URL; interrupt to clean up the
temporary DB and credentials. The generated password is test-only.
"""
import io, json, os, secrets, socket, subprocess, sys, tempfile, threading, time, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import httpx
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[2]

class Catalog(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        items = ([{'id':'fixture-other-only','name':'Other provider only'}] if self.path.startswith('/other/') else
                 [{'id':'fixture-coder','name':'Local fixture coder'}, {'id':'fixture-reviewer','name':'Local fixture reviewer'},
                  {'id':'fixture-embedding','name':'Local embedding'}])
        self.wfile.write(json.dumps({'data': items}).encode())
    def do_POST(self):
        self.send_error(500, 'Inference disabled in fixture')
    def log_message(self, *args):
        pass

with tempfile.TemporaryDirectory(prefix='forge-design-browser-') as state:
    state = Path(state)
    (state / 'imports' / 'fixture').mkdir(parents=True)
    (state / 'imports' / 'fixture' / 'README.md').write_text('Isolated test fixture.\n')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    catalog = ThreadingHTTPServer(('127.0.0.1', 0), Catalog)
    threading.Thread(target=catalog.serve_forever, daemon=True).start()
    password = 'isolated-fixture-' + secrets.token_hex(8)
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'backend'), 'DATABASE_URL': 'sqlite:///' + str(state / 'forge.db'),
           'DATA_ROOT': str(state / 'data'), 'IMPORT_ROOT': str(state / 'imports'), 'FRONTEND_DIR': str(ROOT / 'frontend/dist'),
           'ADMIN_PASSWORD': password, 'ENCRYPTION_KEY': Fernet.generate_key().decode(), 'DISABLE_DISPATCH': 'true',
           'FORGE_VALIDATION_MODE': 'false', 'ALLOWED_ORIGINS': f'http://localhost:{port},http://127.0.0.1:{port}', 'SECURE_COOKIE': 'false'}
    with (state / 'api.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'forge.api:app', '--host', '127.0.0.1', '--port', str(port)], env=env, cwd=state, stdout=log, stderr=subprocess.STDOUT)
        client = httpx.Client(base_url=f'http://127.0.0.1:{port}', headers={'X-Forge-Request':'1'}, trust_env=False)
        try:
            for _ in range(120):
                try:
                    if client.get('/api/health').status_code == 200: break
                except httpx.HTTPError: pass
                time.sleep(.15)
            def post(path, data):
                r = client.post('/api' + path, json=data)
                r.raise_for_status()
                return r.json()
            post('/auth/login', {'username':'admin','password':password})
            provider = post('/providers', {'name':'隔离验收 · 本地目录','protocol':'openai_chat','base_url':f'http://127.0.0.1:{catalog.server_port}/v1','external':False})
            other = post('/providers', {'name':'隔离验收 · 独立供应商','protocol':'openai_chat','base_url':f'http://127.0.0.1:{catalog.server_port}/other/v1','external':False})
            model = post('/models', {'name':'隔离验收模型 · 无推理','provider_id':provider['id'],'model_id':'fixture-coder','allowed_parameters':['max_tokens'],'parameters':{'max_tokens':2048}})
            post('/models', {'name':'待移出历史配置','provider_id':provider['id'],'model_id':'fixture-old','allowed_parameters':[],'parameters':{}})
            post('/models', {'name':'其他供应商独立配置','provider_id':other['id'],'model_id':'fixture-coder','allowed_parameters':[],'parameters':{}})
            project = post('/projects', {'name':'隔离验收项目','import_path':'fixture','check_commands':[]})
            source = post('/skill-store/sources', {'name':'隔离角色模板来源','kind':'archive'})
            archive = io.BytesIO()
            with zipfile.ZipFile(archive,'w') as bundle:
                bundle.writestr('roles/engineering/backend-architect.md', '---\nname: 后端架构师\ndescription: 隔离测试角色职责\n---\n# 后端架构师\n负责接口边界和服务设计；不自动执行任务。\n')
                bundle.writestr('roles/LICENSE', 'MIT License\nCopyright (c) Test fixture\n')
            uploaded = client.post('/api/skill-store/sources/' + source['id'] + '/archive', params={'expected_version':source['version']}, content=archive.getvalue(), headers={'Content-Type':'application/zip'})
            uploaded.raise_for_status()
            skill = post('/skills', {'files':{'SKILL.md':'---\nname: fixture-review\ndescription: 隔离验收的回归测试方法。\n---\n# 回归测试\n检查实际结果，输出复核清单。\n'}})
            post('/skills/' + skill['id'] + '/review', {'version':skill['version'],'digest':skill['digest'],'action':'approve'})
            post('/skills', {'files':{'SKILL.md':'---\nname: fixture-pending\ndescription: 尚未批准，不能绑定。\n---\n# 待审核方法\n此版本只作审核状态测试。\n'}})
            # Cookies are scoped by host, not port: keep browser fixture login
            # separate from the real deployment at 127.0.0.1.
            print(json.dumps({'url':f'http://localhost:{port}/','username':'admin','password':password,'model_id':model['id'],'project_id':project['id'],'state':str(state),'pid':process.pid},ensure_ascii=False), flush=True)
            while process.poll() is None: time.sleep(.3)
        finally:
            process.terminate()
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill();process.wait()
            client.close();catalog.shutdown()
