"""Real WebGL2 / local authored-asset smoke test, never a simulated task run."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

base = os.environ.get('FORGE_ASSET_PREVIEW_URL', 'http://127.0.0.1:58431/realistic-assets-preview.html')
output = Path(__file__).resolve().parents[2] / 'docs/evidence/realistic-assets'
output.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', args=['--disable-background-networking'])
    page = browser.new_page(viewport={'width':1600,'height':1000}, device_scale_factor=1)
    errors, failures, remote = [], [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.on('requestfailed', lambda r: failures.append(r.url))
    page.on('request', lambda r: remote.append(r.url) if not r.url.startswith(('http://127.0.0.1:58431/','data:','blob:')) else None)
    page.goto(base)
    page.wait_for_function('window.__realism?.frames > 10', timeout=60000)
    page.wait_for_timeout(1200)
    result = page.evaluate('''()=>({...window.__realism,glError:window.__assetScene.gl.getError(),projectedTarget:window.__assetScene.project(window.__assetScene.target),resources:window.__assetScene.renderer.info.memory})''')
    page.screenshot(path=str(output/'04-office.png'))
    page.get_by_role('button', name='办公椅细节').click()
    page.wait_for_timeout(500)
    page.screenshot(path=str(output/'05-office-chair.png'))
    page.get_by_role('button', name='人物与骨骼').click()
    page.wait_for_timeout(500)
    page.screenshot(path=str(output/'06-person.png'))
    result.update(errors=errors,requestFailures=failures,externalRequests=remote)
    (output/'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(result, ensure_ascii=False))
    assert not errors and not failures and not remote and not result['errors'], result
    assert result['glError']==0 and len(result['loaded'])>=3, result
    assert all(m['maps']>0 and m['triangles']>1000 for m in result['loaded']), result
    assert abs(result['projectedTarget'][0]-800)<1 and abs(result['projectedTarget'][1]-500)<1, result
    browser.close()
