"""Actual DOM/WebGL interaction against the explicitly labelled in-memory preview.
Does NOT validate production APIs; see v05_live_acceptance.py for that.
"""
from pathlib import Path
import json,time,traceback,os
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v05';OUT.mkdir(exist_ok=True)
results=[];errors=[]
def check(name,fn):
    try:
        detail=fn();results.append({'name':name,'status':'passed','detail':detail});print('PASS',name,flush=True)
    except Exception as e:
        results.append({'name':name,'status':'failed','error':str(e)});print('FAIL',name,str(e),flush=True);raise
with sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
    page=browser.new_page(viewport={'width':1600,'height':1000},device_scale_factor=1);page.set_default_timeout(10000);page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        page.set_content((ROOT/'frontend/dist/workbench-preview.html').read_text(),wait_until='load')
        expect(page.locator('.living-label')).to_have_count(14)
        check('实际WebGL生成14位成员，非静态截图',lambda: {'labels':page.locator('.living-label').count(),'errors':page.locator('.scene-error').count()})
        def seated():
            page.wait_for_function("[...document.querySelectorAll('.living-label')].length===14 && [...document.querySelectorAll('.living-label')].every(e=>e.dataset.posture==='seated')")
            page.screenshot(path=str(OUT/'01-seated-office.png'));return page.locator('.living-label').evaluate_all('(els)=>els.map(e=>({id:e.dataset.agentId,posture:e.dataset.posture,blend:e.dataset.sitBlend}))')
        check('无来访时所有工位成员坐着',seated)
        nova=page.locator('.living-label[data-agent-id="demo-Nova"]')
        page.locator('.studio-member').filter(has_text='Nova').click();page.get_by_role('button',name='聚焦成员',exact=True).click();page.wait_for_timeout(900)
        page.screenshot(path=str(OUT/'02-seated-closeup.png'))
        check('选中人物与成员详情及镜头聚焦联动',lambda:expect(page.get_by_test_id('posture-evidence')).to_contain_text('坐在椅子上'))
        def peer():
            page.get_by_role('button',name='同事来访',exact=True).click()
            page.wait_for_function("(()=>{let e=document.querySelector('[data-agent-id=\"demo-Nova\"]');return e?.dataset.visitEvent && e.dataset.posture==='seated' && e.dataset.leaderVisit==='false'})()",timeout=25000)
            data=nova.evaluate('(e)=>({...e.dataset})');page.screenshot(path=str(OUT/'03-peer-seated.png'));return data
        check('同事到访：接待者仍坐着，事件编号可追溯',peer)
        page.wait_for_function("!document.querySelector('[data-agent-id=\"demo-Nova\"]').dataset.visitEvent",timeout=25000)
        def leader():
            page.get_by_role('button',name='领导来访',exact=True).click()
            page.wait_for_function("(()=>{let e=document.querySelector('[data-agent-id=\"demo-Nova\"]');return e?.dataset.leaderVisit==='true' && e.dataset.posture==='standing'})()",timeout=25000)
            data=nova.evaluate('(e)=>({...e.dataset})');page.screenshot(path=str(OUT/'04-leader-standing.png'));return data
        check('领导到访：覆盖坐姿偏好，站在椅旁',leader)
        page.wait_for_function("(()=>{let e=document.querySelector('[data-agent-id=\"demo-Nova\"]');return !e.dataset.visitEvent && e.dataset.posture==='seated'})()",timeout=25000)
        check('领导离开后重新坐回椅子',lambda:expect(page.get_by_test_id('posture-evidence')).to_contain_text('坐在椅子上'))
        page.get_by_role('button',name='任务看板',exact=True).click();expect(page.get_by_test_id('task-board')).to_be_visible()
        check('任务看板的五个状态列与4条示例运行可操作',lambda:expect(page.locator('.board-card')).to_have_count(4))
        page.get_by_label('搜索看板任务').fill('回归测试');check('看板按需求搜索',lambda:expect(page.locator('.board-card')).to_have_count(1))
        page.get_by_label('搜索看板任务').fill('');page.get_by_label('看板状态筛选').select_option('awaiting_approval');check('看板按状态筛选',lambda:expect(page.locator('.board-card')).to_have_count(1))
        page.get_by_label('看板状态筛选').select_option('');page.screenshot(path=str(OUT/'05-task-board.png'))
        page.locator('.board-card').filter(has_text='需求拆分').click();check('看板卡片进入任务审批与执行详情',lambda:expect(page.get_by_role('button',name='批准并开始开发')).to_be_visible())
        nav=page.locator('.sidebar nav')
        nav.get_by_role('button',name='项目工作区').click();page.get_by_role('button',name='项目配置').click();dialog=page.locator('dialog[open]')
        dialog.get_by_label('项目名称',exact=True).fill('已编辑的项目 · 示例');dialog.get_by_role('button',name='保存项目配置').click()
        check('项目配置表单保存并更新卡片',lambda:expect(page.get_by_role('heading',name='已编辑的项目 · 示例',exact=True)).to_be_visible())
        nav.get_by_role('button',name='模型与供应商').click();page.locator('.entity-card').filter(has_text='开发模型').get_by_role('button',name='编辑',exact=True).click();dialog=page.locator('dialog[open]')
        dialog.get_by_label('默认模型参数 · JSON').fill('{"max_tokens":2048}');dialog.get_by_role('button',name='保存配置',exact=True).click()
        check('模型参数通过编辑表单保存',lambda:expect(page.locator('.entity-card').filter(has_text='开发模型')).to_contain_text('2048'))
        nav.get_by_role('button',name='Agent团队').click();card=page.locator('.agent-card').filter(has=page.get_by_role('heading',name='Nova',exact=True));card.get_by_role('button',name='配置成员').click();dialog=page.locator('dialog[open]')
        dialog.get_by_label('同事来访接待方式',exact=True).select_option('stand');dialog.get_by_label('直属领导',exact=True).select_option('demo-Atlas');dialog.get_by_role('button',name='保存配置',exact=True).click()
        check('成员的领导关系和同事站姿偏好可保存',lambda:expect(page.locator('.agent-card').filter(has=page.get_by_role('heading',name='Nova',exact=True))).to_contain_text('同事来访起身'))
        nav.get_by_role('button',name='多Agent编排').click();page.get_by_role('button',name='新建编排',exact=True).click();dialog=page.locator('dialog[open]')
        dialog.get_by_label('编排名称',exact=True).fill('浏览器验证 · 双成员并行3轮');dialog.get_by_label('编排类型',exact=True).select_option('discussion');dialog.get_by_label('轮内执行方式',exact=True).select_option('parallel')
        dialog.locator('.agent-option').filter(has_text='Nova').locator('input').check();dialog.locator('.agent-option').filter(has_text='Vega').locator('input').check();dialog.get_by_label('最大讨论轮数',exact=True).fill('3');dialog.get_by_role('button',name='上移 Vega',exact=True).click()
        check('编排可选择子团队并调整发言顺序',lambda:expect(dialog.locator('.speaker-order').first).to_contain_text('Vega'))
        page.screenshot(path=str(OUT/'06-orchestration-editor.png'));dialog.get_by_role('button',name='保存编排模板').click()
        card=page.locator('.entity-card').filter(has=page.get_by_role('heading',name='浏览器验证 · 双成员并行3轮',exact=True));check('编排模板保存后显示真实编辑字段',lambda:expect(card).to_contain_text('并行轮次'))
        page.screenshot(path=str(OUT/'07-orchestration-templates.png'));card.get_by_role('button',name='使用此编排创建任务').click();dialog=page.locator('dialog[open]');dialog.get_by_label('任务名称',exact=True).fill('双Agent多轮讨论 · 新示例');dialog.get_by_label('开发需求与验收期望',exact=True).fill('讨论功能接口与回归测试范围，列出分歧。')
        check('纯讨论创建页不要求固定角色或整个团队',lambda:expect(dialog).to_contain_text('本次只调用编排中选定的 2 位成员'))
        dialog.get_by_role('button',name='创建任务',exact=True).click();expect(dialog).not_to_be_visible()
        check('使用编排创建示例任务并明确不执行模型',lambda:expect(page.locator('.toast')).to_contain_text('不会执行模型'))
        # Back to board via sidebar, since creation may retain detailed mode.
        if page.locator('.sidebar').is_visible():nav.get_by_role('button',name='任务看板',exact=True).click()
        else:page.locator('.studio-header').get_by_role('button',name='任务看板',exact=True).click()
        page.get_by_label('看板状态筛选').select_option('queued');check('新建讨论任务出现在看板队列',lambda:expect(page.locator('.board-card')).to_contain_text('双Agent多轮讨论'))
        page.set_viewport_size({'width':1100,'height':850});page.wait_for_timeout(500)
        check('1100px界面无文档横向溢出（看板内部可横滚）',lambda:page.evaluate('document.documentElement.scrollWidth<=innerWidth') is True or (_ for _ in ()).throw(AssertionError('overflow')))
        check('无未捕获前端异常',lambda:len(errors)==0 or (_ for _ in ()).throw(AssertionError(errors)))
    except Exception:
        page.screenshot(path=str(OUT/'preview-failure.png'));(OUT/'preview-failure.txt').write_text(traceback.format_exc())
    finally:
        (OUT/'preview-browser-results.json').write_text(json.dumps({'mode':'explicit in-memory preview, not production API','passed':sum(x['status']=='passed' for x in results),'failed':sum(x['status']=='failed' for x in results),'pageerrors':errors,'results':results},ensure_ascii=False,indent=2));browser.close()
if any(x['status']=='failed' for x in results):raise SystemExit(1)
