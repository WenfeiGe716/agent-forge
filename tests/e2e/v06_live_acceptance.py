"""DOM + actual local APIs via the disclosed managed-browser transport bridge.
Includes template creation, goal-driven execution, evidence, and human acceptance.
"""
from pathlib import Path
import os,json,time,traceback
from playwright.sync_api import sync_playwright,expect
from browser_transport import LocalTransport
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-v06';STATE=Path(os.getenv('FORGE_E2E_STATE','/tmp/forge-v06-e2e'));cfg=json.loads((STATE/'setup.json').read_text());env=json.loads((STATE/'environment.json').read_text());results=[];errors=[];transport=LocalTransport()
def check(name,fn=lambda: True):
    value=fn();results.append({'name':name,'status':'passed','detail':value});print('PASS',name,flush=True);return value
def get(path):
    r=transport.client.get('/api'+path);r.raise_for_status();return r.json()
def waitstatus(rid,desired,timeout=45):
    start=time.monotonic()
    while time.monotonic()-start<timeout:
        r=get('/runs/'+rid)
        if r['status'] in desired:return r
        if r['status']=='needs_attention':raise AssertionError(r['error'])
        time.sleep(.15)
    raise TimeoutError((rid,desired,r['status']))
with sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
    page=browser.new_page(viewport={'width':1600,'height':1000});page.set_default_timeout(25000);page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        transport.attach(page,(ROOT/'frontend/dist/standalone.html').read_text());page.get_by_label('密码',exact=True).fill(env['ADMIN_PASSWORD']);page.get_by_role('button',name='登录工作空间',exact=True).click()
        expect(page.locator('.living-label')).to_have_count(4);expect(page.locator('.scene-error')).to_have_count(0)
        check('正式页面登录真实API，真实4成员与WebGL正常',lambda:{'members':page.locator('.living-label').count(),'demo_controls':page.locator('.demo-control-bar').count()})
        assert page.locator('.demo-control-bar').count()==0
        page.screenshot(path=str(OUT/'08-live-office.png'))
        page.locator('.studio-header').get_by_role('button',name='项目',exact=True).click();nav=page.locator('.sidebar nav')
        page.get_by_role('button',name='项目配置',exact=True).click();dialog=page.locator('dialog[open]');dialog.get_by_label('项目规范',exact=False).fill('HTTP浏览器验收：保持签名，依据固定测试判断。');dialog.get_by_role('button',name='保存项目配置',exact=True).click();expect(dialog).not_to_be_visible(timeout=15000)
        assert 'HTTP浏览器验收' in get('/projects')[0]['instructions'];check('项目表单保存至真实数据库',lambda:True)
        nav.get_by_role('button',name='模型与供应商',exact=True).click();modelcard=page.locator('.entity-card').filter(has_text='脚本化验收模型 / openai');modelcard.get_by_role('button',name='编辑',exact=True).click();dialog=page.locator('dialog[open]')
        dialog.get_by_label('默认模型参数 · JSON').fill('invalid-json');dialog.get_by_role('button',name='保存配置',exact=True).click();expect(dialog.get_by_role('alert')).to_be_visible();check('错误模型JSON显示错误且不保存',lambda:get('/models')[0]['version'])
        dialog.get_by_label('默认模型参数 · JSON').fill('{"max_tokens":3072}');dialog.get_by_role('button',name='保存配置',exact=True).click();expect(dialog).not_to_be_visible(timeout=15000)
        model=next(m for m in get('/models') if m['model_id']=='fixture-openai');assert model['parameters']['max_tokens']==3072;check('模型参数真实持久化且版本增加',lambda:{'version':model['version'],'parameters':model['parameters']})
        nav.get_by_role('button',name='Agent团队').click();card=page.locator('.agent-card').filter(has=page.get_by_role('heading',name='Nova',exact=True));card.get_by_role('button',name='配置成员').click();dialog=page.locator('dialog[open]')
        atlas=next(a for a in cfg['agents'] if a['role']=='planner');dialog.get_by_label('直属领导',exact=True).select_option(atlas['id']);dialog.get_by_label('同事来访接待方式',exact=True).select_option('stand');dialog.get_by_role('button',name='保存配置',exact=True).click();expect(dialog).not_to_be_visible(timeout=15000)
        nova=next(a for a in get('/agents') if a['name']=='Nova');assert nova['reports_to']==atlas['id'] and nova['peer_reception']=='stand';check('成员管理真实保存领导关系和接待偏好',lambda:{'version':nova['version'],'reception':nova['peer_reception']})
        page.screenshot(path=str(OUT/'09-live-agent-management.png'))
        nav.get_by_role('button',name='多Agent编排',exact=True).click();page.get_by_role('button',name='新建编排',exact=True).click();dialog=page.locator('dialog[open]')
        title='浏览器真实目标协作 '+str(int(time.time()));dialog.get_by_label('编排名称',exact=True).fill(title);dialog.get_by_label('编排类型',exact=True).select_option('discussion')
        dialog.locator('.agent-option').filter(has_text='Nova').locator('input').check();dialog.locator('.agent-option').filter(has_text='Echo').locator('input').check();assert dialog.get_by_label('最大讨论轮数').count()==0
        dialog.get_by_label('验收目标 1',exact=True).fill('说明加法错误与修复方案，关联两位成员的论证');dialog.get_by_label('验收目标 2',exact=True).fill('明确边界测试与剩余风险，不能只说已就绪');dialog.get_by_label('目标复核成员',exact=True).select_option(next(a['id'] for a in cfg['agents'] if a['name']=='Echo'))
        dialog.get_by_label('验收目标 1').scroll_into_view_if_needed();page.screenshot(path=str(OUT/'10-live-goal-editor.png'));dialog.get_by_role('button',name='保存编排模板',exact=True).click();expect(dialog).not_to_be_visible(timeout=15000)
        tpl=next(t for t in get('/orchestrations') if t['name']==title);assert len(tpl['graph']['participants'])==2 and 'rounds' not in tpl['graph'];assert tpl['graph']['stop_when']=='goal_satisfied';check('目标验收模板经浏览器保存至真实数据库，无轮数停止参数',lambda:{'version':tpl['version'],'graph':tpl['graph']})
        card=page.locator('.orchestration-cards .entity-card').filter(has=page.get_by_role('heading',name=title,exact=True));card.get_by_role('button',name='使用此编排创建任务',exact=True).click();dialog=page.locator('dialog[open]');dialog.get_by_label('任务名称',exact=True).fill(title);dialog.get_by_label('开发需求与验收期望',exact=True).fill('核对加法修复方案、边界测试和风险；只有证据充分、目标满足才能提交。');dialog.get_by_role('button',name='创建任务',exact=True).click();expect(dialog).not_to_be_visible(timeout=15000)
        r=next(r for r in get('/runs') if r['title']==title);rid=r['id'];assert len(get('/runs/'+rid)['snapshot']['agents'])==2;check('从模板启动真实目标工作流，仅所选两位成员参与',lambda:{'run_id':rid})
        page.wait_for_timeout(600)
        print('OFFICE STATE',page.evaluate("({hidden:document.hidden,dialogs:document.querySelectorAll('dialog[open]').length,viewport:document.querySelector('[data-testid=office-viewport]')?.dataset,canvas:document.querySelector('canvas')?.dataset})"),flush=True)
        page.locator('.studio-header').get_by_role('button',name='任务看板',exact=True).click();page.get_by_label('搜索看板任务').fill(title);expect(page.locator('.board-card')).to_have_count(1);page.locator('.board-card').click()
        r=waitstatus(rid,['awaiting_acceptance']);expect(page.locator('.discussion-turns details')).to_have_count(4,timeout=15000);assert r['calls']==6 and len([t for t in r['tasks'] if t['kind']=='goal_review'])==2
        checkpoint=page.get_by_test_id('goal-status');expect(checkpoint).to_contain_text('2/2 项目标');expect(checkpoint).to_contain_text('满足提交条件');check('界面展示真实目标检查点及4次发言2次复核结果',lambda:{'calls':r['calls'],'checkpoint':r['report']['goal_checkpoint']['outcome']})
        checkpoint.locator('details').first.locator('summary').click();expect(checkpoint).to_contain_text('discussion_');checkpoint.scroll_into_view_if_needed();page.screenshot(path=str(OUT/'11-live-goal-result.png'));check('目标详情可展开核对实际发言任务编号与理由',lambda:r['report']['goal_checkpoint']['criteria'][0]['evidence'])
        es=get('/runs/'+rid+'/events?limit=1000')['events'];outcomes=[e['data']['outcome'] for e in es if e['kind']=='orchestration.goal.assessed'];assert outcomes==['continue','goal_satisfied'];check('浏览器启动的流程实际经历缺项继续和目标达成',lambda:outcomes)
        assert page.locator('.artifact-grid a').count()==3;assert all('source.zip' not in (s or '') for s in page.locator('.artifact-grid a').evaluate_all('(els)=>els.map(e=>e.getAttribute("href"))'));check('目标讨论仅展示纪要产物，不伪造代码快照')
        page.get_by_placeholder('审批说明 / 人工验收意见').fill('已查看目标检查点、来源任务和风险说明。脚本化服务只验证平台流程，未执行代码测试。');page.get_by_role('button',name='验收通过',exact=True).click();waitstatus(rid,['completed']);check('目标达到后仍由实际人工验收操作完成运行',lambda:get('/runs/'+rid)['status'])
        (OUT/'browser-live-run.json').write_text(json.dumps(get('/runs/'+rid),ensure_ascii=False,indent=2));(OUT/'browser-live-events.json').write_text(json.dumps(es,ensure_ascii=False,indent=2))
        nav.get_by_role('button',name='任务看板',exact=True).click();page.get_by_label('搜索看板任务').fill(title);page.get_by_label('看板状态筛选').select_option('completed');page.screenshot(path=str(OUT/'12-live-goal-board.png'));expect(page.locator('.board-card')).to_have_count(1);check('完成列由真实后台结果驱动')
        assert not errors;check('真实API页面全链路无未捕获前端异常',lambda:True)
    except Exception:
        results.append({'name':'真实API浏览器验收异常','status':'failed','error':traceback.format_exc()});print(traceback.format_exc(),flush=True);page.screenshot(path=str(OUT/'live-browser-failure.png'))
    finally:
        (OUT/'live-browser-results.json').write_text(json.dumps({'method':'actual DOM/WebGL + real HTTP bridge; SCRIPTED model replies; not native navigation/SSE','passed':sum(r['status']=='passed' for r in results),'failed':sum(r['status']=='failed' for r in results),'pageerrors':errors,'results':results},ensure_ascii=False,indent=2));browser.close();transport.shutdown()
if any(r['status']=='failed' for r in results):raise SystemExit(1)
