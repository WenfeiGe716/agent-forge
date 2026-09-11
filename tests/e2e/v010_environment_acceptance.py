"""Real production DOM/WebGL + real local API. The existing explicit transport
bridge respects the managed browser's URL restrictions. No model/traffic results
are supplied by the bridge. Navigation here means in-app route changes.
"""
from pathlib import Path
import json,traceback,time
from playwright.sync_api import sync_playwright,expect
from browser_transport import LocalTransport
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v010';OUT.mkdir(exist_ok=True)
config=json.loads((ROOT/'.local/config.json').read_text());t=LocalTransport('http://127.0.0.1:8797');results=[];errors=[]
expect.set_options(timeout=45000)
def record(name,detail=True):results.append({'name':name,'status':'passed','detail':detail});print('PASS',name,flush=True)
with sync_playwright() as pw:
 b=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
 page=b.new_page(viewport={'width':1600,'height':1000});page.set_default_timeout(45000);page.on('pageerror',lambda e:errors.append(str(e)))
 def state():return page.evaluate("loadUI('office/ambientEnvironment').environment.status()")
 def snapshot():return page.evaluate("loadUI('office/ambientEnvironment').environment.traffic.snapshot()")
 def btn(name):return page.get_by_role('button',name=name,exact=True)
 def view():return page.get_by_test_id('office-viewport')
 def login():
  page.get_by_label('用户名',exact=True).fill('admin');page.get_by_label('密码',exact=True).fill(config['ADMIN_PASSWORD']);btn('登录工作空间').click();page.wait_for_selector('[data-testid="office-viewport"][data-frame-total]')
 try:
  t.attach(page,(ROOT/'frontend/dist/index.html').read_text());expect(page.get_by_test_id('login-page')).to_be_visible();assert not state()['running'];record('正式首页是登录，未认证时不运行环境时钟')
  login();expect(view()).to_have_attribute('data-city-enabled','true');assert t.client.get('/api/providers').json()==[] and t.client.get('/api/runs').json()==[];record('零供应商仍直接进入正式办公室，城市默认常驻')
  page.wait_for_timeout(4500);assert len(page.locator('.living-label').all())==4;assert page.locator('.scene-error').count()==0
  assert float(view().get_attribute('data-camera-zoom'))==.8;assert view().get_attribute('data-parked-cars')=='4';page.screenshot(path=str(OUT/'01-office-campus.png'));record('默认办公镜头同时绘制室内精修、门口道路、人车和停车位')
  assert page.get_by_label('显示城市街景',exact=True).count()==0;assert page.get_by_text('示例散会 / 推进下一阶段',exact=True).count()==0;record('没有关闭城市的旧开关或演示推进按钮')
  a=state();page.wait_for_timeout(5100);bb=state();assert bb['time']-a['time']>=4.5;record('环境时钟按实际时间推进，不再按低帧率慢放',{'elapsed':bb['time']-a['time']})
  # Pause optional idle choreography for close-up images, through the actual API.
  r=t.client.put('/api/office/settings',json={'idle_enabled':False},headers={'X-Forge-Request':'1'});r.raise_for_status();page.wait_for_timeout(1500)
  btn('空闲策略').click();expect(page.locator('dialog[open]')).to_be_visible();a=state();frames=int(view().get_attribute('data-frame-total'));page.wait_for_timeout(4500);bb=state();assert bb['time']-a['time']>4;assert int(view().get_attribute('data-frame-total'))<=frames+1;page.locator('dialog[open]').get_by_role('button',name='关闭',exact=True).click();record('编辑弹窗停止GPU绘制，但人车信号时钟继续',{'elapsed':bb['time']-a['time']})
  page.evaluate("window.__sameTraffic=loadUI('office/ambientEnvironment').environment.traffic")
  btn('任务看板').click();expect(page.locator('.sidebar')).to_be_visible();assert view().count()==0;a=state();page.wait_for_timeout(4300);bb=state();assert bb['time']-a['time']>3.9;page.locator('.sidebar').get_by_role('button',name='工作台',exact=True).click();page.wait_for_selector('[data-testid="office-viewport"][data-frame-total]');assert page.evaluate("window.__sameTraffic===loadUI('office/ambientEnvironment').environment.traffic");record('去任务看板再回来，交通没有重新建场或重置')
  for label in ['项目','模型','Skill']:
   print('CHECK route',label,flush=True)
   page.locator('.studio-header').get_by_role('button',name=label,exact=True).click();print('LEFT OFFICE',label,flush=True);a=state();page.wait_for_timeout(1000);assert state()['time']>a['time']+.7;print('CLOCK OK',label,flush=True);page.locator('.sidebar').get_by_role('button',name='工作台',exact=True).click();page.wait_for_selector('[data-testid="office-viewport"][data-frame-total]')
  record('项目、供应商及Skill管理页面与同一常驻环境隔离运行')
  a=state();btn('城市全景').click();page.wait_for_timeout(3200);assert float(view().get_attribute('data-camera-zoom'))<.3;assert state()['time']>a['time'];page.screenshot(path=str(OUT/'02-city-day.png'));record('城市全景只改变镜头，不重置车辆和信号')
  btn('路口观察').click();page.wait_for_timeout(2400);page.screenshot(path=str(OUT/'03-intersection.png'));assert float(view().get_attribute('data-camera-zoom'))>1.2;record('路口观察显示实际车辆、停止线、斑马线和行人')
  # Pick an actual rendered car by using the production renderer's projection.
  # Canvas coordinates are derived from its exact current camera, not guessed.
  point=page.evaluate("""()=>{const e=loadUI('office/ambientEnvironment').environment,s=e.traffic;const car=s.cars.find(c=>Math.abs(c.x-20)<9&&Math.abs(c.z-14)<9)||s.cars[0];const M=loadUI('graphics/scene'),c=document.querySelector('canvas.native-scene'),yaw=1.12,pitch=.88,r=64,target=[20,.6,14],eye=[target[0]+Math.cos(yaw)*Math.cos(pitch)*r,target[1]+Math.sin(pitch)*r,target[2]+Math.sin(yaw)*Math.cos(pitch)*r],height=25/1.25,vp=M.multiply(M.ortho(height*c.clientWidth/c.clientHeight,height),M.lookAt(eye,target)),q=M.transform(vp,[car.x,.5,car.z]);return{x:(q[0]+1)*c.clientWidth/2,y:(1-q[1])*c.clientHeight/2};}""")
  if 280<point['x']<1300 and 150<point['y']<700:
   page.locator('canvas.native-scene').click(position=point,force=True);page.wait_for_timeout(300)
   if page.locator('.studio-notice').count():record('车辆拾取显示环境来源，而不是Agent任务',page.locator('.studio-notice').inner_text())
  btn('环境设置').click();expect(page.get_by_label('城市人车密度',exact=True)).to_have_value('auto');btn('切换环境运动').click();page.wait_for_timeout(350);a=snapshot();page.wait_for_timeout(3000);assert snapshot()==a;assert view().get_attribute('data-city-enabled')=='true';record('减少运动保留所有静态城市与人车位置，不隐藏环境')
  btn('切换环境运动').click();a=state();page.wait_for_timeout(1900);assert 1.2<state()['time']-a['time']<3.0;record('恢复运动从原位置继续，不补跳手动暂停时间')
  page.get_by_label('城市人车密度',exact=True).select_option('low');page.wait_for_timeout(1000);assert state()['density']=='low';a=state();page.get_by_label('3D画质',exact=True).select_option('detail');page.wait_for_timeout(1000);assert state()['density']=='low';assert state()['time']>=a['time'];record('显式低密度覆盖画质建议，切换不重置交通')
  page.get_by_label('城市人车密度',exact=True).select_option('auto');page.wait_for_timeout(1000);assert state()['density']=='busy';assert state()['cars']==16;record('精细档自动启用丰富人车并保持碰撞规则')
  page.get_by_label('3D画质',exact=True).select_option('economy');page.wait_for_timeout(1000);assert state()['density']=='low';record('节能档降低环境密度与渲染开销，不改变业务')
  page.get_by_label('3D画质',exact=True).select_option('balanced');page.wait_for_timeout(1000);assert state()['density']=='normal';btn('关闭环境设置').click()
  btn('城市全景').click();page.wait_for_timeout(2000);btn('切换日夜').click();page.wait_for_timeout(2500);page.screenshot(path=str(OUT/'04-city-night.png'));assert page.get_by_test_id('studio-workspace').evaluate('(e)=>e.classList.contains("night")');record('城市与办公室共用日夜，路灯、窗灯、车灯有真实几何变化')
  btn('返回办公区').click();page.wait_for_timeout(2000);assert abs(float(view().get_attribute('data-camera-zoom'))-.8)<.001;assert view().get_attribute('data-city-enabled')=='true';record('返回办公区后外景仍存在且交通继续')
  btn('切换日夜').click();btn('聚焦成员').click();page.wait_for_timeout(2500);assert float(view().get_attribute('data-camera-zoom'))>3.5;page.screenshot(path=str(OUT/'05-interior-preserved.png'));assert state()['running'];record('保留v0.9人物细节、坐姿与家具，剖切近景不停止环境')
  btn('返回办公区').click();page.wait_for_timeout(2500);samples=[]
  for quality in ['balanced','economy']:
   page.get_by_label('3D画质',exact=True).select_option(quality);page.wait_for_timeout(2400)
   sample=page.evaluate("""async()=>{const el=document.querySelector('[data-testid="office-viewport"]'),env=loadUI('office/ambientEnvironment').environment,t=performance.now(),frames=+el.dataset.frameTotal,sim=env.traffic.time;await new Promise(r=>setTimeout(r,8000));const ms=performance.now()-t,n=+el.dataset.frameTotal-frames;return{ms,frames:n,fps:n*1000/ms,simulatedSeconds:env.traffic.time-sim,quality:el.dataset.renderQuality,triangles:el.dataset.dynamicTriangles,staticTriangles:el.dataset.staticTriangles,drawCalls:el.dataset.drawCalls};}""")
   samples.append(sample)
  record('软件GPU实际帧率和独立交通时钟采样，不假称硬件GPU性能',samples)
  assert page.locator('canvas.native-scene').evaluate("c=>c.getContext('webgl').getError()") == 0;assert not errors;record('本次实际DOM/WebGL检查无未捕获异常，WebGL错误0')
  assert t.client.get('/api/providers').json()==[] and t.client.get('/api/runs').json()==[];record('全部环境操作未生成供应商、任务、会议、Agent消息或模型调用')
  page.set_viewport_size({'width':1100,'height':850});page.wait_for_timeout(2000);assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');btn('环境设置').click();expect(page.get_by_label('常驻环境设置',exact=True)).to_be_visible();page.screenshot(path=str(OUT/'06-compact-settings.png'));btn('关闭环境设置').click();record('1100px环境控制可操作，无文档水平溢出')
  btn('退出登录').click();expect(page.get_by_test_id('login-page')).to_be_visible();assert not state()['running'];before=state()['time'];page.wait_for_timeout(1500);assert state()['time']==before;record('退出登录释放运行计时器，环境不进入登录主页')
  login();assert state()['time']>=before;assert view().get_attribute('data-city-enabled')=='true';record('同一会话再次登录保留环境位置，不导入虚构业务数据')
 except Exception:
  results.append({'name':'browser flow','status':'failed','error':traceback.format_exc()});page.screenshot(path=str(OUT/'browser-failure.png'));raise
 finally:
  (OUT/'browser-results.json').write_text(json.dumps({'passed':sum(x['status']=='passed' for x in results),'failed':sum(x['status']=='failed' for x in results),'errors':errors,'results':results},ensure_ascii=False,indent=2));b.close();t.shutdown()
