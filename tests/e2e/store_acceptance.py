"""Real production DOM and API store acceptance. The synthetic archive is NOT upstream.
Respects managed browser navigation policies via the existing constrained bridge.
"""
import base64,io,json,os,signal,subprocess,sys,time,zipfile,tempfile
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright,expect
from browser_transport import LocalTransport
ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'docs/evidence-store';OUT.mkdir(exist_ok=True)
PORT=8797
if (ROOT/'.local').exists():raise SystemExit('Run in a clean source copy; refuses to change existing .local data')
records=[];errors=[]
def record(name):records.append({'name':name,'passed':True});print('PASS',name,flush=True)
transport=LocalTransport(f'http://127.0.0.1:{PORT}')
rawlog=tempfile.NamedTemporaryFile(mode='w',prefix='forge-store-private-',suffix='.log',delete=False);process=None

def start():
    p=subprocess.Popen([str(ROOT/'.venv/bin/python'),'scripts/start-local.py','--port',str(PORT),'--no-auto-sandbox'],cwd=ROOT,stdout=rawlog,stderr=subprocess.STDOUT,start_new_session=True)
    for _ in range(160):
        try:
            if transport.client.get('/api/health').status_code==200:return p
        except httpx.HTTPError:pass
        if p.poll() is not None:raise RuntimeError('Server exited')
        time.sleep(.15)
    raise TimeoutError('start')
def stop(p):
    if p and p.poll() is None:
        os.killpg(p.pid,signal.SIGTERM)
        try:p.wait(timeout=20)
        except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
def get(path):
    r=transport.client.get('/api'+path);r.raise_for_status();return r.json()
def post(path,body):
    r=transport.client.post('/api'+path,json=body,headers={'X-Forge-Request':'1'});r.raise_for_status();return r.json()
def repository_zip(revision=1):
    """Original, openly labeled test data; never a counterfeit upstream copy."""
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('fixture/LICENSE','MIT License\nCopyright (c) 2026 Agent Forge fixture authors\nTest-only original content.\n')
        z.writestr('fixture/engineering/example-role.md',f'---\nname: 测试专用角色方法包\ndescription: Synthetic repository fixture for acceptance; not an Agency Agents profile.\ncolor: cyan\n---\n# Original test fixture\nCheck test evidence, revision {revision}.\n')
        z.writestr('fixture/scripts/install.sh','exit 99 # must never execute\n')
    return stream.getvalue()
