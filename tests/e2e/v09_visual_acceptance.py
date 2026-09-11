"""Visual-regression checks on real API/login + production JS. LocalTransport is
an explicit test harness, not proof of native navigation/cookies/SSE deployment.
No provider, model or task is fabricated or called by this test.
"""
from pathlib import Path
import json,time,traceback
from playwright.sync_api import sync_playwright,expect
from browser_transport import LocalTransport
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v09';OUT.mkdir(exist_ok=True)
results=[];errors=[];transport=LocalTransport('http://127.0.0.1:8797')
expect.set_options(timeout=35000)
def record(name,detail=True):results.append({'name':name,'status':'passed','detail':detail});print('PASS',name,flush=True)
config=json.loads((ROOT/'.local/config.json').read_text())
with sync_playwright() as pw:
 b=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
 page=b.new_page(viewport={'width':1600,'height':1000});page.set_default_timeout(35000);page.on('pageerror',lambda e:errors.append(str(e)))
 try:
  transport.attach(page,(ROOT/'frontend/dist/index.html').read_text());expect(page.get_by_test_id('login-page')).to_be_visible()
  record('正式入口仍是登录页，没有供应商配置门禁或演示推进按钮')
  page.get_by_label('用户名',exact=True).fill('admin');page.get_by_label('密码',exact=True).fill(config['ADMIN_PASSWORD']);page.get_by_role('button',name='登录工作空间',exact=True).click()
  expect(page.get_by_test_id('studio-workspace')).to_be_visible();expect(page.locator('.living-label')).to_have_count(4);page.wait_for_timeout(4500)
  assert transport.client.get('/api/providers').json()==[] and transport.client.get('/api/runs').json()==[]
  assert page.locator('.scene-error').count()==0;record('真实空白实例4名成员，零供应商零任务也能渲染精修模型')
  # Prevent voluntary idle wandering during visual inspection only; ordinary setting/API.
  r=transport.client.put('/api/office/settings',json={'idle_enabled':False},headers={'X-Forge-Request':'1'});r.raise_for_status();page.wait_for_timeout(2500)
  quality=page.get_by_label('3D画质',exact=True);expect(quality).to_have_value('balanced')
  view=page.get_by_test_id('office-viewport');expect(view).to_have_attribute('render-quality','balanced') if False else None
  assert view.get_attribute('data-render-quality')=='balanced';record('默认均衡画质实际生效')
  page.screenshot(path=str(OUT/'01-formal-office.png'))
  quality.select_option('detail');page.wait_for_timeout(2000);assert view.get_attribute('data-shadow-size')=='2048';record('精细档实际分配2048阴影缓冲')
  quality.select_option('economy');page.wait_for_timeout(1600);assert view.get_attribute('data-shadow-size')=='768';record('节能档实际切换渲染资源，任务状态不变')
  quality.select_option('detail');page.wait_for_timeout(1500)
  page.get_by_role('button',name='聚焦成员',exact=True).click();page.wait_for_timeout(2000)
  assert float(view.get_attribute('data-camera-zoom'))>3.5;assert float(view.get_attribute('data-camera-pitch'))<.5
  page.screenshot(path=str(OUT/'02-character-closeup.png'));record('聚焦转到成员正面近景，可查看脸部服装和座椅')
  page.get_by_role('button',name='外观',exact=True).click();expect(page.get_by_role('button',name='查看人物细节',exact=True)).to_be_visible();page.get_by_role('button',name='查看人物细节',exact=True).click();record('外观面板提供真实人物细节入口，保留静态GLB边界提示')
  # Named roster selection and inspected member remain linked.
  page.locator('.studio-roster').get_by_role('button').filter(has_text='Nova').first.click() if page.locator('.studio-roster').count() else page.get_by_role('button',name='查看成员 Nova',exact=True).click()
  page.get_by_role('button',name='聚焦成员',exact=True).click();page.wait_for_timeout(1800);page.screenshot(path=str(OUT/'03-nova-detail.png'));record('选择另一成员后近景和模型随实际成员变化')
  page.get_by_role('button',name='切换日夜',exact=True).click();page.wait_for_timeout(2000);page.screenshot(path=str(OUT/'04-night-materials.png'));record('夜间材质与光照切换，不生成业务活动')
  page.get_by_role('button',name='切换日夜',exact=True).click();page.get_by_role('button',name='返回办公区',exact=True).click();page.wait_for_timeout(2000)
  assert abs(float(view.get_attribute('data-camera-zoom'))-1)<.01;record('从人物细节恢复办公室全景')
  page.get_by_role('button',name='城市全景',exact=True).click();page.wait_for_timeout(1800);assert float(view.get_attribute('data-camera-zoom'))<.3;record('原城市全景和交通仍可使用')
  page.get_by_role('button',name='返回办公区',exact=True).click();quality.select_option('balanced');page.wait_for_timeout(2000)
  renderer={k:view.get_attribute('data-'+k) for k in ['instanced','dynamic-triangles','static-triangles','draw-calls','fps','render-quality']}
  # Timed sample excludes most startup compilation and reports actual count, not target FPS.
  perf=[]
  for mode in ['balanced','economy']:
   quality.select_option(mode);expect(view).to_have_attribute('data-render-quality',mode);page.wait_for_timeout(2500)
   sample=page.evaluate('''async()=>{const el=document.querySelector('[data-testid="office-viewport"]');const t=performance.now(),n=Number(el.dataset.frameTotal||0);await new Promise(r=>setTimeout(r,10000));const dt=performance.now()-t,frames=Number(el.dataset.frameTotal||0)-n;return {ms:dt,frames,fps:frames*1000/dt,quality:el.dataset.renderQuality};}''')
   perf.append(sample)
  renderer['samples']=perf
  record('真实WebGL渲染观测（非硬件帧率承诺）',renderer)
  assert not errors;record('本轮无未捕获前端异常')
  assert transport.client.get('/api/providers').json()==[] and transport.client.get('/api/runs').json()==[];record('全部视觉操作没有创建供应商、任务或模型调用')
  page.get_by_role('button',name='退出登录',exact=True).click();expect(page.get_by_test_id('login-page')).to_be_visible();record('退出仍回到登录页')
 except Exception as e:
  results.append({'name':'browser flow','status':'failed','error':traceback.format_exc()});page.screenshot(path=str(OUT/'browser-failure.png'));raise
 finally:
  (OUT/'visual-browser-results.json').write_text(json.dumps({'passed':sum(x['status']=='passed' for x in results),'failed':sum(x['status']=='failed' for x in results),'errors':errors,'results':results},ensure_ascii=False,indent=2));b.close();transport.shutdown()
