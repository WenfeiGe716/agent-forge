"""Isolated v0.13 browser/API acceptance; no real model calls or production DB access.

Uses installed Chrome and optional test dependency playwright. The temporary API
has dispatch disabled; a loopback fixture only provides a model directory.
"""
import json, os, secrets, socket, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs' / 'evidence-v013'
OUT.mkdir(parents=True, exist_ok=True)
checks, errors, calls = [], [], []

def record(message):
    checks.append(message)
    print('PASS', message, flush=True)

def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]

class Catalog(BaseHTTPRequestHandler):
    def do_GET(self):
        calls.append(self.path)
        data = json.dumps({'data': [{'id': 'fixture-coder', 'name': 'Local fixture coder'}, {'id': 'fixture-reviewer', 'name': 'Local fixture reviewer'}]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        calls.append('UNEXPECTED MODEL CALL ' + self.path)
        self.send_error(500, 'No inference in this browser acceptance')

    def log_message(self, *args):
        pass

with tempfile.TemporaryDirectory(prefix='forge-design-ui-') as state:
    state = Path(state)
    (state / 'imports' / 'fixture').mkdir(parents=True)
    (state / 'imports' / 'fixture' / 'README.md').write_text('Isolated browser fixture.\n')
    catalog = ThreadingHTTPServer(('127.0.0.1', 0), Catalog)
    threading.Thread(target=catalog.serve_forever, daemon=True).start()
    port = free_port()
    password = 'test-' + secrets.token_hex(16)
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'backend'),
           'DATABASE_URL': 'sqlite:///' + str(state / 'forge.db'), 'DATA_ROOT': str(state / 'data'),
           'IMPORT_ROOT': str(state / 'imports'), 'FRONTEND_DIR': str(ROOT / 'frontend' / 'dist'),
           'ADMIN_PASSWORD': password, 'ENCRYPTION_KEY': Fernet.generate_key().decode(),
           'DISABLE_DISPATCH': 'true', 'FORGE_VALIDATION_MODE': 'false',
           'ALLOWED_ORIGINS': f'http://127.0.0.1:{port}', 'SECURE_COOKIE': 'false'}
    log = (state / 'api.log').open('w')
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'forge.api:app', '--host', '127.0.0.1', '--port', str(port)], cwd=state, env=env, stdout=log, stderr=subprocess.STDOUT)
    client = httpx.Client(base_url=f'http://127.0.0.1:{port}', headers={'X-Forge-Request': '1'}, trust_env=False, timeout=25)
    page = None

    def get(path):
        response = client.get('/api' + path)
        response.raise_for_status()
        return response.json()

    def post(path, body):
        response = client.post('/api' + path, json=body)
        if response.is_error:
            raise AssertionError(f'{path}: {response.status_code} {response.text}')
        return response.json()

    try:
        for _ in range(160):
            try:
                if client.get('/api/health').status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if process.poll() is not None:
                raise RuntimeError((state / 'api.log').read_text())
            time.sleep(.15)
        else:
            raise TimeoutError('isolated API startup')
        post('/auth/login', {'username': 'admin', 'password': password})
        provider = post('/providers', {'name': '隔离验收 · 本地目录', 'protocol': 'openai_chat', 'base_url': f'http://127.0.0.1:{catalog.server_port}/v1', 'external': False})
        model = post('/models', {'name': '隔离验收模型 · 无推理', 'provider_id': provider['id'], 'model_id': 'fixture-coder', 'allowed_parameters': ['max_tokens'], 'parameters': {'max_tokens': 2048}, 'supports_tools': True})
        project = post('/projects', {'name': '隔离验收项目', 'import_path': 'fixture', 'check_commands': []})
        viewer_password = 'viewer-' + secrets.token_hex(12)
        post('/users', {'username': 'design-viewer', 'password': viewer_password, 'role': 'viewer'})

        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.getenv('CHROMIUM_PATH', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'), headless=True, args=['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
            page = browser.new_page(viewport={'width': 1536, 'height': 1040})
            page.set_default_timeout(20000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{port}/')
            page.get_by_label('用户名', exact=True).fill('admin')
            page.get_by_label('密码', exact=True).fill(password)
            page.get_by_role('button', name='登录工作空间', exact=True).click()
            page.locator('.studio-header').get_by_role('button', name='设计中心', exact=True).click()
            expect(page.get_by_test_id('design-center')).to_be_visible()
            expect(page.locator('.hub-model')).to_have_count(1)
            expect(page.locator('.hub-score-grid')).to_contain_text('未知')
            page.screenshot(path=str(OUT / '01-model-hub.png'), full_page=True)
            record('正常浏览器导航、登录、设计中心入口与真实模型目录；无评测时显示未知')

            def dialog():
                return page.get_by_role('dialog')

            def save_draft():
                dialog().get_by_role('button', name='保存草稿版本', exact=True).click()
                expect(dialog().get_by_role('button', name='批准并发布此版本', exact=True)).to_be_visible()
                expect(dialog().get_by_role('button', name='批准并发布此版本', exact=True)).to_be_disabled()

            def publish():
                dialog().get_by_label('我已核对此版本的模型、权限、Skill 与配置内容', exact=True).check()
                dialog().get_by_role('button', name='批准并发布此版本', exact=True).click()
                expect(dialog().locator('.design-status')).to_contain_text('已批准发布')

            def close():
                dialog().get_by_role('button', name='关闭', exact=True).click()
                expect(dialog()).not_to_be_visible()

            page.get_by_role('button', name='人工调整画像', exact=True).click()
            for dimension, value in [('coding', '95'), ('reasoning', '92'), ('tool', '94'), ('cost', '80')]:
                dialog().get_by_label(dimension + ' 管理员调整', exact=True).fill(value)
            dialog().get_by_label('人工调整依据', exact=True).fill('隔离验收人工样例；没有真实模型推理评测。')
            save_draft()
            assert get('/model-hub')['models'][0]['capability'] is None
            publish()
            assert get('/model-hub')['models'][0]['capability']['final_scores']['coding'] == 95
            dialog().get_by_role('button', name='编辑并创建新版本', exact=True).click()
            dialog().get_by_label('coding 管理员调整', exact=True).fill('88')
            save_draft()
            assert get('/model-hub')['models'][0]['capability']['final_scores']['coding'] == 95
            dialog().get_by_label('配置历史版本', exact=True).select_option('1')
            expect(dialog().get_by_label('coding 管理员调整', exact=True)).to_have_value('95')
            page.screenshot(path=str(OUT / '02-capability-review.png'))
            close()
            record('管理员修正、审核勾选、发布与历史版本；新草稿未覆盖已发布画像')

            page.get_by_role('button', name='模型路由', exact=False).click()
            page.get_by_label('推荐需求', exact=True).fill('修复支付模块，优先正确性，采用内网模型。')
            page.get_by_role('button', name='生成规则建议草稿', exact=True).click()
            expect(dialog().locator('.design-rationale')).to_contain_text('本地规则建议')
            dialog().get_by_label('设计配置名称', exact=True).fill('支付模块策略')
            dialog().get_by_label('最大并发', exact=True).fill('2')
            save_draft()
            publish()
            dialog().get_by_role('button', name='设为所有新任务的企业规则', exact=True).click()
            expect(page.locator('.design-enterprise')).to_contain_text('支付模块策略')
            routing = next(a for a in get('/design/assets') if a['kind'] == 'routing')
            close()
            page.screenshot(path=str(OUT / '03-routing.png'), full_page=True)
            record('按已发布画像生成规则建议，逐角色修改并发布；企业规则经独立点击启用')

            page.get_by_role('button', name='团队设计', exact=False).click()
            page.get_by_label('推荐需求', exact=True).fill('开发商城系统，需要架构、开发、测试和独立审查。')
            page.get_by_role('button', name='生成规则建议草稿', exact=True).click()
            expect(dialog().locator('.team-node')).to_have_count(4)
            dialog().get_by_label('设计配置名称', exact=True).fill('商城开发团队')
            dialog().get_by_label('当前编辑成员', exact=True).select_option('developer')
            dialog().get_by_label('成员名称', exact=True).fill('Nova API')
            dialog().get_by_label('职责与交付要求', exact=True).fill('开发可测试的商城 API，并记录接口约束。')
            dialog().get_by_label('通信范围', exact=True).select_option('custom')
            dialog().get_by_label('允许通信的成员', exact=True).select_option(['planner', 'tester', 'reviewer'])
            dialog().get_by_label('删除文件', exact=True).uncheck()
            dialog().get_by_label('工位成员', exact=True).select_option('developer')
            dialog().get_by_label('楼层', exact=True).fill('2')
            dialog().get_by_label('办公室楼层', exact=True).select_option('2')
            desk = dialog().locator('.office-desk').filter(has_text='Nova API')
            floor = dialog().locator('.office-floor')
            target = floor.bounding_box()
            desk.drag_to(floor, target_position={'x': target['width'] * .7, 'y': target['height'] * .6})
            expect(dialog().get_by_label('X 坐标', exact=True)).not_to_have_value('0')
            dialog().get_by_label('办公区域', exact=True).fill('二楼开发区')
            page.screenshot(path=str(OUT / '04-team-office-design.png'), full_page=True)
            save_draft()
            publish()
            team = next(a for a in get('/design/assets') if a['kind'] == 'team')
            dialog().get_by_role('button', name='实例化团队并填写任务 →', exact=True).click()
            expect(dialog().get_by_label('团队模板版本', exact=True)).to_have_value(team['id'] + ':' + str(team['version']))
            assert len(get('/agents')) == 8 and not get('/runs')
            record('团队职责、通信白名单、权限收缩、二楼拖放均保存；发布后明确实例化，尚未启动任务')

            dialog().get_by_label('项目', exact=True).select_option(project['id'])
            dialog().get_by_label('任务名称', exact=True).fill('冻结版本验收任务')
            dialog().get_by_label('开发需求与验收期望', exact=True).fill('验收版本冻结行为；不启动任何实际模型执行。')
            dialog().get_by_label('路由策略版本', exact=True).select_option(routing['id'] + ':' + str(routing['version']))
            dialog().get_by_role('button', name='查看最终生效配置', exact=True).click()
            expect(dialog().locator('.json-view')).to_contain_text('runtime')
            expect(dialog().locator('.json-view')).to_contain_text('Nova API')
            page.screenshot(path=str(OUT / '05-runtime-snapshot.png'))
            dialog().get_by_role('button', name='创建任务', exact=True).click()
            expect(dialog()).not_to_be_visible()
            run = get('/runs')[0]
            snapshot = get('/runs/' + run['id'])['snapshot']
            assert snapshot['mode'] == 'runtime'
            assert snapshot['design']['team']['id'] == team['id']
            assert snapshot['design']['routing']['version'] == routing['version']
            assert len(snapshot['agents']) == 4
            assert snapshot['runtime']['max_parallel'] == 2
            record('任务表单绑定准确团队实例与路由版本，配置预览和实际运行持久化保留完整快照')

            page.locator('.sidebar').get_by_role('button', name='设计中心', exact=True).click()
            page.get_by_role('button', name='办公室', exact=False).filter(has_text='办公室').first.click()
            page.get_by_role('button', name='新建办公室布局', exact=True).click()
            dialog().get_by_label('设计配置名称', exact=True).fill('商城办公室布局')
            save_draft()
            publish()
            dialog().get_by_role('button', name='应用办公室布局', exact=True).click()
            expect(page.get_by_role('status')).to_contain_text('已应用')
            assert all(a.get('office_position') for a in get('/agents'))
            close()
            record('办公室布局独立保存、发布和显式应用，真实成员保存工位坐标')

            page.get_by_role('button', name='Benchmark', exact=False).click()
            page.get_by_role('button', name='新建评测套件', exact=True).click()
            dialog().get_by_label('设计配置名称', exact=True).fill('内部业务基础验收案例')
            save_draft()
            publish()
            close()
            page.get_by_role('button', name='Model Hub', exact=True).click()
            page.get_by_role('button', name='运行评测', exact=True).click()
            expect(dialog().get_by_role('button', name='运行评测并保存证据', exact=True)).to_be_disabled()
            expect(dialog().get_by_label('评测套件', exact=True).locator('option')).to_have_count(2)
            close()
            record('可编辑并发布内部评测套件；实际评测需要额外计费确认，本测试未调用')

            page.get_by_role('button', name='供应商与协议配置', exact=True).click()
            page.get_by_role('button', name='添加供应商', exact=True).click()
            expect(dialog().get_by_label('协议适配器', exact=True).locator('option')).to_have_count(5)
            dialog().get_by_label('协议适配器', exact=True).select_option('custom_http')
            expect(dialog().get_by_label('自定义协议 · JSON', exact=False)).to_be_visible()
            page.screenshot(path=str(OUT / '06-custom-protocol.png'))
            close()
            page.get_by_role('button', name='添加模型', exact=True).click()
            expect(dialog().get_by_label('模型ID', exact=False)).to_be_visible()
            assert not any(path.startswith('UNEXPECTED') for path in calls), calls
            close()
            record('供应商表单提供五种协议与自定义映射；模型登记独立进行，未产生推理调用')

            page.locator('.sidebar').get_by_role('button', name='设计中心', exact=True).click()
            for width in [900, 1280]:
                page.set_viewport_size({'width': width, 'height': 960})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            page.screenshot(path=str(OUT / '07-design-responsive.png'), full_page=True)
            record('900px 和 1280px 设计中心布局没有文档水平溢出')

            page.get_by_role('button', name='退出登录', exact=True).click()
            page.get_by_label('用户名', exact=True).fill('design-viewer')
            page.get_by_label('密码', exact=True).fill(viewer_password)
            page.get_by_role('button', name='登录工作空间', exact=True).click()
            page.locator('.studio-header').get_by_role('button', name='设计中心', exact=True).click()
            expect(page.get_by_test_id('design-center')).to_be_visible()
            expect(page.get_by_role('button', name='人工调整画像', exact=True)).to_have_count(0)
            page.get_by_role('button', name='团队设计', exact=False).click()
            expect(page.get_by_role('button', name='新建团队模板', exact=True)).to_have_count(0)
            page.locator('.design-asset').get_by_role('button', name='查看 / 编辑', exact=False).click()
            expect(dialog().locator('.design-editor-actions')).to_have_count(0)
            record('只读账号可审阅设计资产，未显示编辑、发布或实例化动作')
            assert not errors, errors
            browser.close()
        result = {'passed': len(checks), 'failed': 0, 'checks': checks, 'browser_errors': errors,
                  'model_calls': 0, 'catalog_requests': calls, 'isolation': 'temporary database, imports and keys; dispatch disabled; no production writes',
                  'browser': 'installed Chrome headless; actual navigation + local HTTP APIs'}
        (OUT / 'browser-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({'passed': len(checks), 'browser_errors': errors}, ensure_ascii=False))
    except Exception as error:
        if page:
            try:
                page.screenshot(path=str(OUT / 'browser-failure.png'), full_page=True)
                (OUT / 'browser-failure.txt').write_text(page.locator('body').inner_text())
            except Exception:
                pass
        (OUT / 'browser-results-failed.json').write_text(json.dumps({'checks': checks, 'error': str(error), 'browser_errors': errors}, ensure_ascii=False, indent=2))
        raise
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        catalog.shutdown()
        client.close()
        log.close()
