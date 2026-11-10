"""Packaged Svelte assets, strict CSP, and the existing browser-only boundary."""
import hashlib
import json
import shutil

import pytest

from pbatch import hub_web_assets
from pbatch.hub_web_page import page, security_policy
from test_hub_auth import signed
from test_hub_web import web_env
from test_hub_web_http import browser


def test_compiled_page_has_integrity_bound_resources_and_no_inline_credentials():
    body = page()
    assert '<html lang="zh-CN">' in body
    assert '/web/assets/app.js' in body and '/web/assets/app.css' in body
    assert body.count('integrity="sha256-') == 2
    assert '<noscript>' in body
    for secret in ('access_token', 'client_secret', 'localStorage', 'sessionStorage', 'innerHTML'):
        assert secret not in body
    policy = security_policy('https://identity.test')
    assert 'unsafe-inline' not in policy and 'unsafe-eval' not in policy
    assert "frame-ancestors 'none'" in policy and "connect-src 'self'" in policy
    assert "form-action 'self' https://identity.test" in policy
    assert "script-src 'sha256-" in policy


@pytest.mark.parametrize('name,mime', [('app.js', 'text/javascript'), ('app.css', 'text/css')])
def test_public_assets_are_bounded_real_files_and_never_cookie_authority(browser, name, mime):
    call, _, _ = browser
    status, headers, raw = call('/web/assets/' + name)
    expected, _ = hub_web_assets.asset(name)
    assert status == 200 and raw == expected and raw
    assert headers['Content-Type'].startswith(mime)
    assert headers['Cache-Control'] == 'no-store'
    assert headers['X-Content-Type-Options'] == 'nosniff'
    assert headers['Referrer-Policy'] == 'no-referrer'
    assert 'Set-Cookie' not in headers
    assert call('/web/assets/' + name, headers={'Authorization': 'Bearer ignored'})[0] == 400
    assert call('/api/v1/agent/instances', cookie='__Host-abr_session=ignored')[0] == 401


@pytest.mark.parametrize('path,status', [
    ('/web/assets/app.js?token=secret', 400), ('/web/assets/assets.json', 404),
    ('/web/assets/iris.LICENSE', 404), ('/web/assets/../hub_auth.py', 404),
    ('/web/assets/%2e%2e/hub_auth.py', 404), ('/web/assets/app.js.map', 404),
])
def test_static_endpoint_does_not_become_file_reader(browser, path, status):
    assert browser[0](path)[0] == status


def test_assets_remain_read_only_and_origin_bound(browser):
    call, _, _ = browser
    assert call('/web/assets/app.js', method='POST', headers={'Origin': 'https://abr.test'})[0] == 405
    assert call('/web/assets/app.css', headers={'Host': 'evil.test'})[0] == 403
    assert call('/web/assets/app.js', headers={'Origin': 'https://evil.test'})[0] == 403


@pytest.fixture
def isolated_assets(tmp_path, monkeypatch):
    target = tmp_path / 'assets'
    shutil.copytree(hub_web_assets.ASSET_ROOT, target)
    monkeypatch.setattr(hub_web_assets, 'ASSET_ROOT', target)
    return target


@pytest.mark.parametrize('name', ['index.html', 'app.js', 'app.css', 'iris.LICENSE', 'third-party.LICENSE'])
def test_corrupt_or_missing_required_asset_is_not_served(isolated_assets, name):
    path = isolated_assets / name
    path.write_bytes(b'rejected data')
    with pytest.raises(ValueError, match='asset'):
        page()
    path.unlink()
    with pytest.raises((OSError, ValueError)):
        page()


@pytest.mark.parametrize('change', ['unknown', 'path', 'hash', 'count', 'bool', 'style', 'duplicate'])
def test_malformed_manifest_fails_closed(isolated_assets, change):
    path = isolated_assets / 'assets.json'
    manifest = json.loads(path.read_text())
    if change == 'unknown':
        manifest['unknown'] = True
    elif change == 'path':
        manifest['files']['../hub_auth.py'] = manifest['files'].pop('app.js')
    elif change == 'hash':
        manifest['files']['app.js']['sha256'] = 'not-a-digest'
    elif change == 'count':
        manifest['files']['app.js']['bytes'] = 1048577
    elif change == 'bool':
        manifest['files']['app.js']['bytes'] = True
    elif change == 'style':
        manifest['style_hashes'] = ["unsafe-inline"]
    else:
        path.write_text('{"schema_version":1,"schema_version":1}')
        with pytest.raises(ValueError):
            page()
        return
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        page()


def test_asset_links_and_oversized_manifest_are_rejected(isolated_assets):
    target = isolated_assets / 'app.js'
    original = isolated_assets / 'original.js'
    target.rename(original)
    target.symlink_to(original)
    with pytest.raises((ValueError, OSError)):
        page()
    target.unlink()
    original.rename(target)
    (isolated_assets / 'assets.json').write_bytes(b' ' * 32769)
    with pytest.raises(ValueError):
        page()


def test_all_runtime_assets_are_explicitly_packaged():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    declaration = (root / 'pyproject.toml').read_text()
    manifest = (root / 'MANIFEST.in').read_text()
    for name in ('index.html', 'app.js', 'app.css', 'assets.json', 'iris.LICENSE', 'third-party.LICENSE'):
        assert f'"hub_web_assets/{name}"' in declaration
        raw = (hub_web_assets.ASSET_ROOT / name).read_bytes()
        assert raw and len(hashlib.sha256(raw).hexdigest()) == 64
    assert 'recursive-include pbatch/hub_web_assets *.html *.js *.css *.json *.LICENSE' in manifest
