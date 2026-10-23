"""Actual headless Chrome + loopback HTTP; synthetic facts, not phone/production acceptance."""
from __future__ import annotations

import json
import os
import shutil
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from pbatch import ops_serve, task_sources
from pbatch.campaign_state import StateStore

CHROME = next((shutil.which(name) for name in ("google-chrome", "chromium", "chromium-browser")
               if shutil.which(name)), None)
pytestmark = pytest.mark.skipif(CHROME is None, reason="actual Chrome browser not installed")
TOKEN = "synthetic-browser-test-token"


@pytest.fixture
def browser():
    api = pytest.importorskip("playwright.sync_api", reason="optional real-browser test needs Playwright")
    with api.sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=CHROME, headless=True)
        yield browser
        browser.close()


@pytest.fixture
def mobile_server(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "home"))
    data = tmp_path / "runtime"
    data.mkdir()
    source = task_sources.register_source(task_sources.source("synthetic-mobile", str(data)))
    store = StateStore(Path(source["campaign_state"]))
    _record(store, "fast", "RUNNING")
    _record(store, "slow", "RUNNING")

    class Handler(ops_serve.OpsHandler):
        global_tasks = True
        token = TOKEN
        control = None
        cwd = str(tmp_path)
        data_dir = str(data)
        api_requests = 0

        def do_GET(self):
            if self.path.startswith("/api/"):
                type(self).api_requests += 1
            super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:" + str(server.server_address[1]), Handler, store
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()


def _record(store, direction, state):
    scheduling = {"queue_wait_seconds": 1.25, "execution_wall_seconds": None}
    if state == "EXECUTION_COMPLETED":
        scheduling.update(execution_wall_seconds=2.5, execution_status="PASSED")
    store.append({"campaign": "synthetic-browser", "module": "owned-module",
                  "direction_id": direction, "direction": "synthetic-" + direction,
                  "run_id": "synthetic-run", "round_id": "synthetic-round", "status": state,
                  "fingerprint": "synthetic-fingerprint", "evidence": [], "reason": "",
                  "scheduling": scheduling})


def _open(browser, base, suffix=""):
    context = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True,
                                  has_touch=True)
    page = context.new_page()
    page.goto(base + "/" + suffix, wait_until="domcontentloaded", timeout=15000)
    return context, page


def _login(page, token=TOKEN):
    page.locator("#token-input").fill(token)
    page.locator("#token-form").evaluate("form => form.requestSubmit()")
    page.wait_for_function("() => document.querySelectorAll('#tasks tr').length === 2", timeout=15000)


def test_phone_sized_shell_login_live_completion_and_static_worker(browser, mobile_server):
    base, _handler, store = mobile_server
    context, page = _open(browser, base)
    try:
        page.wait_for_function("() => document.querySelector('#connection').textContent.includes('自动刷新已停止')")
        assert page.locator("#tasks tr").count() == 0
        _login(page)
        assert page.locator("#token-input").input_value() == ""
        page.wait_for_function("async () => !!(await navigator.serviceWorker.getRegistration())?.active", timeout=15000)
        _record(store, "fast", "EXECUTION_COMPLETED")
        page.locator("#refresh").click()
        page.wait_for_function("() => document.querySelector('#tasks').textContent.includes('EXECUTION_COMPLETED')")
        assert "RUNNING" in page.locator("#tasks").inner_text()
        assert "执行结果 PASSED（非合并/验收）" in page.locator("#tasks").inner_text()
        assert "1.25" in page.locator("#tasks").inner_text()
        assert "2.5" in page.locator("#tasks").inner_text()
        assert TOKEN not in page.url
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
        _assert_static_cache(page)
        evidence = os.environ.get("PBATCH_BROWSER_EVIDENCE_DIR")
        if evidence:
            target = Path(evidence)
            target.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target / "mobile-progress.png"), full_page=True)
    finally:
        context.close()


def _assert_static_cache(page):
    urls = page.evaluate("""async () => {
      const names = await caches.keys(); const urls = [];
      for (const name of names) {
        const cache = await caches.open(name);
        for (const request of await cache.keys()) urls.push(request.url);
      }
      return urls;
    }""")
    assert urls
    from urllib.parse import urlsplit
    allowed = {"/manifest.webmanifest", "/pwa/icon-192.png", "/pwa/icon-512.png"}
    assert all(urlsplit(url).path in allowed and not urlsplit(url).query for url in urls)


