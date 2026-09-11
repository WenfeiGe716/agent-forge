"""Real browser and local HTTP projection tests; scripted model fixtures only.
The managed browser's navigation policies remain unchanged. See browser_transport.py.
"""
import json,os,time,traceback,hashlib
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from browser_transport import LocalTransport
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v04';STATE=Path(os.getenv('FORGE_E2E_STATE','/tmp/forge-v04-e2e'));os.environ.setdefault('DISPLAY',':0')
results=[];errors=[]
def record(name):
 results.append({'name':name,'status':'passed'});print('PASS',name,flush=True)
 (OUT/'browser-progress.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
def main():
 env=json.loads((STATE/'environment.json').read_text());transport=LocalTransport();c=transport.client;c.headers.update({'X-Forge-Request':'1'})
 with sync_playwright() as p:
  b=p.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
  page=b.new_page(viewport={'width':1600,'height':1000},device_scale_factor=1);page.set_default_timeout(20000);page.on('pageerror',lambda e:errors.append(str(e)))
  def shot(name):page.screenshot(path=str(OUT/(name+'.png')),timeout=30000)
  def wait_run(rid,status,seconds=60):
   until=time.monotonic()+seconds
   while time.monotonic()<until:
    data=c.get('/api/runs/'+rid).json()
    if data['status']==status:return data
    if data['status']=='needs_attention':raise AssertionError(data['error'])
    page.wait_for_timeout(150)
   raise AssertionError((status,data))
  try:
   transport.attach(page,(ROOT/'frontend/dist/standalone.html').read_text());page.get_by_label('密码',exact=True).fill(env['ADMIN_PASSWORD']);page.get_by_role('button',name='登录工作空间',exact=True).click()
   expect(page.locator('.studio-workspace')).to_be_visible();expect(page.locator('.living-label')).to_have_count(4);assert not page.locator('.scene-error').count();record('实际登录→全屏办公室→全局占用接口联动')
   assert c.get('/api/settings').json()['validation_mode'] is True, 'Refuse to mutate non-fixture data'
   for old in c.get('/api/runs').json():
    if old['status'] not in ['completed','cancelled','needs_attention']:
     c.post('/api/runs/'+old['id']+'/control',json={'action':'cancel'}).raise_for_status();wait_run(old['id'],'cancelled')
   c.put('/api/office/settings',json={'idle_grace_s':0,'idle_stagger_s':0,'idle_cooldown_s':0,'activities':['drink'],'durations_s':{'drink':25,'phone':15,'restroom':15,'nap':15,'stretch':8}}).raise_for_status()
   page.get_by_role('button',name='刷新',exact=True).click();page.wait_for_function("document.querySelectorAll('.living-label[data-pose=drink]').length>0",timeout=35000);shot('02-live-drinking');record('无任务的实际配置成员进入喝水，饮水机最多2个占位')
   labels=page.locator('.living-label').evaluate_all('es=>es.map(e=>({id:e.dataset.agentId,pose:e.dataset.pose}))');assert sum(x['pose']=='drink' for x in labels)<=2
   before=[r['calls'] for r in c.get('/api/runs').json()];page.wait_for_timeout(600);assert before==[r['calls'] for r in c.get('/api/runs').json()];record('空闲动作不产生模型调用或业务任务')
   page.locator('.studio-member').filter(has_text='Nova').click();expect(page.locator('.studio-inspector h2')).to_contain_text('Nova');page.locator('.inspector-tabs').get_by_role('button',name='模型',exact=True).click();page.get_by_label('成员最大执行步数').fill('17');page.locator('.inspector-body').get_by_role('button',name='保存配置',exact=True).click();page.wait_for_timeout(500);assert next(a for a in c.get('/api/agents').json() if a['name']=='Nova')['runtime']['max_steps']==17;record('右侧模型与运行参数编辑保留，真实API持久化')
   # Creating a real run reserves the entire selected team, even before its plan is approved.
   title='v0.4空闲抢占验收 '+str(int(time.time()));page.get_by_label('开发需求',exact=True).fill(title+'：修复加法，补充说明与边界测试。');page.get_by_role('button',name='创建开发任务',exact=True).click();expect(page.locator('dialog')).to_be_visible();page.get_by_role('button',name='创建真实开发任务',exact=True).click();expect(page.locator('dialog')).to_have_count(0)
   rid=next(r['id'] for r in c.get('/api/runs').json() if title in r['request']);wait_run(rid,'awaiting_approval');page.wait_for_function("[...document.querySelectorAll('.living-label')].every(e=>e.dataset.source==='business')",timeout=12000)
   assert not page.locator('.living-label[data-pose=drink],.living-label[data-pose=phone],.living-label[data-pose=nap],.living-label[data-pose=restroom]').count();shot('03-live-preempted');record('真实任务创建后，全队停止喝水/空闲；等待审批也不会摸鱼')
   page.get_by_role('button',name='任务看板',exact=True).click();expect(page.get_by_role('button',name='批准并开始开发',exact=True)).to_be_visible();page.get_by_role('button',name='批准并开始开发',exact=True).click();page.get_by_role('button',name='返回3D办公室',exact=True).click();record('办公室→批准计划→办公室，后台执行不等待角色回到工位')
   d=wait_run(rid,'awaiting_acceptance');ev=c.get('/api/runs/'+rid+'/events?limit=1000').json()['events'];assert len([e for e in ev if e['kind']=='agent.message'])==4;assert len([e for e in ev if e['kind']=='agent.message.delivered'])==4;assert d['report']['checks']['verification']=='passed';record('双协议脚本模型、4条真实工具消息、Git修改与固定单测仍通过')
   page.get_by_role('button',name='活动记录',exact=False).click();expect(page.locator('.studio-timeline details')).not_to_have_count(0);page.get_by_role('button',name='查看动作依据',exact=True).click();expect(page.locator('.studio-timeline')).to_contain_text('business');record('动作因果日志与后台事件分开，可查看真实占用依据')
   page.get_by_role('button',name='关闭活动记录',exact=True).click();page.get_by_role('button',name='任务看板',exact=True).click();page.get_by_placeholder('审批说明 / 人工验收意见').fill('核对实际Git与固定测试结果，场景不得替代验收。');page.get_by_role('button',name='验收通过',exact=True).click();wait_run(rid,'completed');page.get_by_role('button',name='返回3D办公室',exact=True).click();record('实际人工验收释放任务，空闲状态由后端重新确认')
   # One completed run is selected while another real run holds the same agents.
   setup=json.loads((STATE/'setup.json').read_text());r=c.post('/api/runs',json={'project_id':setup['project']['id'],'title':'另一个占用中的任务','request':'修复加法与边界测试','agent_ids':[a['id'] for a in setup['agents']]});r.raise_for_status();other=r.json()['id'];wait_run(other,'awaiting_approval');page.get_by_role('button',name='刷新',exact=True).click();page.wait_for_timeout(800);page.get_by_label('查看开发任务').select_option(rid);page.wait_for_function("[...document.querySelectorAll('.living-label')].every(e=>e.dataset.source==='business')",timeout=10000);record('查看已完成任务时，另一个运行仍保持所有相关Agent占用')
   # Read-only projection network fault: scene fails closed, not to idle.
   page.evaluate("() => { window.__savedFetch=window.fetch;window.fetch=(u,o)=>String(u).startsWith('/api/office')?Promise.reject(new Error('Injected projection outage')):window.__savedFetch(u,o); }")
   page.wait_for_function("[...document.querySelectorAll('.living-label')].every(e=>e.dataset.source==='safety')",timeout=10000);record('真实页面占用接口故障时停止空闲演绎，不伪造无任务状态');page.evaluate('() => { window.fetch=window.__savedFetch; }');page.get_by_role('button',name='刷新',exact=True).click();page.wait_for_function("[...document.querySelectorAll('.living-label')].every(e=>e.dataset.source==='business')",timeout=10000);record('网络恢复后重读快照，恢复真实任务动作')
   # Cancel this fixture-only waiting run through its actual control endpoint.
   c.post('/api/runs/'+other+'/control',json={'action':'cancel'}).raise_for_status()
   (OUT/'live-events.json').write_text(json.dumps(ev,ensure_ascii=False,indent=2));(OUT/'live-report.json').write_text(json.dumps(d['report'],ensure_ascii=False,indent=2))
   # Separate, clearly labelled preview tests richer population and specific poses.
   preview=b.new_page(viewport={'width':1600,'height':1000},device_scale_factor=1);preview.on('pageerror',lambda e:errors.append(str(e)));page=preview;page.set_default_timeout(20000);page.set_content((ROOT/'frontend/dist/office-preview.html').read_text(),wait_until='load');expect(page.locator('.living-label')).to_have_count(14);expect(page.locator('.connection')).to_contain_text('示例事件');record('14成员独立预览明确标为示例，不访问模型服务')
   page.wait_for_function("document.querySelector('.living-label[data-pose=phone]')&&document.querySelector('.living-label[data-pose=drink]')&&document.querySelector('.living-label[data-pose=nap]')",timeout=35000);shot('04-idle-activities');record('手机、喝水、小憩等姿态实际渲染，工作成员不参与')
   assert page.get_by_role('button',name='查看成员 Nova',exact=True).get_attribute('data-source')=='business';assert page.get_by_role('button',name='查看成员 Nova',exact=True).get_attribute('data-pose')=='typing'
   page.wait_for_function("document.querySelector('.living-label[data-pose=restroom]')",timeout=35000);wc=page.locator('.living-label[data-pose=restroom]').first;aid=wc.get_attribute('data-agent-id');name=wc.locator('b').inner_text();assert wc.get_attribute('data-visible')=='false';assert page.locator('.office-facility').inner_text()=='WC · 使用中';record('卫生间只有一个占位，人物进门后隐藏，门牌显示使用状态')
   page.locator('.studio-member').filter(has_text=name).click();page.get_by_role('button',name='给此成员分派示例任务',exact=True).click();target=page.locator(f'.living-label[data-agent-id="{aid}"]');expect(target).to_have_attribute('data-source','business');expect(target).to_have_attribute('data-visible','true');assert target.get_attribute('data-pose') not in ['restroom','phone','drink','nap'];shot('05-restroom-preempted');record('厕所在用时收到任务也会立即退出空闲，不等如厕计时结束')
   page.get_by_role('button',name='消息交接',exact=True).click();expect(page.locator('.office-speech:visible')).not_to_have_count(0);shot('06-event-message');record('只有明确消息输入才出现带事件ID的气泡和连线')
   page.get_by_role('button',name='断线保护',exact=True).click();page.wait_for_function("[...document.querySelectorAll('.living-label')].every(e=>e.dataset.source==='safety')");assert not page.locator('.office-speech:visible').count();page.get_by_role('button',name='恢复同步',exact=True).click();page.wait_for_function("[...document.querySelectorAll('.living-label')].some(e=>e.dataset.source==='business')");record('预览断线保护可复现，中断时清除对话效果')
   page.get_by_role('button',name='空闲策略',exact=True).click();page.get_by_label('启用空闲演绎',exact=True).uncheck();page.get_by_role('button',name='保存场景策略',exact=True).click();expect(page.locator('dialog')).to_have_count(0);page.wait_for_function("![...document.querySelectorAll('.living-label')].some(e=>['phone','drink','restroom','nap','stretch'].includes(e.dataset.pose))",timeout=10000);record('可关闭空闲演绎，已有动作终止；业务状态保持不变')
   page.get_by_role('button',name='切换日夜',exact=True).click();page.wait_for_timeout(400);shot('07-night');page.get_by_role('button',name='切换日夜',exact=True).click();page.locator('.studio-member').filter(has_text='Nova').click();page.get_by_role('button',name='聚焦成员',exact=True).click();page.wait_for_timeout(600);shot('08-focused');record('日夜、选人、平滑聚焦在新状态机下保持可用')
   page.get_by_role('button',name='恢复全景',exact=True).click();page.get_by_role('button',name='切换成员面板',exact=True).click();page.get_by_role('button',name='切换详情面板',exact=True).click();expect(page.locator('.studio-roster')).to_have_count(0);expect(page.locator('.studio-inspector')).to_have_count(0);record('成员和详情浮层可收起，不影响执行/空闲状态')
   page.set_viewport_size({'width':1100,'height':850});page.wait_for_timeout(300);assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');record('1100px视口无水平溢出')
   assert not errors,errors;record('上述交互无未捕获前端异常')
  except Exception as e:
   results.append({'name':'browser sequence','status':'failed','error':str(e),'traceback':traceback.format_exc()});print(traceback.format_exc());shot('failure')
  finally:
   report={'transport':'Real Chromium DOM/WebGL. Live tests use constrained local HTTP bridge, not normal URL navigation.','models':'Explicit scripted HTTP fixtures, not LLM quality verification.','results':results,'page_errors':errors};(OUT/'browser-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'passed':sum(x['status']=='passed' for x in results),'failed':sum(x['status']=='failed' for x in results),'errors':errors},ensure_ascii=False));b.close();transport.shutdown()
 if any(x['status']=='failed' for x in results):raise SystemExit(1)
if __name__=='__main__':main()
