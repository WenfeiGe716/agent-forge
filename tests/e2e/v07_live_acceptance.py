"""Actual browser DOM/WebGL + actual authenticated HTTP using disclosed bridge.
The first scripted discussion reply is deliberately delayed for scene inspection.
No real LLM, browser-native URL/SSE/download or Docker coverage is claimed.
"""
from pathlib import Path
import json,os,time,traceback,re
from playwright.sync_api import sync_playwright,expect
from browser_transport import LocalTransport
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v07';STATE=Path(os.getenv('FORGE_E2E_STATE','/tmp/forge-v07-browser'));env=json.loads((STATE/'environment.json').read_text());results=[];errors=[];transport=LocalTransport()
def record(name,detail=True):results.append({'name':name,'status':'passed','detail':detail});print('PASS',name,flush=True)
def get(path):r=transport.client.get('/api'+path);r.raise_for_status();return r.json()
def waitstatus(rid,states,timeout=90):
 start=time.monotonic()
 while time.monotonic()-start<timeout:
  r=get('/runs/'+rid)
  if r['status'] in states:return r
  if r['status'] in ('needs_attention','cancelled'):raise AssertionError((r['status'],r['error']))
  time.sleep(.15)
 raise TimeoutError((rid,states,r['status']))
with sync_playwright() as pw:
 browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
 page=browser.new_page(viewport={'width':1600,'height':1000});page.set_default_timeout(25000);page.on('pageerror',lambda e:errors.append(str(e)))
 try:
  transport.attach(page,(ROOT/'frontend/dist/standalone.html').read_text());page.get_by_label('密码',exact=True).fill(env['ADMIN_PASSWORD']);page.get_by_role('button',name='登录工作空间',exact=True).click();expect(page.locator('.living-label')).to_have_count(4);assert page.locator('.demo-control-bar').count()==0;record('真实API登录与4位实际Agent，正式页面无示例控制条')
  page.locator('.studio-header').get_by_role('button',name='Skill',exact=True).click();page.get_by_role('button',name='创建Skill',exact=True).click();dialog=page.locator('dialog[open]');skill_name='browser-clarification-'+str(int(time.time()));text='---\nname: '+skill_name+'\ndescription: 明确接口需求与用户决策，再形成可追溯行动项。\n---\n# 需求澄清\n\n先读取references/checklist.md。信息不足时调用request_decision，不猜测用户决定。\n';dialog.get_by_label('Skill文件内容',exact=True).fill(text);dialog.get_by_role('button',name='保存待审核版本',exact=True).click();expect(dialog.get_by_role('button',name='批准此版本',exact=True)).to_be_visible();s=next(s for s in get('/skills') if s['name']==skill_name);assert s['status']=='draft';record('Skill编辑器保存真实SKILL.md与文本资源，新版本待审核')
  dialog.get_by_role('button',name='批准此版本',exact=True).click();expect(dialog.get_by_role('button',name='撤销此版本',exact=True)).to_be_visible();assert get('/skills/'+s['id'])['status']=='approved';page.screenshot(path=str(OUT/'01-skill-library.png'));record('按当前摘要审核后才允许绑定，内容可检查与导出')
  dialog.get_by_role('button',name='关闭',exact=True).click();nav=page.locator('.sidebar nav');nav.get_by_role('button',name=re.compile(r'^Agent团队')).click();card=page.locator('.agent-card').filter(has=page.get_by_role('heading',name='Atlas',exact=True));card.get_by_role('button',name='配置成员',exact=True).click();dialog=page.locator('dialog[open]');
  expect(dialog.get_by_label('绑定Skill '+skill_name,exact=True)).to_be_visible()
  for old in dialog.locator('input[aria-label^="绑定Skill "]').all():
   if old.is_checked():old.uncheck()
  dialog.get_by_label('绑定Skill '+skill_name,exact=True).check();dialog.get_by_label('成员部门',exact=True).fill('产品研发');dialog.get_by_label('通信范围',exact=True).select_option('run');dialog.get_by_role('button',name='保存配置',exact=True).click();expect(dialog).not_to_be_visible();planner=next(a for a in get('/agents') if a['name']=='Atlas');assert planner['skills'][0]['skill_id']==s['id'] and not planner['skills'][0]['allow_scripts'];record('成员表单实际绑定审核版本和按需模式，默认不授予脚本执行')
  nav.get_by_role('button',name='工作台',exact=True).click();page.get_by_label('指令收件人',exact=True).select_option(planner['id']);title='浏览器协作链 '+str(int(time.time()));page.get_by_label('开发需求',exact=True).fill('@Atlas 修复add，保持签名并补充边界测试。[immediate] SCENE_WALKTHROUGH_ONLY');page.get_by_role('button',name='创建开发任务',exact=True).click();dialog=page.locator('dialog[open]');dialog.get_by_label('任务名称',exact=True).fill(title);expect(dialog.get_by_label('启用定向协作链')).to_be_checked();dialog.get_by_role('button',name='核对@收件人',exact=True).click();expect(dialog).to_contain_text('entry_agent_id');page.screenshot(path=str(OUT/'02-directed-request.png'));record('办公室收件人与@指令进入创建表单，并由后端核对')
  dialog.get_by_role('button',name='创建任务',exact=True).click();expect(dialog).not_to_be_visible();expect(page.get_by_test_id('collaboration-workbench')).to_be_visible();r=next(r for r in get('/runs') if r['title']==title);rid=r['id'];r=waitstatus(rid,['awaiting_decision']);expect(page.locator('.decision-banner')).to_contain_text('问题需要你的决定');page.get_by_role('button',name='查看并回答',exact=True).click();qcard=page.get_by_test_id('decision-card');expect(qcard).to_contain_text('接口能力');page.screenshot(path=str(OUT/'03-human-decision.png'));record('Agent真实提问显示可选择选项、上下文及问题摘要')
  qcard.get_by_text('接口优先，保持现有函数签名',exact=True).click();qcard.get_by_label('决策补充说明').fill('保持当前函数签名，使用现有测试验收。');qcard.get_by_role('button',name='提交回答并继续',exact=True).click();expect(qcard).to_contain_text('已回答');record('通过浏览器提交决定，已回答状态写入后端')
  page.get_by_role('button',name='查看办公室',exact=True).click();expect(page.locator('.living-label')).to_have_count(4)
  # Wait for actual globally projected meeting participants to be seated; not injected labels.
  page.wait_for_function("()=>document.querySelectorAll('.living-label[data-meeting-id]:not([data-meeting-id=\"\"])[data-posture=seated]').length>=3",timeout=24000)
  positions=page.locator('.living-label').evaluate_all('(els)=>els.map(e=>({...e.dataset}))');page.screenshot(path=str(OUT/'04-real-meeting-office.png'));assert sum(bool(p.get('meetingId')) and p['posture']=='seated' for p in positions)>=3;record('真实会议状态驱动至少3名成员入座，业务运行不等待动画',positions)
  page.locator('.studio-header').get_by_role('button',name='协作链',exact=True).click();expect(page.get_by_test_id('collaboration-workbench')).to_be_visible();r=waitstatus(rid,['awaiting_approval']);page.get_by_role('button',name='协作与行动项',exact=True).click();expect(page.locator('.action-card')).to_have_count(3,timeout=20000);expect(page.locator('.meeting-card')).to_contain_text('会议已结束');page.screenshot(path=str(OUT/'05-action-workbench.png'));record('实际散会决议关联3个行动项和责任人，未批准时不显示完成')
  page.get_by_role('button',name='成员会话',exact=True).click();expect(page.locator('.session-workbench aside button').first).to_be_visible();page.locator('.session-workbench aside button').filter(has_text='定向需求').first.click();expect(page.locator('.session-message')).to_have_count(8);expect(page.locator('.session-workbench section')).to_contain_text('request_decision');expect(page.locator('.session-workbench section')).to_contain_text('activate_skill');page.screenshot(path=str(OUT/'06-session-skill-trace.png'));record('会话工作台展示真实Skill调用、用户回答回执和公开发言')
  page.get_by_role('button',name='追溯时间线',exact=True).click();page.get_by_label('会话搜索').fill('skill.');page.get_by_role('button',name='检索',exact=True).click();expect(page.locator('.trace-panel')).to_contain_text('skill.activated');expect(page.locator('.trace-panel')).to_contain_text('skill.resource.read');record('Skill来源可按事件检索，与同一运行和成员关联')
  page.get_by_role('button',name='审批 / 执行 / 验收',exact=True).click();page.get_by_role('button',name='批准并开始开发',exact=True).click();r=waitstatus(rid,['awaiting_acceptance']);page.get_by_placeholder('审批说明 / 人工验收意见').fill('已检查实际Git与固定测试证据。脚本化模型仅验证平台功能，不证明真实LLM能力。');page.get_by_role('button',name='验收通过',exact=True).click();waitstatus(rid,['completed']);record('从协作工作台进入真实计划审批与最终验收，开发链路保持可用')
  nav.get_by_role('button',name='任务看板',exact=True).click();page.get_by_label('搜索看板任务').fill(title);page.get_by_label('看板状态筛选').select_option('completed');expect(page.locator('.board-card')).to_have_count(1);page.locator('.board-card').click();expect(page.get_by_test_id('collaboration-workbench')).to_be_visible();expect(page.locator('.action-card').first).to_contain_text('已验收');record('看板卡片可回到同一决策、会议和行动项链路')
  page.set_viewport_size({'width':1100,'height':850});page.wait_for_timeout(400);assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');record('1100px下工作台没有文档水平溢出');page.set_viewport_size({'width':1600,'height':1000})
  assert not errors;record('全链路实际浏览器操作无未捕获异常');(OUT/'browser-run.json').write_text(json.dumps(get('/runs/'+rid),ensure_ascii=False,indent=2));(OUT/'browser-events.json').write_text(json.dumps(get('/runs/'+rid+'/events?limit=1000'),ensure_ascii=False,indent=2))
 except Exception:
  results.append({'name':'浏览器验收异常','status':'failed','error':traceback.format_exc()});print(traceback.format_exc(),flush=True);page.screenshot(path=str(OUT/'live-browser-failure.png'))
 finally:
  (OUT/'live-browser-results.json').write_text(json.dumps({'method':'actual DOM/WebGL + real HTTP bridge; scripted models; not browser-native navigation/SSE/download','passed':sum(r['status']=='passed' for r in results),'failed':sum(r['status']=='failed' for r in results),'pageerrors':errors,'results':results},ensure_ascii=False,indent=2));browser.close();transport.shutdown()
if any(r['status']=='failed' for r in results):raise SystemExit(1)
