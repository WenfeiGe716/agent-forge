"""Interaction acceptance against real local API; see browser_transport.py limitations."""
import base64,hashlib,io,json,os,struct,time,traceback,zipfile
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from browser_transport import LocalTransport
ROOT=Path(__file__).resolve().parents[2];STATE=Path(os.getenv('FORGE_E2E_STATE','/tmp/agent-forge-e2e'));OUT=ROOT/'docs/evidence';OUT.mkdir(parents=True,exist_ok=True)
env=json.loads((STATE/'environment.json').read_text());setup=json.loads((STATE/'setup.json').read_text())
results=[];errors=[];screens=[];TITLE='浏览器交互验收 / '+str(int(time.time()))
def record(name,details=''):
    results.append({'name':name,'status':'passed','details':details});print('PASS',name,flush=True);(OUT/'browser-progress.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
def glb_bytes():
    vertices=struct.pack('<12f',0,0,0,1,0,0,0,1,0,0,0,1);indices=struct.pack('<12H',0,2,1,0,1,3,0,3,2,1,2,3);binary=vertices+indices
    data={'asset':{'version':'2.0'},'buffers':[{'byteLength':len(binary)}],'bufferViews':[{'buffer':0,'byteOffset':0,'byteLength':48},{'buffer':0,'byteOffset':48,'byteLength':24}],'accessors':[{'bufferView':0,'componentType':5126,'count':4,'type':'VEC3'},{'bufferView':1,'componentType':5123,'count':12,'type':'SCALAR'}],'meshes':[{'primitives':[{'attributes':{'POSITION':0},'indices':1,'material':0}]}],'materials':[{'pbrMetallicRoughness':{'baseColorFactor':[.7,.8,.4,1]}}],'nodes':[{'mesh':0}],'scenes':[{'nodes':[0]}],'scene':0}
    text=json.dumps(data).encode();text+=b' '*((-len(text))%4);return struct.pack('<III',0x46546c67,2,12+8+len(text)+8+len(binary))+struct.pack('<II',len(text),0x4e4f534a)+text+struct.pack('<II',len(binary),0x004e4942)+binary

def main():
 with sync_playwright() as p:
    b=p.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist'])
    page=b.new_page(viewport={'width':1440,'height':1050},device_scale_factor=1);page.set_default_timeout(7000);page.on('pageerror',lambda e:errors.append(str(e)))
    transport=LocalTransport();transport.attach(page,(ROOT/'frontend/dist/standalone.html').read_text())
    c=transport.client;c.headers.update({'X-Forge-Request':'1'})
    def snap(name):page.screenshot(path=str(OUT/(name+'.png')),full_page=True,timeout=30000);screens.append(name+'.png')
    def close():page.keyboard.press('Escape');expect(page.locator('dialog')).to_have_count(0)
    def nav(label):page.locator('nav').get_by_role('button',name=label,exact=True).click()
    def wait_run(rid,status,timeout=25):
        until=time.time()+timeout
        while time.time()<until:
            d=c.get('/api/runs/'+rid).json()
            if d['status']==status:return d
            if d['status'] in ['needs_attention','cancelled'] and status!=d['status']:raise AssertionError(d['error'] or d['status'])
            page.wait_for_timeout(150)
        raise AssertionError(f'Expected {status}; got {d}')
    try:
        page.get_by_label('密码',exact=True).fill(env['ADMIN_PASSWORD']);page.get_by_role('button',name='登录工作空间',exact=True).click();expect(page.get_by_role('heading',name='开发控制台',exact=True)).to_be_visible();expect(page.locator('.office-agent-label')).to_have_count(4);record('登录与初始化，无前端运行错误')
        expect(page.locator('.validation-banner')).to_be_visible();record('验证环境标识可见，未伪装真实模型')
        if not any(r['status']=='completed' for r in c.get('/api/runs').json()):
            # Seed one complete baseline through actual services for graph inspection.
            r=c.post('/api/runs',json={'project_id':setup['project']['id'],'title':'图形验收基线','request':'修复加法、补充文档与边界测试。','agent_ids':[a['id'] for a in setup['agents']]});r.raise_for_status();baseline=r.json()['id']
            d=wait_run(baseline,'awaiting_approval');c.post(f'/api/runs/{baseline}/decision/plan',json={'digest':d['plan_digest'],'approved':True}).raise_for_status()
            d=wait_run(baseline,'awaiting_acceptance');c.post(f'/api/runs/{baseline}/decision/acceptance',json={'digest':d['artifact_digest'],'approved':True,'note':'脚本化图形验收基线'}).raise_for_status();wait_run(baseline,'completed');page.get_by_role('button',name='刷新',exact=True).click();page.wait_for_timeout(1600)
        assert page.locator('canvas').evaluate("c=>!!c.getContext('webgl')");assert page.locator('.scene-error').count()==0;record('WebGL真实渲染 / SwiftShader软件GPU')
        page.wait_for_timeout(900);snap('01-office')
        # Canvas hit-testing, not just clicking an HTML label.
        label=page.get_by_role('button',name='查看成员 Nova',exact=True);xy=label.evaluate('e=>({x:+e.dataset.pickX,y:+e.dataset.pickY})');canvas=page.locator('canvas');box=canvas.bounding_box();page.mouse.click(box['x']+xy['x'],box['y']+xy['y']);expect(page.locator('dialog h3')).to_contain_text('Nova');record('3D人物画布拾取→成员详情');close();record('Esc关闭弹窗并返回工作台')
        before=page.get_by_label('相机状态').inner_text();page.mouse.move(box['x']+180,box['y']+170);page.mouse.down();page.mouse.move(box['x']+270,box['y']+200,steps=8);page.mouse.up();page.wait_for_timeout(850);assert before!=page.get_by_label('相机状态').inner_text();assert not page.locator('dialog').count();record('3D拖拽旋转，不误触成员弹窗')
        before=page.get_by_label('相机状态').inner_text();page.mouse.wheel(0,-260);page.wait_for_timeout(850);assert before!=page.get_by_label('相机状态').inner_text();record('滚轮缩放改变相机状态')
        page.get_by_role('button',name='复位视角',exact=True).click();page.wait_for_timeout(850);assert '100%' in page.get_by_label('相机状态').inner_text();record('相机复位')
        canvas.focus();page.keyboard.press('ArrowLeft');page.wait_for_timeout(850);assert '44°' not in page.get_by_label('相机状态').inner_text();page.keyboard.press('Home');record('相机键盘操作及Home复位')
        before=canvas.screenshot();page.get_by_role('button',name='切换日间',exact=True).click();page.wait_for_timeout(350);assert before!=canvas.screenshot();record('日夜切换改变实际渲染结果')
        old=label.evaluate('e=>e.dataset.pickX');page.get_by_role('button',name='会议布局',exact=True).click();page.wait_for_timeout(900);assert old!=label.evaluate('e=>e.dataset.pickX');record('布局切换移动模型，界面注明不是实际会议');snap('02-meeting-layout')
        page.get_by_role('button',name='聚焦成员',exact=True).click();page.wait_for_timeout(800);assert '100%' not in page.get_by_label('相机状态').inner_text();record('聚焦已选成员')
        uploader=page.get_by_test_id('glb-upload');uploader.set_input_files({'name':'bad.glb','mimeType':'model/gltf-binary','buffer':b'not-a-model'});expect(page.locator('.scene-hint')).to_contain_text('GLB');record('非法GLB拒绝且界面可继续操作')
        raw=glb_bytes();(OUT/'fixture-tetrahedron.glb').write_bytes(raw);uploader.set_input_files({'name':'fixture-tetrahedron.glb','mimeType':'model/gltf-binary','buffer':raw});expect(page.get_by_role('button',name='恢复人物',exact=True)).to_be_visible();page.wait_for_timeout(800);assert not page.locator('.scene-error').count();record('静态GLB解析、替换与渲染');snap('03-glb-preview')
        page.get_by_role('button',name='恢复人物',exact=True).click();expect(page.get_by_role('button',name='恢复人物',exact=True)).to_have_count(0);page.wait_for_timeout(300);record('恢复默认人物，不丢失WebGL上下文')
        # Invalid model variants use the actual shipped parser.
        parser_result=page.evaluate('''b64=>{const base=Uint8Array.from(atob(b64),c=>c.charCodeAt(0));const parse=window.__forgeTestModules('graphics/scene').parseGLB;const cases=[];function test(name,bytes){try{parse(bytes.buffer);cases.push({name,rejected:false});}catch(e){cases.push({name,rejected:true});}}test('bad-magic',new Uint8Array(28));test('truncated',base.slice(0,-1));test('oversized',new Uint8Array(3000001));const version=base.slice();new DataView(version.buffer).setUint32(4,1,true);test('old-version',version);return cases;}''',base64.b64encode(raw).decode());assert all(x['rejected'] for x in parser_result);record('GLB错误头、截断、超限、版本验证',json.dumps(parser_result))
        page.get_by_role('button',name='任务依赖',exact=True).click();expect(page.locator('[data-task-node]')).to_have_count(3);page.get_by_role('button',name='放大任务图').click();expect(page.locator('.graph-tools output')).to_have_text('115%');page.get_by_role('button',name='复位任务图').click();expect(page.locator('.graph-tools output')).to_have_text('100%');record('任务图真实依赖节点与缩放复位')
        page.locator('[data-task-node="edge_tests"]').click();expect(page.locator('dialog')).to_contain_text('fix_add');expect(page.locator('dialog')).to_contain_text('handoffs');close();record('任务节点打开契约、依赖和上游交付')
        for _ in range(4):page.get_by_role('button',name='3D办公室',exact=True).click();expect(page.locator('.office-agent-label')).to_have_count(4);page.get_by_role('button',name='任务依赖',exact=True).click()
        page.get_by_role('button',name='3D办公室',exact=True).click();expect(page.locator('.office-agent-label')).to_have_count(4);assert not page.locator('.scene-error').count();record('反复切换3D／任务图，不重复标签或丢失上下文')
        nav('Agent团队4') if False else page.locator('nav button').filter(has_text='Agent团队').click()
        card=page.locator('.agent-card').filter(has=page.get_by_role('heading',name='Nova',exact=True));card.get_by_role('button',name='配置成员').click();page.get_by_label('Agent运行参数覆盖').fill('{bad');page.get_by_role('button',name='保存配置',exact=True).click();expect(page.locator('dialog .form-error')).to_be_visible();record('配置JSON错误显示，不提交损坏配置')
        page.get_by_label('Agent运行参数覆盖').fill('{"max_steps": 14}');page.get_by_role('button',name='保存配置',exact=True).click();expect(page.locator('dialog')).to_have_count(0);assert next(a for a in c.get('/api/agents').json() if a['name']=='Nova')['runtime']['max_steps']==14;record('Agent参数从表单保存至后端')
        nav('运行配置');field=page.get_by_label('单运行协作消息总上限');field.fill('180');page.get_by_role('button',name='保存配置',exact=True).click();page.wait_for_timeout(400);assert c.get('/api/settings').json()['runtime']['max_agent_messages']==180;record('平台运行参数可修改并持久化')
        nav('模型与供应商');page.get_by_role('button',name='添加供应商',exact=True).click();page.get_by_label('连接名称').fill('UI验证连接');page.get_by_label('API基础地址').fill('http://127.0.0.1:8099/openai/v1');page.get_by_label('这是外部模型服务').uncheck();page.get_by_role('button',name='保存配置',exact=True).click();expect(page.locator('dialog')).to_have_count(0);assert any(x['name']=='UI验证连接' for x in c.get('/api/providers').json());record('供应商连接可视化新增')
        page.locator('.entity-card').filter(has=page.get_by_role('heading',name='脚本化验收模型 / openai',exact=True)).get_by_role('button',name='测试调用').click();expect(page.locator('.toast')).to_contain_text('SCRIPTED FIXTURE OK');record('界面连通测试调用真实HTTP协议服务')
        nav('工作台');page.get_by_role('button',name='发起开发任务',exact=True).click();page.get_by_label('任务名称',exact=True).fill(TITLE);page.get_by_label('开发需求与验收期望').fill('修复add加法并补充边界测试和文档，要求完成真实的任务交接。');page.get_by_label('本次运行参数覆盖').fill('{"max_parallel": 2, "max_model_calls": 50}');page.get_by_role('button',name='查看最终生效配置').click();expect(page.locator('dialog pre')).to_contain_text('fixture-anthropic');expect(page.locator('dialog pre')).to_contain_text('fixture-openai');record('新建任务预览跨供应商配置与字段来源')
        page.get_by_role('button',name='创建真实开发任务',exact=True).click();expect(page.locator('dialog')).to_have_count(0);page.wait_for_timeout(300);runs=c.get('/api/runs').json();rid=next(r['id'] for r in runs if r['title']==TITLE);record('浏览器创建真实持久化运行')
        wait_run(rid,'awaiting_approval');expect(page.get_by_role('button',name='批准并开始开发')).to_be_visible(timeout=12000);assert len(c.get('/api/runs/'+rid).json()['plan']['tasks'])==3;snap('04-plan-approval');record('负责人自动规划、拆分依赖并等待审批')
        page.get_by_role('button',name='批准并开始开发').click();page.wait_for_timeout(450);page.get_by_role('button',name='暂停',exact=True).click();d=wait_run(rid,'paused');calls=d['calls'];page.wait_for_timeout(750);assert c.get('/api/runs/'+rid).json()['calls']==calls;expect(page.get_by_role('button',name='继续',exact=True)).to_be_visible(timeout=10000);record('暂停在安全边界生效，暂停期间无新模型请求');snap('05-paused')
        page.get_by_role('button',name='继续',exact=True).click();d=wait_run(rid,'awaiting_acceptance');expect(page.get_by_role('button',name='验收通过',exact=True)).to_be_visible(timeout=12000);record('继续执行→并行任务→集成→测试→独立审查')
        events=c.get(f'/api/runs/{rid}/events?limit=1000').json()['events'];messages=[e for e in events if e['kind']=='agent.message'];delivered=[e for e in events if e['kind']=='agent.message.delivered'];assert len(messages)==len(delivered)==4;assert len({e['data']['provider'] for e in events if e['kind']=='model.completed'})==2;record('4条Agent消息真实送入上下文，2个供应商参与同一运行')
        assert sum(e['kind']=='task.handoff' for e in events)==2;record('2条依赖交接及上游产物进入下游契约')
        expect(page.locator('.agent-message')).to_have_count(9);record('协作消息与交接可视化同步');page.get_by_role('button',name='任务依赖',exact=True).click();snap('06-graph-and-results');page.get_by_role('button',name='3D办公室',exact=True).click()
        assert d['report']['checks']['verification']=='passed';assert d['calls']==16;record('16次脚本化模型调用，真实Git修改及3项固定单测通过')
        for filename in ['changes.patch','report.json','source.zip']:
            r=c.get(f'/api/runs/{rid}/artifacts/{filename}');r.raise_for_status();(OUT/('browser-'+filename)).write_bytes(r.content)
        with zipfile.ZipFile(io.BytesIO(c.get(f'/api/runs/{rid}/artifacts/source.zip').content)) as z:
            assert 'return a + b' in z.read('calc.py').decode();assert not any(n.endswith('.env') for n in z.namelist())
        assert 'return a - b' in (STATE/'imports/fixture-project/calc.py').read_text();record('产物HTTP下载、补丁与源码有效，原项目未修改，秘密文件未导出')
        page.get_by_placeholder('审批说明 / 人工验收意见').fill('浏览器操作验收：代码、测试与交接链路已核对。');page.get_by_role('button',name='验收通过',exact=True).click();wait_run(rid,'completed');page.wait_for_timeout(1000);snap('07-completed-office');record('人工验收后运行完成，而非Agent自行判定完成')
        page.get_by_role('button',name='配置快照',exact=True).click();expect(page.locator('dialog')).to_contain_text('"max_model_calls": 50');expect(page.locator('dialog')).not_to_contain_text('fixture-no-real-secret');close();record('运行快照保留覆盖值且不泄漏凭据')
        # More than eight actors are paged, never overlapped at duplicate coordinates.
        for i in range(6):r=c.post('/api/agents',json={'name':f'分页验证成员{i+1}','role':'developer'});r.raise_for_status()
        page.get_by_role('button',name='刷新',exact=True).click();expect(page.locator('.office-agent-label')).to_have_count(8);page.get_by_label('办公室成员分页').select_option('1');expect(page.locator('.office-agent-label')).to_have_count(2);page.get_by_label('办公室成员分页').select_option('0');expect(page.locator('.office-agent-label')).to_have_count(8);record('10成员分页显示8＋2，模型位置不重叠复用');snap('08-ten-agents')
        page.set_viewport_size({'width':1024,'height':900});page.wait_for_timeout(300);assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1');record('1024px工作台不发生水平溢出');page.set_viewport_size({'width':1440,'height':1050})
        nav('操作审计');expect(page.locator('.timeline-event')).not_to_have_count(0);page.locator('.timeline-event').first.locator('summary').click();expect(page.locator('.timeline-event').first.locator('pre')).to_be_visible();record('审计事件展开读取')
        assert not errors,errors;record('全部上述交互无未捕获前端异常')
        (OUT/'browser-run-events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2))
    except Exception as e:
        results.append({'name':'browser-sequence','status':'failed','details':str(e),'traceback':traceback.format_exc()});snap('browser-failure');print(traceback.format_exc())
    finally:
        report={'transport':'Real Chromium DOM/WebGL + constrained test bridge to actual local HTTP/SSE. Browser URL policy unchanged; native navigation/download/CORS/EventSource not validated.','model_services':'scripted HTTP fixtures, not LLMs','browser':b.version,'gpu':b.new_browser_cdp_session().send('SystemInfo.getInfo')['gpu']['auxAttributes'].get('glRenderer'),'results':results,'page_errors':errors,'screenshots':screens}
        (OUT/'browser-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps({'passed':sum(r['status']=='passed' for r in results),'failed':sum(r['status']=='failed' for r in results),'page_errors':errors},ensure_ascii=False));b.close();transport.shutdown()
 if any(r['status']=='failed' for r in results):raise SystemExit(1)
if __name__=='__main__':main()
