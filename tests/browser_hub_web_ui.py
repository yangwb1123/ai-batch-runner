"""Explicit Chrome UI checks; local Hub/TLS, synthetic issuer or UI API responses.

Never real Snaplink/FRP/production acceptance. Run alongside browser_hub_web.py.
"""
import json
import os
from pathlib import Path
import shutil

import pytest
from playwright.sync_api import expect, sync_playwright

from browser_hub_web import browser_hub
from test_hub_auth import signed
from test_hub_web import web_env


@pytest.fixture
def ui(browser_hub):
    origin, _ = browser_hub
    errors = []
    executable = shutil.which('google-chrome') or shutil.which('chromium')
    assert executable, 'Chrome is required; this smoke does not silently skip'
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=executable, headless=True, chromium_sandbox=True)
        context = browser.new_context(ignore_https_errors=True, viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(type(error).__name__))
        page.on('console', lambda message: errors.append(message.text[:240])
                if message.type == 'error' and 'Content Security Policy' in message.text else None)
        yield page, context, origin, errors
        context.close()
        browser.close()
    assert not errors, errors


def screenshot(page, name, tmp_path):
    target = Path(os.environ.get('PBATCH_WEB_BROWSER_ARTIFACTS', str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(target / name), full_page=True)


def test_iris_styles_theme_keyboard_and_login_state_under_actual_csp(ui, tmp_path):
    page, _, origin, errors = ui
    page.goto(origin)
    expect(page.locator('#notice')).to_contain_text('当前数据不可用')
    expect(page.locator('h1')).to_contain_text('工作台')
    expect(page.locator('[data-iris-card]')).to_be_visible()
    assert page.locator('[data-iris-icon]').count() >= 8
    expect(page.locator('body')).to_have_css('background-color', 'rgb(245, 246, 250)')
    page.keyboard.press('Tab')
    expect(page.locator('.skip-link')).to_be_focused()
    page.keyboard.press('Enter')
    expect(page.locator('main')).to_be_focused()
    page.get_by_role('button', name='切换深色主题').click()
    expect(page.locator('body')).to_have_css('background-color', 'rgb(20, 23, 34)')
    page.get_by_role('button', name='切换浅色主题').click()
    expect(page.locator('body')).to_have_css('background-color', 'rgb(245, 246, 250)')
    assert page.locator('input[type=password]').count() == 0
    assert not errors
    screenshot(page, 'abr-desktop-login.png', tmp_path)


def synthetic_data(route, mode):
    instances = [{'instance_id': 'alpha', 'name': '本地研发实例', 'project_id': 'abr', 'online': True, 'last_seen': 1700000000}]
    sessions = [{'session_id': 'session-1', 'instance_id': 'alpha', 'name': '<script>untrusted</script>', 'status': 'idle', 'updated_at': 1700000000}]
    status = 403 if mode['value'] == 'forbidden' and '/sessions?' in route.request.url else 200
    rows = instances if '/instances?' in route.request.url else sessions
    if mode['value'] == 'long':
        instances[0]['name'] = '超长名称' * 200
        instances[0]['project_id'] = 'project/' * 80
    if mode['value'] == 'empty':
        rows = []
    body = {'error': {'code': 'forbidden'}} if status == 403 else {'data': rows}
    route.fulfill(status=status, content_type='application/json', body=json.dumps(body))


def test_window_filter_text_safety_and_partial_failure_cleanup(ui, tmp_path):
    page, context, origin, _ = ui
    mode = {'value': 'ready'}
    context.route(origin + '/web/api/**', lambda route: synthetic_data(route, mode))
    page.goto(origin)
    expect(page.locator('#notice')).to_contain_text('已读取授权窗口')
    expect(page.locator('#sessions')).to_contain_text('<script>untrusted</script>')
    assert page.locator('#sessions script').count() == 0
    screenshot(page, 'abr-desktop-window.png', tmp_path)
    page.get_by_role('button', name='授权实例', exact=True).click()
    page.get_by_role('searchbox', name='筛选已读取窗口').fill('not-present')
    expect(page.locator('#instances')).to_contain_text('没有匹配的实例')
    page.get_by_role('searchbox', name='筛选已读取窗口').fill('alpha')
    expect(page.locator('#instances')).to_contain_text('alpha')
    mode['value'] = 'forbidden'
    page.locator('#refresh').click()
    expect(page.locator('#notice')).to_contain_text('当前数据不可用')
    expect(page.locator('#instances')).to_contain_text('实例数据尚不可用')
    assert 'alpha' not in page.locator('#instances').inner_text()
    mode['value'] = 'empty'
    page.locator('#refresh').click()
    expect(page.locator('#notice')).to_contain_text('实例 0，会话 0')
    expect(page.locator('#instances')).to_contain_text('没有匹配的实例')
    page.get_by_role('searchbox', name='筛选已读取窗口').fill('')
    expect(page.locator('#instances')).to_contain_text('授权窗口暂无实例')
    expect(page.locator('#notice')).not_to_contain_text('当前数据不可用')


@pytest.mark.parametrize('mode', ['empty', 'long'])
def test_mobile_navigation_no_document_overflow_and_real_empty_state(ui, tmp_path, mode):
    page, context, origin, _ = ui
    context.route(origin + '/web/api/**', lambda route: synthetic_data(route, {'value': mode}))
    page.set_viewport_size({'width': 375, 'height': 900})
    page.goto(origin)
    expect(page.locator('#notice')).to_contain_text('已读取授权窗口')
    assert page.locator('html').evaluate('(element) => element.scrollWidth <= window.innerWidth')
    for name in ('工作台', '授权实例', '会话记录'):
        page.get_by_role('button', name=name, exact=True).click()
        expect(page.locator('h1')).to_contain_text(name)
    page.get_by_role('button', name='工作台', exact=True).click()
    assert page.locator('html').evaluate('(element) => element.scrollWidth <= window.innerWidth')
    if mode == 'empty':
        expect(page.locator('h2#read-title')).to_contain_text('登录已验证')
    else:
        expect(page.locator('#instances')).to_contain_text('alpha')
        assert len(page.locator('#instances .record-meta').inner_text()) == 256
    screenshot(page, 'abr-mobile-' + mode + '.png', tmp_path)