def test_revocation_stops_polling_until_explicit_reconnect(browser, mobile_server):
    base, handler, _store = mobile_server
    context, page = _open(browser, base)
    try:
        _login(page)
        handler.token = "synthetic-rotated-browser-token"
        page.locator("#refresh").click()
        page.wait_for_function("() => document.querySelector('#connection').textContent.includes('自动刷新已停止')")
        assert page.locator("#tasks tr").count() == 0
        denied_count = handler.api_requests
        page.wait_for_timeout(5500)
        assert handler.api_requests == denied_count
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
        page.locator("#refresh").click()
        page.wait_for_timeout(100)
        assert handler.api_requests == denied_count
        _login(page, handler.token)
        assert "最近成功刷新" in page.locator("#connection").inner_text()
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function("() => document.querySelector('#connection').textContent.includes('自动刷新已停止')")
        assert page.locator("#tasks tr").count() == 0
        assert not page.locator("#token-form").is_hidden()
        assert context.cookies() == []
    finally:
        context.close()


def test_mobile_progress_is_readable_without_horizontal_scrolling(browser, mobile_server):
    base, _handler, _store = mobile_server
    context, page = _open(browser, base)
    try:
        _login(page)
        titles = []
        for name in ("synthetic-fast", "synthetic-slow"):
            titles.extend(item for item in page.get_by_text(name, exact=True).all()
                          if item.is_visible())
        assert titles, "the phone must show the primary tasks"
        title = min(titles, key=lambda item: item.bounding_box()["y"])
        assert title.bounding_box()["y"] < 500, "primary progress must not follow pages of metadata"
        box = title.evaluate("""element => {
          let node = element;
          while (node && node.tagName !== 'BODY') {
            if (node.textContent.includes('RUNNING') && node.querySelector('button')) {
              const r = node.getBoundingClientRect();
              return {x: r.x, right: r.right, height: r.height};
            }
            node = node.parentElement;
          }
          return null;
        }""")
        assert box is not None, "task status and detail action must remain together"
        assert box["x"] >= 0 and box["right"] <= 390, "essential task facts must fit the phone"
        assert box["height"] < 650, "short synthetic tasks must not become giant blank rows"
    finally:
        context.close()


def test_mobile_renderer_failure_cannot_publish_a_successful_refresh(browser, mobile_server):
    base, _handler, _store = mobile_server
    context, page = _open(browser, base)
    try:
        _login(page)
        page.wait_for_function("() => document.querySelectorAll('#mobile-tasks article').length === 2")
        page.evaluate("""() => {
          const original = window.pbatchMobileView;
          window.pbatchMobileView = (kind, payload) => {
            original(kind, payload);
            if (kind === 'tasks') throw new Error('synthetic view failure');
          };
        }""")
        with page.expect_response(lambda response: '/api/tasks' in response.url):
            page.locator('#refresh').click()
        page.wait_for_timeout(100)
        assert '连接中断' in page.locator('#connection').inner_text()
        assert page.locator('#tasks tr').count() == 0
        assert page.locator('#mobile-tasks article').count() == 0
        assert page.locator('#summary').text_content() == '数据不可用'
    finally:
        context.close()


def test_legacy_url_token_is_removed_and_data_api_stays_authenticated(browser, mobile_server):
    base, _handler, _store = mobile_server
    context, page = _open(browser, base, "?token=" + TOKEN)
    try:
        page.wait_for_function("() => document.querySelectorAll('#tasks tr').length === 2")
        assert TOKEN not in page.url and "token=" not in page.url
        assert context.request.get(base + "/api/tasks").status == 401
        for path in ("/manifest.webmanifest", "/sw.js", "/pwa/icon-192.png", "/pwa/icon-512.png"):
            response = context.request.get(base + path)
            assert response.status == 200
            assert response.headers["x-content-type-options"] == "nosniff"
        assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
        assert context.cookies() == []
    finally:
        context.close()
