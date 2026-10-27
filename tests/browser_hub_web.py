"""Explicit browser smoke, not default collection: pytest tests/browser_hub_web.py.

Requires Playwright + Chrome/Chromium. Real local browser/HTTP/TLS, synthetic
issuer and token endpoint: NOT real Snaplink login or deployment acceptance.
"""

import json
import shutil
import ssl
import threading
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import expect, sync_playwright

from pbatch.hub_http import HubHTTPServer
from pbatch.hub_store import HubStore
from pbatch.hub_web import HubWeb
from test_hub_web import web_env
from test_hub_auth import signed


def local_tls(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-local-test")])
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(datetime.utcnow() - timedelta(minutes=1))
                   .not_valid_after(datetime.utcnow() + timedelta(hours=1))
                   .sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                         serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_file.chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context


def issuer_page(route):
    query = parse_qs(urlsplit(route.request.url).query)
    assert query["code_challenge_method"] == ["S256"]
    target = query["redirect_uri"][0] + "?" + urlencode({
        "iss": "https://identity.test", "state": query["state"][0], "code": "synthetic-browser-code"})
    route.fulfill(status=200, content_type="text/html", body=(
        '<button id="approve">Synthetic issuer approval</button><script>'
        'document.getElementById("approve").onclick=()=>{location.href=' + json.dumps(target) + ';};</script>'))


@pytest.fixture
def browser_hub(tmp_path, web_env):
    _, auth, transport, settings, _ = web_env
    store = HubStore(tmp_path / "browser.sqlite3")
    store.register_instance("team", "alpha", {"boot_id": "browser-test"})
    server = HubHTTPServer(("127.0.0.1", 0), store, auth, tls_context=local_tls(tmp_path))
    origin = f"https://127.0.0.1:{server.server_port}"
    settings["web"]["public_origin"] = origin
    settings["allowed_origins"] = [origin]
    server.web = HubWeb(settings, auth, transport=transport)
    server.allowed_origins = frozenset([origin])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield origin, transport
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.close()


def test_chrome_sdk_login_and_local_logout(browser_hub):
    origin, transport = browser_hub
    executable = shutil.which("google-chrome") or shutil.which("chromium")
    assert executable, "install Chrome/Chromium before running this explicit smoke"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable, headless=True)
        # Test-only self-signed local cert. Production TLS verification is never disabled.
        context = browser.new_context(ignore_https_errors=True)
        context.route("https://identity.test/**", issuer_page)
        page = context.new_page()
        page.goto(origin)
        page.get_by_role("button", name="使用 Snaplink 登录").click()
        page.get_by_role("button", name="Synthetic issuer approval").click(timeout=5000)
        # Poll the rendered DOM, not a string eval forbidden by the page CSP.
        expect(page.locator("#notice")).to_contain_text("已读取授权窗口", timeout=30_000)
        assert page.locator("#instances").inner_text().startswith("alpha")
        cookies = context.cookies(origin)
        assert len(cookies) == 1 and cookies[0]["httpOnly"] and cookies[0]["secure"]
        page.get_by_role("button", name="退出本地 abr 会话").click()
        expect(page.locator("#notice")).to_contain_text("当前数据不可用", timeout=30_000)
        assert context.cookies(origin) == []
        assert len(transport.calls) == 1
        context.close()
        browser.close()
