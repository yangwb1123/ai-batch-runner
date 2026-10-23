"""Public PWA shell serving: exact routes only, auth isolation, bounded headers."""

from __future__ import annotations

import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from pbatch import ops_pwa, ops_serve, pwa_shell
from pbatch.task_center_report import page_html

TOKEN = "private-token"
ASSET_PATHS = (
    "/manifest.webmanifest", "/sw.js", "/pwa/icon-192.png", "/pwa/icon-512.png",
)


def _handler(global_tasks: bool):
    return type("ScopedOpsHandler", (ops_serve.OpsHandler,), {
        "global_tasks": global_tasks, "token": TOKEN, "cwd": "", "data_dir": "",
        "control": None,
    })


@pytest.fixture()
def make_server(tmp_path):
    servers = []

    def start(global_tasks: bool) -> str:
        handler = _handler(global_tasks)
        handler.cwd = str(tmp_path)
        handler.data_dir = str(tmp_path / ("data-global" if global_tasks else "data-work"))
        (tmp_path / ("data-global" if global_tasks else "data-work")).mkdir(exist_ok=True)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        servers.append((httpd, thread))
        return "http://127.0.0.1:%d" % httpd.server_address[1]

    yield start
    for httpd, thread in servers:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _open(base: str, route: str, token: str = "", method: str = "GET"):
    request = urllib.request.Request(base + route, method=method,
                                     data=b"{}" if method == "POST" else None)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    return urllib.request.urlopen(request, timeout=5)


def _rejected(base: str, route: str, token: str = "", method: str = "GET") -> int:
    with pytest.raises(urllib.error.HTTPError) as exc:
        _open(base, route, token, method)
    return exc.value.code


def test_exact_assets_are_public_and_carry_documented_headers(make_server):
    base = make_server(True)
    for route in ASSET_PATHS:
        with _open(base, route) as response:
            assert response.status == 200
            body = response.read()
            headers = response.headers
        expected, declared = pwa_shell.asset(route)
        assert body == expected
        assert headers["Content-Type"] == declared["Content-Type"]
        assert headers["Cache-Control"] == declared["Cache-Control"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["Content-Length"] == str(len(expected))


def test_global_root_is_public_static_and_never_reflects_query_token(make_server):
    base = make_server(True)
    with _open(base, "/") as response:
        plain = response.read()
        csp = response.headers["Content-Security-Policy"]
    with _open(base, "/?token=" + TOKEN) as response:
        with_token = response.read()
    assert plain == with_token
    text = with_token.decode("utf-8")
    assert TOKEN not in text
    assert "全局任务中心" in text
    assert 'type="password"' in text
    assert "/manifest.webmanifest" in text
    assert "/sw.js" in text
    assert "api/tasks" in text
    for directive in ("default-src 'none'", "script-src 'self' 'unsafe-inline'",
                      "connect-src 'self'", "img-src 'self'",
                      "manifest-src 'self'", "worker-src 'self'"):
        assert directive in csp, directive


def test_workspace_root_and_all_api_routes_stay_authenticated(make_server):
    base = make_server(False)
    assert _rejected(base, "/") == 401
    assert _rejected(base, "/api/overview") == 401
    with _open(base, "/", TOKEN) as response:
        page = response.read().decode("utf-8")
    assert "运营控制台" in page
    with _open(base, "/api/overview", TOKEN) as response:
        assert response.status == 200
    # The four static bytes are public in every mode; they carry no facts.
    for route in ASSET_PATHS:
        with _open(base, route) as response:
            assert response.status == 200


def test_global_api_remains_authenticated_even_though_root_is_public(make_server):
    base = make_server(True)
    assert _rejected(base, "/api/tasks") == 401
    with _open(base, "/api/tasks", TOKEN) as response:
        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("route", [
    "/manifest.webmanifest?token=" + TOKEN,
    "/manifest.webmanifest?",
    "/sw.js?x=1",
    "/pwa/icon-192.png?cache=1",
    "/pwa/../sw.js",
    "/pwa/./icon-512.png",
    "/pwa/%2e%2e/sw.js",
    "/pwa/",
    "/manifest.webmanifest/extra",
    "/PWA/ICON-192.PNG",
])
def test_asset_queries_traversal_and_prefix_routes_are_not_public(make_server, route):
    base = make_server(True)
    assert _rejected(base, route) != 200
    assert _rejected(base, route, TOKEN) == 404


def test_public_route_policy_is_an_exact_closed_set():
    assert ops_pwa.public_get("/manifest.webmanifest", True, True) is None
    assert ops_pwa.public_get("/sw.js", True, False) is None
    assert ops_pwa.public_get("/", False, True) == ("page", "/")
    assert ops_pwa.public_get("/", True, True) == ("page", "/")
    assert ops_pwa.public_get("/", False, False) is None
    for route in ASSET_PATHS:
        assert ops_pwa.public_get(route, False, False) == ("asset", route)
        assert ops_pwa.public_get(route, True, False) is None
        assert ops_pwa.public_get(route + "/", False, False) is None
        assert ops_pwa.public_get("/x" + route, False, False) is None
    assert ops_pwa.public_get("/api/tasks", False, True) is None
    assert ops_pwa.public_get("/pwa/icon-96.png", False, True) is None


def test_public_shell_and_assets_leak_no_runtime_task_tenant_or_credential_facts(make_server):
    base = make_server(True)
    with _open(base, "/") as response:
        shell = response.read().decode("utf-8")
    assert TOKEN not in shell
    assert shell == page_html()
    for needle in ("tc-" + "a" * 32, "src-" + "a" * 24, "owned-project"):
        assert needle not in shell, needle
    for route in ASSET_PATHS:
        with _open(base, route) as response:
            body = response.read().decode("utf-8", "replace")
        assert TOKEN not in body and "Bearer" not in body
        assert "api/tasks" not in body


def test_first_inline_script_stays_the_primary_legacy_view():
    html = page_html()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts, "the shell must keep its primary inline script"
    first = scripts[0]
    for needle in ("function renderTask", "function resources", "td.textContent=text",
                   "async function refresh", "setInterval(refresh,5000)",
                   "/api/tasks?limit=200"):
        assert needle in first, needle
    assert 'navigator.serviceWorker.register' not in first
    assert any("navigator.serviceWorker.register" in script for script in scripts[1:])
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids)), "public shell element identities must be unique"
    assert html.count('id="token-input"') == 1
    assert 'id="token-input" type="password"' in html


def test_asset_routes_do_not_accept_post(make_server):
    base = make_server(True)
    assert _rejected(base, "/sw.js", TOKEN, "POST") in (403, 405)