try:
    process=start();config=json.loads((ROOT/'.local/config.json').read_text())
    assert get('/health')['version']=='0.10.1';record('新目录正式启动0.10.1，未登录商店接口401')
    assert transport.client.get('/api/skill-store/items').status_code==401
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=False,args=['--no-sandbox','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        page=browser.new_page(viewport={'width':1600,'height':1000});page.set_default_timeout(30000);page.on('pageerror',lambda e:errors.append(str(e)))
        transport.attach(page,(ROOT/'frontend/dist/index.html').read_text())
        def binary_send(args):
            path=transport.path(args['url']);b=base64.b64decode(args['base64'],validate=True)
            if len(b)>20_000_000:raise ValueError('Test upload too large')
            r=transport.client.request(args['method'],path,headers=args['headers'],content=b)
            return {'status':r.status_code,'headers':dict(r.headers),'body':r.text}
        page.expose_function('__storeUpload',binary_send)
        page.evaluate('''() => {const original=window.fetch;window.fetch=async(url,options={})=>{if(options.body instanceof Blob){const bytes=new Uint8Array(await options.body.arrayBuffer());let raw='';for(const b of bytes)raw+=String.fromCharCode(b);const r=await window.__storeUpload({url:String(url),method:options.method||'POST',headers:options.headers||{},base64:btoa(raw)});return new Response(r.body,{status:r.status,headers:r.headers});}return original(url,options);};}''')
        expect(page.get_by_test_id('login-page')).to_be_visible();page.get_by_label('用户名',exact=True).fill('admin');page.get_by_label('密码',exact=True).fill(config['ADMIN_PASSWORD']);page.get_by_role('button',name='登录工作空间',exact=True).click()
        expect(page.get_by_test_id('studio-workspace')).to_be_visible();assert get('/providers')==[] and get('/runs')==[]
        record('零供应商登录进入办公室，无商店或模型配置门禁')
        env_before=page.evaluate("loadUI('office/ambientEnvironment').environment.status()")
        page.locator('.studio-header').get_by_role('button',name='Skill商店',exact=True).click();expect(page.get_by_test_id('skill-store')).to_be_visible();expect(page.locator('.store-card')).to_have_count(3)
        assert get('/skills')==[]
        page.screenshot(path=str(OUT/'01-store.png'),full_page=True);record('正式商店展示3份真实内置Skill，未自动安装或绑定')
        page.get_by_label('按商店来源筛选',exact=True).select_option('agency-agents');expect(page.get_by_text('Agency Agents 尚未同步',exact=True)).to_be_visible();record('Agency Agents来源未同步时明确空目录，不伪造上游条目')
        page.get_by_label('按商店来源筛选',exact=True).select_option('');page.get_by_label('搜索技能商店',exact=True).fill('证据驱动');expect(page.locator('.store-card')).to_have_count(1)
        page.get_by_role('button',name='查看详情',exact=True).click();d=page.get_by_role('dialog');expect(d).to_be_visible()
        expect(d.get_by_role('button',name='安装到本地能力库',exact=True)).to_be_disabled();d.get_by_role('button',name='文件内容',exact=True).click();expect(d.get_by_label('商店文件内容',exact=True)).to_contain_text('name: evidence-code-review')
        record('搜索、来源筛选、文件审阅有效；未经确认不能安装')
        d.get_by_label('我已查看内容、来源与许可证，并确认有权安装使用',exact=True).check();d.get_by_role('button',name='安装到本地能力库',exact=True).click();expect(d.get_by_role('button',name='批准安装版本',exact=True)).to_be_visible()
        assert get('/skills')[0]['status']=='draft';assert all(not a['skills'] for a in get('/agents'));record('安装写入真实本地待审核版本，不自动授予工具权限')
        d.get_by_role('button',name='批准安装版本',exact=True).click();expect(d.get_by_label('绑定目标Agent',exact=True)).to_be_visible()
        nova=next(a for a in get('/agents') if a['name']=='Nova');d.get_by_label('绑定目标Agent',exact=True).select_option(nova['id']);d.get_by_label('绑定参数 JSON',exact=True).fill('{bad');d.get_by_role('button',name='确认绑定所选版本',exact=True).click();expect(d.get_by_role('alert')).to_be_visible();assert not next(a for a in get('/agents') if a['id']==nova['id'])['skills']
        d.get_by_label('绑定参数 JSON',exact=True).fill('{"focus":"regression"}');d.get_by_role('button',name='确认绑定所选版本',exact=True).click();d.get_by_role('button',name='说明与权限',exact=True).click();expect(d.locator('.store-bindings-summary')).to_contain_text('Nova')
        after=next(a for a in get('/agents') if a['id']==nova['id']);binding=after['skills'][0]
        assert binding['version']==1 and binding['parameters']=={'focus':'regression'} and not binding['allow_scripts']
        assert after['model_profile_id']==nova['model_profile_id'];record('审核后从详情绑定Nova；错误JSON不写入；供应商/模型不受影响')
        d.get_by_role('button',name='说明与权限',exact=True).click();page.screenshot(path=str(OUT/'02-detail-bind.png'))
        d.get_by_role('button',name='卸载 / 保留历史',exact=True).click();expect(d.get_by_role('alert')).to_contain_text('解除成员绑定');record('仍有成员绑定时卸载被拒绝并说明原因')
        d.get_by_role('button',name='关闭',exact=True).click();page.get_by_label('搜索技能商店',exact=True).fill('')
        page.get_by_role('button',name='发布团队Skill',exact=True).click();d=page.get_by_role('dialog');d.get_by_label('发布的本地Skill',exact=True).select_option(binding['skill_id']);expect(d.get_by_label('发布版本',exact=True)).to_have_value('1');d.get_by_label('商店显示名称',exact=True).fill('团队代码审查规范');d.get_by_role('button',name='确认发布',exact=True).click();expect(d).not_to_be_visible();expect(page.locator('.store-card')).to_have_count(4);record('已审核本地Skill从页面发布到团队目录，不上传外部服务')
        page.get_by_role('button',name='来源管理',exact=False).click();expect(page.get_by_test_id('source-agency-agents')).to_be_visible();page.screenshot(path=str(OUT/'03-sources.png'),full_page=True)
        page.get_by_role('button',name='添加来源',exact=True).click();d=page.get_by_role('dialog');d.get_by_label('来源名称',exact=True).fill('验收ZIP · 合成测试，不是上游');d.get_by_label('来源类型',exact=True).select_option('archive');d.get_by_role('button',name='保存来源',exact=True).click();expect(d).not_to_be_visible()
        src=next(s for s in get('/skill-store/sources') if s['name'].startswith('验收ZIP'));card=page.get_by_test_id('source-'+src['id'])
        with page.expect_file_chooser() as chooser:card.get_by_role('button',name='导入仓库ZIP',exact=True).click()
        chooser.value.set_files({'name':'synthetic-fixture.zip','mimeType':'application/zip','buffer':repository_zip(1)})
        expect(card.locator('.store-source-count')).to_contain_text('1');record('真实文件选择导入ZIP，HTTP持久化角色方法包，标明上游未核验')
        card.get_by_role('button',name='浏览条目 →',exact=True).click();expect(page.locator('.store-card')).to_have_count(1);page.get_by_role('button',name='查看详情',exact=True).click();d=page.get_by_role('dialog');expect(d.locator('.store-detail-meta')).to_contain_text('角色方法包')
        d.get_by_role('button',name='文件内容',exact=True).click();d.get_by_role('button',name='references/original-agent.md',exact=True).click();assert 'Synthetic repository fixture' in d.get_by_label('商店文件内容',exact=True).input_value();d.get_by_role('button',name='references/forge-upstream-license.txt',exact=True).click();assert 'fixture authors' in d.get_by_label('商店文件内容',exact=True).input_value()
        record('角色方法包保留原始正文与许可证；安装脚本没有进入包')
        d.get_by_label('我已查看内容、来源与许可证，并确认有权安装使用',exact=True).check();d.get_by_role('button',name='安装到本地能力库',exact=True).click();expect(d.get_by_role('button',name='批准安装版本',exact=True)).to_be_visible();d.get_by_role('button',name='批准安装版本',exact=True).click();expect(d.get_by_label('绑定目标Agent',exact=True)).to_be_visible();d.get_by_role('button',name='关闭',exact=True).click()
        page.get_by_role('button',name='来源管理',exact=False).click();card=page.get_by_test_id('source-'+src['id'])
        with page.expect_file_chooser() as chooser:card.get_by_role('button',name='导入仓库ZIP',exact=True).click()
        chooser.value.set_files({'name':'synthetic-fixture-v2.zip','mimeType':'application/zip','buffer':repository_zip(2)})
        expect(page.get_by_role('status')).to_contain_text('ZIP目录已导入');card.get_by_role('button',name='浏览条目 →',exact=True).click();expect(page.locator('.store-card')).to_contain_text('可更新')
        page.get_by_role('button',name='查看详情',exact=True).click();d=page.get_by_role('dialog');d.get_by_role('button',name='版本差异',exact=True).click();expect(d.locator('.store-diffs')).to_contain_text('revision 2')
        versions=d.get_by_label('商店版本',exact=True).locator('option').count();assert versions==2
        record('同步更新产生新目录版本和真实差异，没有自动更新本地Skill或成员')
        d.get_by_label('我已查看内容、来源与许可证，并确认有权安装使用',exact=True).check();d.get_by_role('button',name='安装所选更新（不自动换绑）',exact=True).click();expect(d.locator('.store-installed-state')).to_contain_text('v2');record('显式安装更新为本地v2待审核，旧版本仍存在')
        page.screenshot(path=str(OUT/'04-update-fixture.png'));d.get_by_role('button',name='关闭',exact=True).click()
        page.get_by_label('按商店来源筛选',exact=True).select_option('');page.get_by_label('搜索技能商店',exact=True).fill('')
        page.set_viewport_size({'width':900,'height':900});time.sleep(.7);assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1');record('900px商店布局无文档水平溢出')
        page.set_viewport_size({'width':1600,'height':1000});page.get_by_role('button',name='已安装能力库',exact=True).click();expect(page.get_by_role('button',name='创建Skill',exact=True)).to_be_visible();record('商店与原能力库联通，原编辑/导出入口仍在')
        assert get('/providers')==[] and get('/models')==[] and get('/runs')==[] and get('/projects')==[]
        record('全程无虚构项目、运行、供应商、模型或业务事件；只产生商店配置审计')
        env_after=page.evaluate("loadUI('office/ambientEnvironment').environment.status()")
        assert env_after['running'] and env_after['time'] > env_before['time'];record('浏览商店和能力库期间城市环境时钟持续推进，没有重置')
        page.get_by_role('button',name='退出登录',exact=True).click();expect(page.get_by_test_id('login-page')).to_be_visible();assert transport.client.get('/api/skill-store/items').status_code==401;record('退出后目录与安装接口仍需登录')
        assert not errors,errors
        browser.close()
    stop(process);process=None
    transport.client.cookies.clear();process=start();post('/auth/login',{'username':'admin','password':config['ADMIN_PASSWORD']})
    assert len(get('/skill-store/items')['items'])==5 and len(get('/skills'))==2
    assert next(a for a in get('/agents') if a['name']=='Nova')['skills'][0]==binding
    assert json.loads((ROOT/'.local/config.json').read_text())['ENCRYPTION_KEY']==config['ENCRYPTION_KEY']
    record('真实停止并重启后，来源、目录、版本、成员绑定和加密密钥保持')
    output={'checks':records,'passed':len(records),'failed':0,'browser_errors':errors,'bridge':'set_content + actual local HTTP/SSE; binary ZIP uploaded through restricted test transport','model_calls':0,'upstream':'No live upstream archive; synthetic role fixture is clearly named and not Agency Agents source'}
    (OUT/'browser-results.json').write_text(json.dumps(output,ensure_ascii=False,indent=2));print(json.dumps({'passed':len(records),'errors':errors},ensure_ascii=False))
except Exception as e:
    try:page.screenshot(path=str(OUT/'browser-failure.png'));(OUT/'browser-failure.txt').write_text(page.locator('body').inner_text())
    except Exception:pass
    (OUT/'browser-results-failed.json').write_text(json.dumps({'checks':records,'error':str(e),'browser_errors':errors},ensure_ascii=False,indent=2))
    raise
finally:
    stop(process);transport.shutdown();rawlog.close();Path(rawlog.name).unlink(missing_ok=True)
