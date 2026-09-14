"""Read-only UI smoke against a real local service (only creates a login session).

No fixture provider, task submission, password reset, or model call is used.
"""
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main():
    root = Path(__file__).resolve().parents[2]
    base = os.environ.get('FORGE_LOGIN_SMOKE_URL', 'http://127.0.0.1:8787')
    if urlparse(base).hostname not in ('127.0.0.1', 'localhost'):
        raise ValueError('This smoke test only accepts a local service')
    config = json.loads((root / '.local/config.json').read_text())
    output = root / '.local/validation/local-login-smoke'
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            headless=True,
        )
        page = browser.new_page(viewport={'width': 1600, 'height': 1000})
        failures, errors = [], []
        page.on('pageerror', lambda error: errors.append(
            getattr(error, 'stack', str(error)).replace(config['ADMIN_PASSWORD'], '<REDACTED>')))
        page.on('response', lambda response: failures.append({
            'path': urlparse(response.url).path, 'status': response.status,
        }) if '/api/' in response.url and response.status >= 400
            and urlparse(response.url).path != '/api/auth/me' else None)
        try:
            page.goto(base)
            page.locator('input[autocomplete="username"]').fill('admin')
            page.locator('input[autocomplete="current-password"]').fill(config['ADMIN_PASSWORD'])
            with page.expect_response(lambda response:
                    urlparse(response.url).path == '/api/auth/login') as login:
                page.get_by_role('button', name='登录工作空间', exact=True).click()
            assert login.value.status == 200, f'Login returned HTTP {login.value.status}'
            page.locator('.studio-header').wait_for()
            page.wait_for_timeout(1500)
            assert not errors, errors
            page.wait_for_function('''() => {
                const office = document.querySelector('[data-testid="office-viewport"]');
                return Number(office?.dataset.frameTotal) > 5;
            }''', timeout=30000)
            page.screenshot(path=str(output / 'office.png'))
            assert not errors and not failures, {'errors': errors, 'failedRequests': failures}
            result = {'loginStatus': 200, 'officeRendered': True,
                      'failedRequests': failures, 'pageErrors': errors}
            (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result))
        except Exception:
            page.screenshot(path=str(output / 'failure.png'))
            print(json.dumps({'pageErrors': errors, 'failedRequests': failures,
                              'sceneErrors': page.locator('.scene-error').all_text_contents()}))
            raise
        finally:
            browser.close()


if __name__ == '__main__':
    main()
