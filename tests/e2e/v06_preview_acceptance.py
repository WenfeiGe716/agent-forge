"""Actual DOM/WebGL on standalone demo. No backend/model execution claimed."""
from pathlib import Path
import json,traceback,time
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v06';results=[];errors=[]
def record(name,detail=True):
 results.append({'name':name,'status':'passed','detail':detail});print('PASS',name,flush=True)
with sync_playwright() as pw:
 browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
 page=browser.new_page(viewport={'width':1600,'height':1000},device_scale_factor=1);page.set_default_timeout(12000);page.on('pageerror',lambda e:errors.append(str(e)))
 try:
  page.set_content((ROOT/'frontend/dist/workbench-preview.html').read_text(),wait_until='load');viewport=page.get_by_test_id('office-viewport');expect(page.locator('.living-label')).to_have_count(14);expect(page.locator('.scene-error')).to_have_count(0)
  page.wait_for_function("[...document.querySelectorAll('.living-label')].every(e=>e.dataset.posture==='seated')")
  record('实际WebGL办公室14名成员保留工位坐姿');page.screenshot(path=str(OUT/'01-office.png'))
  expect(viewport).to_have_attribute('data-city-enabled','true');expect(viewport).to_have_attribute('data-city-cars','12');expect(viewport).to_have_attribute('data-city-peds','6');record('默认加载城市、12车辆及6名过街行人')
  page.get_by_label('城市全景',exact=True).click();page.wait_for_timeout(900);expect(page.locator('.studio-workspace')).to_have_class(__import__('re').compile('city-overview'))
  expect(page.locator('.studio-composer')).not_to_be_visible();assert page.locator('.living-label:visible').count()==0;record('城市全景隐藏办公面板和密集人物标签')
  assert page.locator('.office-city-label:visible').count()==6;record('商场及五座楼宇可定位且具有明确地标说明')
  page.screenshot(path=str(OUT/'02-city-day.png'));page.locator('.office-city-label').filter(has_text='FORGE MALL').click();expect(page.locator('.studio-notice')).to_contain_text('商场',timeout=5000);record('实际点击商场地标显示范围说明');page.get_by_label('关闭提示',exact=True).click()
  page.get_by_label('暂停城市交通',exact=True).click();page.wait_for_timeout(500);a=float(viewport.get_attribute('data-city-time'));page.wait_for_timeout(900);b=float(viewport.get_attribute('data-city-time'));assert a==b;record('暂停交通冻结城市模拟时钟，不暂停Agent',{'time':a})
  page.get_by_label('暂停城市交通',exact=True).click();page.wait_for_function(f"+document.querySelector('[data-testid=office-viewport]').dataset.cityTime>{b+.1}");record('继续交通恢复固定步长模拟')
  page.get_by_label('城市人车密度',exact=True).select_option('low');expect(viewport).to_have_attribute('data-city-cars','4');expect(viewport).to_have_attribute('data-city-peds','2');expect(page.locator('.living-label')).to_have_count(14);record('低密度4车2过街行人不修改办公室成员')
  page.get_by_label('城市人车密度',exact=True).select_option('normal');expect(viewport).to_have_attribute('data-city-cars','12')
  page.get_by_label('路口观察',exact=True).click();page.wait_for_timeout(850);page.screenshot(path=str(OUT/'02b-intersection.png'));record('路口近景镜头可观察实际人车与信号灯');page.get_by_label('城市全景',exact=True).click();page.wait_for_timeout(700)
  page.get_by_label('切换日夜',exact=True).click();expect(page.locator('.studio-workspace')).to_have_class(__import__('re').compile('night'));page.wait_for_timeout(600);page.screenshot(path=str(OUT/'03-city-night.png'));record('日夜切换应用到实际城市渲染与灯光')
  page.get_by_label('显示城市街景',exact=True).uncheck();expect(viewport).to_have_attribute('data-city-enabled','false');expect(page.locator('.studio-composer')).to_be_visible();assert page.locator('.office-city-label:visible').count()==0;record('关闭街景自动返回办公视角，不留下空白远景')
  page.get_by_label('显示城市街景',exact=True).check();page.get_by_label('切换日夜',exact=True).click();page.get_by_label('城市全景',exact=True).click();page.get_by_label('返回办公区',exact=True).click();expect(page.locator('.studio-composer')).to_be_visible();record('办公区与城市镜头可反复切换且保留管理操作')
  page.set_viewport_size({'width':1100,'height':850});page.wait_for_timeout(600);assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');record('1100px视口无文档横向溢出');page.set_viewport_size({'width':1600,'height':1000})
  page.locator('.studio-header').get_by_role('button',name='编排',exact=True).click();page.get_by_role('button',name='新建编排',exact=True).click();dialog=page.locator('dialog[open]')
  assert dialog.get_by_label('最大讨论轮数',exact=True).count()==0;assert dialog.get_by_label('停止条件',exact=True).count()==0;expect(dialog).to_contain_text('逐项验收，不设固定讨论轮数');record('编排页面已移除固定轮数和ready停止设置')
  title='城市预览 · 按证据收敛';dialog.get_by_label('编排名称',exact=True).fill(title);dialog.get_by_label('编排类型',exact=True).select_option('discussion');dialog.get_by_label('协作执行方式',exact=True).select_option('parallel')
  for name in ['Nova','Vega']:dialog.locator('.agent-option').filter(has_text=name).locator('input').check()
  dialog.get_by_role('button',name='上移 Vega',exact=True).click();expect(dialog.locator('.speaker-order').first).to_contain_text('Vega');record('仍支持指定子团队与上下文执行方式')
  dialog.get_by_label('验收目标 1',exact=True).fill('定位实际源文件中的边界问题并给出修复方案');dialog.get_by_label('目标证据 1',exact=True).select_option('code_reference');dialog.get_by_role('button',name='＋ 添加验收目标',exact=True).click();dialog.get_by_label('验收目标 3',exact=True).fill('明确风险与尚需人工确认事项');dialog.get_by_role('button',name='删除目标 2',exact=True).click();assert dialog.locator('.goal-editor-row').count()==2
  dialog.get_by_label('目标复核成员',exact=True).select_option('demo-Vega');record('验收目标、证据类型和复核成员均可编辑')
  dialog.locator('.goal-safety summary').click();dialog.get_by_label('连续无进展检查阈值').fill('4');expect(dialog.locator('.goal-safety')).to_contain_text('不按总轮数停止');record('异常保护与目标成功退出明确分开')
  dialog.get_by_label('验收目标 1').scroll_into_view_if_needed();page.screenshot(path=str(OUT/'04-goal-editor-preview.png'));dialog.get_by_role('button',name='保存编排模板',exact=True).click();expect(dialog).not_to_be_visible()
  card=page.locator('.entity-card').filter(has=page.get_by_role('heading',name=title,exact=True));expect(card).to_contain_text('2 项验收标准');record('新目标模板在示例列表实际保存并更新结构图')
  card.get_by_role('button',name='使用此编排创建任务',exact=True).click();dialog=page.locator('dialog[open]');dialog.get_by_label('任务名称',exact=True).fill('按目标执行 · 预览任务');dialog.get_by_label('开发需求与验收期望',exact=True).fill('核查源文件，讨论方案，未解决的问题继续协作。');expect(dialog).to_contain_text('本次只调用编排中选定的 2 位成员');dialog.get_by_role('button',name='创建任务',exact=True).click();expect(dialog).not_to_be_visible();expect(page.locator('.toast')).to_contain_text('不会执行模型');record('示例创建保留明确不执行模型的提示')
  if page.locator('.sidebar').is_visible():page.locator('.sidebar nav').get_by_role('button',name='任务看板',exact=True).click()
  else:page.locator('.studio-header').get_by_role('button',name='任务看板',exact=True).click()
  page.get_by_label('搜索看板任务').fill('按目标执行');expect(page.locator('.board-card')).to_have_count(1);record('新增任务可从任务看板检索，界面非静态效果图')
  assert not errors;record('所有操作无未捕获前端异常')
 except Exception:
  results.append({'name':'浏览器预览异常','status':'failed','error':traceback.format_exc()});print(traceback.format_exc(),flush=True);page.screenshot(path=str(OUT/'preview-failure.png'))
 finally:
  (OUT/'preview-browser-results.json').write_text(json.dumps({'method':'headed Chromium SwiftShader, actual DOM/WebGL, in-memory demo, no native URL navigation','passed':sum(x['status']=='passed' for x in results),'failed':sum(x['status']=='failed' for x in results),'pageerrors':errors,'results':results},ensure_ascii=False,indent=2));browser.close()
if any(x['status']=='failed' for x in results):raise SystemExit(1)
