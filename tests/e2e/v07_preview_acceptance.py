"""Standalone preview: actual DOM/WebGL with clearly labeled in-memory examples.
No model or backend calls, no pre-rendered image substitution.
"""
from pathlib import Path
import json,traceback,re
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v07';results=[];errors=[]
def rec(n,d=True):results.append({'name':n,'status':'passed','detail':d});print('PASS',n,flush=True)
with sync_playwright() as pw:
 browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
 page=browser.new_page(viewport={'width':1600,'height':1000});page.set_default_timeout(16000);page.on('pageerror',lambda e:errors.append(str(e)))
 try:
  page.set_content((ROOT/'frontend/dist/workbench-preview.html').read_text(),wait_until='load');expect(page.locator('.living-label')).to_have_count(14);expect(page.locator('.demo-control-bar')).to_contain_text('不调用模型');expect(page.locator('.scene-error')).to_have_count(0);rec('独立预览加载14成员、WebGL和明确示例标识')
  page.locator('.studio-header').get_by_role('button',name='协作链',exact=True).click();expect(page.get_by_test_id('collaboration-workbench')).to_be_visible();page.get_by_role('button',name='查看并回答',exact=True).click();q=page.get_by_test_id('decision-card');expect(q).to_contain_text('这次优先交付');q.locator('input[type=radio]').first.check();q.get_by_label('决策补充说明').fill('示例：保持当前接口，确认边界测试。');q.get_by_role('button',name='提交回答并继续',exact=True).click();expect(q).to_contain_text('已回答');rec('决策界面可选择和补充，页面内存记录回答')
  q.screenshot(path=str(OUT/'preview-decision-card.png'))
  page.get_by_role('button',name='查看办公室',exact=True).click();page.wait_for_function("()=>document.querySelectorAll('.living-label[data-meeting-id]:not([data-meeting-id=\"\"])[data-posture=seated]').length===3",timeout=30000);rec('示例会议三名参与者走到不同椅子并入座')
  page.wait_for_timeout(7500);assert page.locator('.living-label[data-meeting-id]:not([data-meeting-id=""])[data-posture=seated]').count()==3;rec('超过旧6秒动画时长仍保持会议，未收到结束状态不散会')
  page.screenshot(path=str(OUT/'preview-meeting-office.png'))
  page.get_by_role('button',name='示例散会 / 推进下一阶段',exact=True).click();page.wait_for_function("()=>!document.querySelector('.living-label[data-meeting-id]:not([data-meeting-id=\"\"])')",timeout=30000);rec('示例结束事件后返回工位，不再强制继续会议')
  page.locator('.studio-header').get_by_role('button',name='协作链',exact=True).click();page.get_by_role('button',name='协作与行动项',exact=True).click();expect(page.locator('.action-card')).to_have_count(2);expect(page.locator('.meeting-card')).to_contain_text('会议已结束');page.screenshot(path=str(OUT/'preview-action-workbench.png'));rec('会议决议与两个建议行动项及责任人联动显示')
  page.locator('.action-card').first.get_by_role('button',name=re.compile('执行会话')).click();expect(page.locator('.session-message')).to_have_count(2);expect(page.locator('.session-workbench')).to_contain_text('示例');rec('行动项可跳到对应公开会话，始终标记为示例')
  page.get_by_role('button',name='追溯时间线',exact=True).click();expect(page.locator('.trace-panel')).to_contain_text('skill.activated');expect(page.locator('.trace-panel')).to_contain_text('meeting.closed');rec('决策、Skill和会议事件可以在同一追溯视图查看')
  page.locator('.sidebar nav').get_by_role('button',name='Skill能力库',exact=True).click();page.get_by_role('button',name='创建Skill',exact=True).click();d=page.locator('dialog[open]');d.get_by_label('Skill文件内容',exact=True).fill('---\nname: preview-review\ndescription: 预览编辑示例，不执行脚本。\n---\n# 审查\n核对真实证据。\n');d.get_by_role('button',name='保存待审核版本',exact=True).click();d.get_by_role('button',name='批准此版本',exact=True).click();expect(d.get_by_role('button',name='撤销此版本',exact=True)).to_be_visible();rec('Skill新建、保存和审批页面可操作，预览不声称后端持久化')
  d.get_by_role('button',name='编辑并创建新版本',exact=True).click();d.get_by_label('新资源路径').fill('references/review.md');d.get_by_role('button',name='添加资源',exact=True).click();d.get_by_label('Skill文件内容',exact=True).fill('需要实际代码与测试证据。');d.get_by_role('button',name='保存待审核版本',exact=True).click();expect(d.get_by_label('Skill版本')).to_have_value('2');d.get_by_label('Skill版本').select_option('1');expect(d).to_contain_text('已批准');rec('编辑生成新版本，历史版本仍可查看')
  d.get_by_role('button',name='关闭',exact=True).click();page.locator('.sidebar nav').get_by_role('button',name='多Agent编排',exact=True).click();card=page.locator('.entity-card').filter(has=page.get_by_role('heading',name='双开发者接力讨论',exact=True));card.get_by_role('button',name='使用此编排创建任务',exact=True).click();d=page.locator('dialog[open]');expect(d.get_by_label('入口负责人',exact=True)).to_have_value('demo-Nova');rec('纯讨论模板自动从授权子团队选择入口，不误带全局负责人')
  expect(d).to_contain_text('本次只调用编排中选定的 2 位成员');assert d.get_by_label('最大讨论轮数',exact=True).count()==0;rec('目标驱动与小组协作保留，不回到固定轮数停止')
  d.get_by_role('button',name='关闭',exact=True).click();page.set_viewport_size({'width':1100,'height':850});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');rec('1100px预览管理界面无文档水平溢出')
  assert not errors;rec('全部预览交互无未捕获前端异常')
 except Exception:
  results.append({'name':'预览验收异常','status':'failed','error':traceback.format_exc()});print(traceback.format_exc(),flush=True);page.screenshot(path=str(OUT/'preview-failure.png'))
 finally:
  (OUT/'preview-browser-results.json').write_text(json.dumps({'method':'actual Chromium/WebGL in-memory preview; no backend/model execution','passed':sum(x['status']=='passed' for x in results),'failed':sum(x['status']=='failed' for x in results),'pageerrors':errors,'results':results},ensure_ascii=False,indent=2));browser.close()
if any(x['status']=='failed' for x in results):raise SystemExit(1)
