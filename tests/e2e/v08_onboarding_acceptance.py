"""Actual launcher + API + browser DOM/WebGL + local scripted protocol service.
Browser is loaded by the existing disclosed HTTP bridge; no policy bypass.
No real LLM or Docker/runsc execution is claimed. Only isolated new .local/ is used.
"""
import json,os,time,threading,subprocess,traceback,re,socket
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
import httpx
from playwright.sync_api import sync_playwright,expect
from browser_transport import LocalTransport
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v08';OUT.mkdir(exist_ok=True)
if (ROOT/'.local').exists():raise SystemExit('Requires fresh checkout; never resets an existing deployment.')
PORT=8795;MODEL_PORT=8097
for port in [PORT,MODEL_PORT]:
 with socket.socket() as sock:
  sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);sock.bind(('127.0.0.1',port))
requests=[];results=[];errors=[]
def record(name,detail=True):results.append({'name':name,'status':'passed','detail':detail});print('PASS',name,flush=True)
class ModelService(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def send_json(self,obj,status=200):
  data=json.dumps(obj).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):
  if self.path!='/v1/models':return self.send_json({'error':'unknown'},404)
  self.send_json({'data':[{'id':'fixture-onboarding-model','display_name':'Protocol fixture model'}]})
 def do_POST(self):
  data=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append({'path':self.path,'model':data.get('model'),'has_tools':bool(data.get('tools'))})
  # A protocol test fixture, not a model pretending to do project work.
  if data.get('tools'):
   assert 'forge_connection_check' in json.dumps(data['tools'])
   nonce=re.search(r'nonce ([a-f0-9]+)',json.dumps(data)).group(1)
   if self.path.endswith('/messages'):
    response={'content':[{'type':'tool_use','id':'probe-tool','name':'forge_connection_check','input':{'nonce':nonce}}],'usage':{'input_tokens':10,'output_tokens':3}}
   else:
    response={'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{'id':'probe-tool','type':'function','function':{'name':'forge_connection_check','arguments':json.dumps({'nonce':nonce})}}]}}],'usage':{'prompt_tokens':10,'completion_tokens':3}}
  else:
   if self.path.endswith('/messages'):response={'content':[{'type':'text','text':'OK'}],'usage':{'input_tokens':4,'output_tokens':1}}
   else:response={'choices':[{'message':{'role':'assistant','content':'OK'}}],'usage':{'prompt_tokens':4,'completion_tokens':1}}
  self.send_json(response)
fixture=ThreadingHTTPServer(('127.0.0.1',MODEL_PORT),ModelService);threading.Thread(target=fixture.serve_forever,daemon=True).start()
log_path=OUT/'launcher-raw.tmp';log=open(log_path,'w');process=None;transport=LocalTransport(f'http://127.0.0.1:{PORT}')
def start():
 p=subprocess.Popen(['bash','start.sh','--port',str(PORT),'--no-auto-sandbox'],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
 for _ in range(100):
  try:
   if transport.client.get('/api/health').status_code==200:return p
  except httpx.HTTPError:pass
  if p.poll() is not None:raise RuntimeError('Launcher failed')
  time.sleep(.15)
 raise TimeoutError('API startup')
def stop(p):
 if p and p.poll() is None:
  p.terminate()
  try:p.wait(timeout=20)
  except subprocess.TimeoutExpired:p.kill();p.wait()
def get(path):
 r=transport.client.get('/api'+path);r.raise_for_status();return r.json()
try:
 process=start();config=json.loads((ROOT/'.local/config.json').read_text());assert get('/health')['version']=='0.8.0';record('单命令启动正式服务，配置与随机密钥自动生成')
 assert transport.client.get('/api/agents').status_code==401;record('默认保留登录保护，不开放未认证供应商管理')
 with sync_playwright() as pw:
  browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
  page=browser.new_page(viewport={'width':1440,'height':1080});page.set_default_timeout(25000);page.on('pageerror',lambda e:errors.append(str(e)))
  try:
   transport.attach(page,(ROOT/'frontend/dist/index.html').read_text())
   page.get_by_label('密码',exact=True).fill(config['ADMIN_PASSWORD']);page.get_by_role('button',name='登录工作空间',exact=True).click()
   expect(page.get_by_test_id('provider-onboarding')).to_be_visible();assert get('/providers')==[] and get('/models')==[] and get('/projects')==[] and get('/runs')==[]
   record('新实例自动进入供应商接入页，无示例供应商、项目或任务')
   page.screenshot(path=str(OUT/'01-provider-setup.png'),full_page=True)
   assert page.locator('.demo-control-bar,.preview-controls').count()==0
   assert not page.evaluate('Boolean(window.__forgeDemo)');assert page.get_by_text('示例散会 / 推进下一阶段',exact=True).count()==0
   record('正式DOM无演示按钮、没有演示数据注入器')
   page.get_by_label('供应商名称',exact=True).fill('浏览器验收 · 本地协议测试服务')
   page.get_by_label('API 地址').fill(f'http://127.0.0.1:{MODEL_PORT}/v1/chat/completions')
   page.get_by_label('API Key').fill('temporary-test-credential')
   page.get_by_label('这是本地或内网模型服务',exact=True).check()
   page.get_by_role('button',name='读取模型列表',exact=True).click();expect(page.get_by_role('status')).to_contain_text('读取到 1 个模型')
   assert page.get_by_label('默认模型 ID',exact=True).input_value()=='' and len(requests)==0;record('模型目录经真实HTTP读取，不擅自挑选型号或触发付费调用')
   page.get_by_label('默认模型 ID',exact=True).fill('fixture-onboarding-model')
   page.get_by_text('高级参数与内置能力',exact=True).click();page.get_by_label('模型参数 JSON').fill('{bad json')
   page.get_by_role('button',name='连接、检查并初始化',exact=True).click();expect(page.get_by_role('alert')).to_be_visible();assert not requests;record('错误JSON由界面拦截，未发送模型请求')
   page.get_by_label('模型参数 JSON').fill('{}');page.get_by_role('button',name='连接、检查并初始化',exact=True).click()
   expect(page.get_by_test_id('connection-success')).to_be_visible();assert len(requests)==2
   agents=get('/agents');assert len(agents)==4 and all(a['model_profile_id'] and a['skills'] for a in agents)
   assert len(get('/skills'))==3 and all(s['status']=='approved' for s in get('/skills'));assert get('/runs')==[]
   record('两次实际HTTP完成工具往返，连接、模型、4位成员、3个内置Skill持久化')
   page.screenshot(path=str(OUT/'02-provider-connected.png'),full_page=True)
   assert page.get_by_label('API Key').count()==0;assert 'temporary-test-credential' not in json.dumps(get('/providers'))
   assert all(not b['allow_scripts'] for a in agents for b in a['skills']);record('接入成功清空页面密钥，返回值不回显，Skill不自动获得脚本权限')
   page.get_by_role('button',name='进入正式工作台',exact=True).click();expect(page.get_by_test_id('studio-workspace')).to_be_visible();expect(page.locator('.living-label')).to_have_count(4)
   assert page.locator('.demo-control-bar,.preview-controls').count()==0;assert get('/runs')==[]
   page.wait_for_timeout(1200);page.screenshot(path=str(OUT/'03-production-office.png'))
   record('进入4名真实配置成员的3D工作台，没有虚构在途任务或人工推进按钮')
   page.locator('.studio-header').get_by_role('button',name='Skill',exact=True).click();expect(page.locator('.card-grid .entity-card')).to_have_count(3)
   record('内置Skill在正式管理页可见，可沿用原有版本和审核管理')
   # Reload the actual build through the same authenticated HTTP bridge.
   page.close();page=browser.new_page(viewport={'width':1440,'height':1000});page.on('pageerror',lambda e:errors.append(str(e)))
   transport.attach(page,(ROOT/'frontend/dist/index.html').read_text());expect(page.get_by_test_id('studio-workspace')).to_be_visible();assert page.get_by_test_id('provider-onboarding').count()==0;record('刷新后直接恢复正式办公室，不重复显示首次向导')
   old_agents=get('/agents');old_skills=get('/skills');old_key=config['ENCRYPTION_KEY'];page.close();stop(process);process=start()
   # Cookies and persistent database survive the actual child process restart.
   assert get('/agents')==old_agents and get('/skills')==old_skills
   assert json.loads((ROOT/'.local/config.json').read_text())['ENCRYPTION_KEY']==old_key
   assert get('/setup/status')['onboarding_completed'];record('真实停止与重启后供应商、成员、Skill、登录会话及密钥保持不变')
   page=browser.new_page(viewport={'width':1100,'height':850});page.set_default_timeout(20000);page.on('pageerror',lambda e:errors.append(str(e)));transport.attach(page,(ROOT/'frontend/dist/index.html').read_text());expect(page.get_by_test_id('studio-workspace')).to_be_visible();print('STEP second supplier form',flush=True)
   page.locator('.studio-header').get_by_role('button',name='模型',exact=True).click();page.get_by_role('button',name='添加供应商',exact=True).click();expect(page.get_by_test_id('provider-onboarding')).to_be_visible()
   page.get_by_label('供应商名称',exact=True).fill('第二连接 · Anthropic协议测试服务');page.get_by_label('接入协议',exact=True).select_option('anthropic_messages')
   page.get_by_label('API 地址').fill(f'http://127.0.0.1:{MODEL_PORT}/v1/messages');page.get_by_label('这是本地或内网模型服务',exact=True).check();page.get_by_label('默认模型 ID',exact=True).fill('fixture-second-model')
   page.get_by_role('button',name='连接、检查并初始化',exact=True).click();expect(page.get_by_test_id('connection-success')).to_be_visible()
   assert get('/agents')==old_agents and len(get('/providers'))==2 and len(requests)==4
   assert requests[-2]['path']=='/v1/messages';record('第二供应商使用另一协议完成工具往返，不覆盖已绑定成员及其Skill')
   assert page.get_by_text('自动更新成员',exact=False).count()>0
   page.set_viewport_size({'width':760,'height':1000});page.wait_for_timeout(200)
   assert page.evaluate('document.documentElement.scrollWidth <= innerWidth');page.screenshot(path=str(OUT/'04-mobile-setup.png'),full_page=True);record('760px宽度接入页无文档水平溢出')
   env=get('/setup/environment');assert env['sandbox']['state']!='ready';record('没有真实沙箱时明确显示未就绪，不用模型连接结果冒充执行环境',env)
   assert transport.client.get('/workbench-preview.html').status_code==404 and transport.client.get('/SuitePreview.js').status_code==404
   record('生产静态目录不提供旧预览入口及示例工厂脚本')
   assert not errors;record('实际浏览器操作未捕获异常为0')
  except Exception:
   page.screenshot(path=str(OUT/'browser-failure.png'),full_page=True,timeout=10000);raise
  finally:browser.close()
except Exception:
 results.append({'name':'onboarding e2e exception','status':'failed','error':traceback.format_exc()});print(traceback.format_exc(),flush=True)
finally:
 stop(process);fixture.shutdown();fixture.server_close();transport.shutdown();log.close()
 # The first-start admin password is shown only to the operator, never in evidence.
 log_text=log_path.read_text();log_path.unlink()
 log_text=re.sub(r'首次登录密码（仅此终端显示一次）：[^\n]+','首次登录密码（已从报告移除）',log_text)
 (OUT/'launcher-output.txt').write_text(log_text)
 (OUT/'browser-results.json').write_text(json.dumps({'method':'real local launcher + API + browser DOM/WebGL via bridge + scripted HTTP model protocol. No real LLM or Docker.','passed':sum(r['status']=='passed' for r in results),'failed':sum(r['status']=='failed' for r in results),'pageerrors':errors,'model_requests':requests,'results':results},ensure_ascii=False,indent=2))
print(json.dumps({'passed':sum(r['status']=='passed' for r in results),'failed':sum(r['status']=='failed' for r in results)}))
if any(r['status']=='failed' for r in results):raise SystemExit(1)
