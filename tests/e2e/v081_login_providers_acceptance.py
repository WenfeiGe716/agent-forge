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
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v081';OUT.mkdir(exist_ok=True)
if (ROOT/'.local').exists():raise SystemExit('Requires fresh checkout; never resets an existing deployment.')
PORT=8797;MODEL_PORT=8098
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
 process=start();config=json.loads((ROOT/'.local/config.json').read_text());assert get('/health')['version']=='0.8.1';record('正式单命令启动v0.8.1，无预设供应商或项目')
 assert transport.client.get('/api/agents').status_code==401;record('未登录的模型/Agent接口保持鉴权')
 with sync_playwright() as pw:
  browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
  def new_page(width=1440,height=960):
   p=browser.new_page(viewport={'width':width,'height':height});p.set_default_timeout(25000);p.on('pageerror',lambda e:errors.append(str(e)));transport.attach(p,(ROOT/'frontend/dist/index.html').read_text());return p
  def login(username='admin',password=None):
   expect(page.get_by_test_id('login-page')).to_be_visible();page.get_by_label('用户名',exact=True).fill(username);page.get_by_label('密码',exact=True).fill(password or config['ADMIN_PASSWORD']);page.get_by_role('button',name='登录工作空间',exact=True).click();expect(page.get_by_test_id('studio-workspace')).to_be_visible()
  def go_models():
   if page.get_by_test_id('studio-workspace').count():page.locator('.studio-header').get_by_role('button',name='模型',exact=True).click()
   else:page.locator('.sidebar nav').get_by_role('button',name='供应商与模型',exact=True).click()
   expect(page.get_by_test_id('provider-management')).to_be_visible()
  def save_provider(name,protocol):
   page.get_by_role('button',name='添加供应商',exact=True).click();dialog=page.get_by_role('dialog');expect(dialog).to_be_visible()
   dialog.get_by_label('连接名称',exact=True).fill(name);dialog.get_by_label('协议适配器',exact=True).select_option(protocol)
   dialog.get_by_label('API基础地址').fill(f'http://127.0.0.1:{MODEL_PORT}/v1');dialog.get_by_label('这是外部模型服务',exact=False).uncheck()
   dialog.get_by_role('button',name='保存配置',exact=True).click();expect(dialog).not_to_be_visible()
   expect(page.locator('.provider-row').filter(has_text=name)).to_be_visible()
  def save_model(name,pid,model_id):
   page.get_by_role('button',name='添加模型',exact=True).click();d=page.get_by_role('dialog');d.get_by_label('显示名称',exact=True).fill(name)
   d.get_by_label('供应商连接',exact=True).select_option(pid);d.get_by_label('模型ID',exact=False).fill(model_id)
   d.get_by_role('button',name='保存配置',exact=True).click();expect(d).not_to_be_visible();expect(page.get_by_role('heading',name=name,exact=True)).to_be_visible()
  def bind_agent(name,pid,mid):
   card=page.locator('.agent-card').filter(has=page.get_by_role('heading',name=name,exact=True));card.get_by_role('button',name='配置成员',exact=False).click()
   d=page.get_by_role('dialog');d.get_by_label('模型供应商',exact=True).select_option(pid)
   assert d.get_by_label('独立模型绑定',exact=True).input_value()==''
   d.get_by_label('独立模型绑定',exact=True).select_option(mid);d.get_by_role('button',name='保存配置',exact=True).click();expect(d).not_to_be_visible()
  page=new_page()
  try:
   expect(page.get_by_test_id('login-page')).to_be_visible();assert page.get_by_label('API Key',exact=True).count()==0
   page.screenshot(path=str(OUT/'01-login.png'),full_page=True);record('未登录默认展示平台登录页，不出现API Key或供应商向导')
   page.get_by_label('密码',exact=True).fill('wrong-password');page.get_by_role('button',name='登录工作空间',exact=True).click();expect(page.locator('.login-card .form-error')).to_be_visible();assert page.get_by_test_id('studio-workspace').count()==0
   record('密码错误停留登录页，不进入工作台')
   login();expect(page.locator('.living-label')).to_have_count(4)
   assert get('/providers')==[] and get('/models')==[] and get('/runs')==[] and get('/projects')==[]
   assert page.get_by_test_id('provider-connector').count()==0 and page.get_by_test_id('provider-onboarding').count()==0
   page.screenshot(path=str(OUT/'02-office-no-providers.png'));record('零供应商登录后直接进入真实办公室，4名未绑定成员可见，无假任务或配置门禁')
   page.close();page=new_page();expect(page.get_by_test_id('studio-workspace')).to_be_visible();assert not requests
   record('有效会话刷新直接恢复办公室，不因零模型重新要求接入')
   go_models();assert get('/providers')==[]
   page.get_by_role('button',name='添加供应商',exact=True).click();page.get_by_role('dialog').get_by_role('button',name='关闭',exact=True).click()
   expect(page.get_by_test_id('provider-management')).to_be_visible();assert page.get_by_role('dialog').count()==0
   record('添加供应商是管理页内可关闭弹窗，关闭后留在列表')
   save_provider('供应商 A · 内网开发','openai_chat');save_provider('供应商 B · 内网审查','anthropic_messages')
   ps=get('/providers');assert len(ps)==2 and not requests and get('/models')==[]
   assert all(not a['model_profile_id'] for a in get('/agents'));record('实际页面添加两个协议的独立供应商，不自动调用模型或绑定全体成员')
   a=next(p for p in ps if p['name'].startswith('供应商 A'));b=next(p for p in ps if p['name'].startswith('供应商 B'))
   save_model('开发模型 A',a['id'],'fixture-code-a');save_model('审查模型 B',b['id'],'fixture-review-b');save_model('测试模型 A2',a['id'],'fixture-test-a2')
   ms=get('/models');ma=next(m for m in ms if m['name']=='开发模型 A');mb=next(m for m in ms if m['name']=='审查模型 B');ma2=next(m for m in ms if m['name']=='测试模型 A2')
   assert len(ms)==3 and not requests;record('一家供应商可保存多个模型，型号不会混入另一供应商')
   page.get_by_label('按供应商筛选模型',exact=True).select_option(a['id']);expect(page.locator('.card-grid .entity-card')).to_have_count(2)
   page.get_by_label('按供应商筛选模型',exact=True).select_option(b['id']);expect(page.locator('.card-grid .entity-card')).to_have_count(1)
   page.get_by_label('按供应商筛选模型',exact=True).select_option('');record('模型列表可按供应商筛选与恢复全部')
   page.get_by_role('button',name='为Agent分配供应商与模型',exact=True).click();expect(page.locator('.agent-card')).to_have_count(4)
   bind_agent('Atlas',a['id'],ma['id']);bind_agent('Nova',b['id'],mb['id']);bind_agent('Echo',a['id'],ma2['id'])
   agents=get('/agents');old_agents=agents
   assert next(x for x in agents if x['name']=='Atlas')['model_profile_id']==ma['id']
   assert next(x for x in agents if x['name']=='Nova')['model_profile_id']==mb['id']
   assert next(x for x in agents if x['name']=='Echo')['model_profile_id']==ma2['id']
   assert not next(x for x in agents if x['name']=='Sage')['model_profile_id']
   record('3位Agent分别绑定两家供应商和三个模型，未配置的第4位不影响管理')
   # Save a screenshot of real per-Agent supplier/model selectors.
   card=page.locator('.agent-card').filter(has=page.get_by_role('heading',name='Nova',exact=True));card.get_by_role('button',name='配置成员',exact=False).click()
   expect(page.get_by_label('模型供应商',exact=True)).to_have_value(b['id']);expect(page.get_by_label('独立模型绑定',exact=True)).to_have_value(mb['id'])
   page.screenshot(path=str(OUT/'04-agent-binding.png'));page.get_by_role('dialog').get_by_role('button',name='关闭',exact=True).click()
   go_models();page.screenshot(path=str(OUT/'03-multiple-providers.png'),full_page=True)
   page.get_by_role('button',name='连接并测试',exact=True).click();expect(page.get_by_test_id('provider-connector')).to_be_visible()
   page.get_by_label('供应商名称',exact=True).fill('协议检查连接 C · 测试服务');page.get_by_label('API 地址',exact=True).fill(f'http://127.0.0.1:{MODEL_PORT}/v1');page.get_by_label('这是本地或内网模型服务',exact=True).check()
   page.get_by_role('button',name='读取模型列表',exact=True).click();expect(page.get_by_test_id('provider-connector').get_by_role('status')).to_contain_text('读取到 1 个模型')
   assert page.get_by_label('本次添加的模型 ID',exact=True).input_value()=='' and not requests
   page.get_by_label('本次添加的模型 ID',exact=True).fill('fixture-onboarding-model');page.get_by_text('模型参数与可选初始化',exact=True).click()
   assert not page.get_by_label('仅绑定尚未配置模型的成员',exact=True).is_checked() and not page.get_by_label('初始化内置Skill',exact=True).is_checked()
   page.get_by_label('模型参数 JSON').fill('{bad-json');page.get_by_role('button',name='测试并保存此连接',exact=True).click();expect(page.get_by_role('alert')).to_be_visible();assert not requests
   page.get_by_label('模型参数 JSON').fill('{}');page.get_by_role('button',name='测试并保存此连接',exact=True).click()
   expect(page.get_by_test_id('connection-success')).to_be_visible();assert len(requests)==2 and get('/agents')==old_agents
   record('可选连接测试经过两次真实HTTP工具往返，默认不改变任何成员或Skill；错误JSON不触发请求')
   page.get_by_role('button',name='继续添加供应商',exact=True).click();expect(page.get_by_label('供应商名称',exact=True)).to_have_value('')
   assert get('/agents')==old_agents;page.get_by_role('dialog').get_by_role('button',name='关闭',exact=True).click()
   expect(page.locator('.provider-row')).to_have_count(3);record('完成后可继续添加，关闭后仍在管理列表且已保存记录不丢失')
   # No supplier is a global active choice; disable all without blocking app entry.
   for p in get('/providers'):
    res=transport.client.put('/api/providers/'+p['id'],json={k:p[k] for k in ('name','protocol','base_url','external')}|{'enabled':False},headers={'X-Forge-Request':'1'});res.raise_for_status()
   assert get('/setup/status')['needs_connection']
   page.get_by_role('button',name='退出登录',exact=True).click();expect(page.get_by_test_id('login-page')).to_be_visible();assert transport.client.get('/api/auth/me').status_code==401
   login();assert page.get_by_test_id('provider-connector').count()==0
   record('全部供应商停用后仍能重新登录办公室；退出时服务端会话失效')
   # Viewer account uses same login entrance; no configuration writes.
   res=transport.client.post('/api/users',json={'username':'readonly081','password':'read-only-test-1234','role':'viewer'},headers={'X-Forge-Request':'1'});res.raise_for_status()
   page.get_by_role('button',name='退出登录',exact=True).click();login('readonly081','read-only-test-1234');go_models()
   assert page.get_by_role('button',name='添加供应商',exact=True).count()==0 and page.get_by_role('button',name='连接并测试',exact=True).count()==0
   assert transport.client.post('/api/setup/connect',json={'name':'blocked'},headers={'X-Forge-Request':'1'}).status_code==403
   record('只读账号同样登录后进入办公室，可读列表但无供应商写入按钮或权限')
   page.get_by_role('button',name='退出登录',exact=True).click();login();page.close();old_key=config['ENCRYPTION_KEY'];stop(process);process=start()
   assert len(get('/providers'))==3 and get('/agents')==old_agents and json.loads((ROOT/'.local/config.json').read_text())['ENCRYPTION_KEY']==old_key
   page=new_page();expect(page.get_by_test_id('studio-workspace')).to_be_visible();assert page.get_by_test_id('provider-connector').count()==0
   record('实际服务重启后会话和多个供应商/独立绑定保持，仍直接恢复工作台')
   assert page.locator('.demo-control-bar,.preview-controls').count()==0 and not page.evaluate('Boolean(window.__forgeDemo)')
   assert transport.client.get('/workbench-preview.html').status_code==404;record('正式页面继续无演示控制、无示例注入器或预览路由')
   # Compact manager and login remain usable.
   go_models();page.set_viewport_size({'width':760,'height':960});page.wait_for_timeout(250)
   assert page.evaluate('document.documentElement.scrollWidth <= innerWidth');page.get_by_role('button',name='退出登录',exact=True).click()
   expect(page.get_by_test_id('login-page')).to_be_visible();assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
   record('760px视口下管理页与登录页无文档水平溢出')
   assert not errors;record('浏览器未捕获异常为0')
  except Exception:
   page.screenshot(path=str(OUT/'browser-failure.png'),full_page=True,timeout=10000);raise
  finally:browser.close()
except Exception:
 results.append({'name':'login/multi-provider e2e exception','status':'failed','error':traceback.format_exc()});print(traceback.format_exc(),flush=True)
finally:
 stop(process);fixture.shutdown();fixture.server_close();transport.shutdown();log.close()
 log_text=log_path.read_text();log_path.unlink();log_text=re.sub(r'首次登录密码（仅此终端显示一次）：[^\n]+','首次登录密码（已从报告移除）',log_text)
 (OUT/'launcher-output.txt').write_text(log_text)
 (OUT/'browser-results.json').write_text(json.dumps({'method':'real local launcher + API + browser DOM/WebGL through restricted HTTP bridge + scripted protocol probe. No real LLM/Docker or normal URL navigation claim.','passed':sum(r['status']=='passed' for r in results),'failed':sum(r['status']=='failed' for r in results),'pageerrors':errors,'model_requests':requests,'results':results},ensure_ascii=False,indent=2))
print(json.dumps({'passed':sum(r['status']=='passed' for r in results),'failed':sum(r['status']=='failed' for r in results)}))
if any(r['status']=='failed' for r in results):raise SystemExit(1)
