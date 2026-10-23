"""Static contract tests for the PWA shell (assets, headers, snippets, SW).

Scope, stated so the suite cannot be mistaken for more than it is: every
check below inspects shipped *bytes and source text*.  Nothing here installs
the app in a browser, exercises a real service worker, exercises the cache
lifecycle, evaluates the CSP of ``pbatch/ops_serve.py`` or proves the
integrated serving path works.  Those remain manual/acceptance work; see
``docs/PWA_SHELL.md`` ("What these checks do not prove").
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

import pytest

from pbatch import pwa_shell

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSET_DIR = REPO_ROOT / "pbatch" / "pwa_assets"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"

ICON_SIZES = {
    "/pwa/icon-192.png": 192,
    "/pwa/icon-512.png": 512,
}
CONTENT_TYPES = {
    "/manifest.webmanifest": "application/manifest+json",
    "/sw.js": "text/javascript; charset=utf-8",
    "/pwa/icon-192.png": "image/png",
    "/pwa/icon-512.png": "image/png",
}
CACHE_CONTROL = {
    "/manifest.webmanifest": "no-store",
    "/sw.js": "no-store",
    "/pwa/icon-192.png": "public, max-age=86400",
    "/pwa/icon-512.png": "public, max-age=86400",
}
ASSET_FILES = ("manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png")


def _manifest() -> dict:
    body, _headers = pwa_shell.asset(pwa_shell.MANIFEST_URL)
    return json.loads(body.decode("utf-8"))


def _service_worker() -> str:
    body, _headers = pwa_shell.asset(pwa_shell.SERVICE_WORKER_URL)
    return body.decode("utf-8")


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def test_manifest_declares_required_fields() -> None:
    manifest = _manifest()
    assert manifest["name"] == "ai-batch-runner"
    assert manifest["short_name"] == "pi-batch"
    assert manifest["id"] == "/"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert manifest["theme_color"] == pwa_shell.THEME_COLOR
    assert manifest["background_color"] == pwa_shell.BACKGROUND_COLOR
    icons = manifest["icons"]
    assert isinstance(icons, list) and len(icons) == 2
    assert [icon["sizes"] for icon in icons] == ["192x192", "512x512"]
    assert [icon["src"] for icon in icons] == [
        pwa_shell.ICON_192_URL, pwa_shell.ICON_512_URL,
    ]
    for icon in icons:
        assert icon["type"] == "image/png"
        assert icon["purpose"] == "any"


def test_manifest_urls_are_root_relative_and_token_free() -> None:
    manifest = _manifest()
    for url in [manifest["id"], manifest["start_url"], manifest["scope"]]:
        assert url == "/", url
    urls = [icon["src"] for icon in manifest["icons"]]
    for url in urls:
        assert url.startswith("/") and not url.startswith("//"), url
        assert "://" not in url and "?" not in url and "#" not in url, url
        assert url in pwa_shell.ASSETS, url
    text = json.dumps(manifest).lower()
    for banned in ("://", "//", "token", "credential", "cookie", "authorization",
                   "bearer", "localhost", "127.0.0.1", "<script"):
        assert banned not in text, banned


# ---------------------------------------------------------------------------
# icons
# ---------------------------------------------------------------------------


def _png_size(data: bytes) -> tuple:
    assert data.startswith(PNG_SIGNATURE), "missing PNG signature"
    length = struct.unpack(">I", data[8:12])[0]
    assert data[12:16] == b"IHDR" and length == 13, "IHDR must lead the chunks"
    width, height = struct.unpack(">II", data[16:24])
    assert data[24] == 8, "expected 8-bit samples"
    assert data.endswith(PNG_IEND), "missing IEND chunk"
    return width, height


@pytest.mark.parametrize("url,size", sorted(ICON_SIZES.items()))
def test_icon_bytes_are_valid_pngs_of_the_declared_size(url: str, size: int) -> None:
    data, _headers = pwa_shell.asset(url)
    assert _png_size(data) == (size, size)
    name = url.rsplit("/", 1)[-1]
    assert (ASSET_DIR / name).read_bytes() == data
    assert len(data) > len(PNG_SIGNATURE) + len(PNG_IEND)


def test_icons_are_distinct_and_the_asset_directory_is_a_closed_set() -> None:
    icon_192, _ = pwa_shell.asset(pwa_shell.ICON_192_URL)
    icon_512, _ = pwa_shell.asset(pwa_shell.ICON_512_URL)
    assert icon_192 != icon_512
    on_disk = sorted(path.name for path in ASSET_DIR.iterdir() if path.is_file())
    assert on_disk == sorted(ASSET_FILES)


# ---------------------------------------------------------------------------
# asset() contract
# ---------------------------------------------------------------------------


def test_assets_mapping_is_frozen_and_closed() -> None:
    assert set(pwa_shell.ASSETS) == set(CONTENT_TYPES)
    for url in pwa_shell.ASSETS:
        assert len(pwa_shell.ASSETS[url]) == 2
        body, content_type = pwa_shell.ASSETS[url]
        assert body and isinstance(body, bytes)
        assert content_type == CONTENT_TYPES[url]
    with pytest.raises(TypeError):
        pwa_shell.ASSETS["/extra.png"] = (b"x", "image/png")  # type: ignore[index]


@pytest.mark.parametrize("url", sorted(CONTENT_TYPES))
def test_asset_returns_bytes_and_documented_headers(url: str) -> None:
    body, headers = pwa_shell.asset(url)
    assert isinstance(body, bytes) and body
    assert headers["Content-Type"] == CONTENT_TYPES[url]
    assert headers["Cache-Control"] == CACHE_CONTROL[url]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert body == pwa_shell.ASSETS[url][0]
    assert headers is not pwa_shell.asset(url)[1], "headers must be per-call"


@pytest.mark.parametrize("url", [
    "",
    "/",
    "/index.html",
    "/api/overview",
    "/pwa/icon-96.png",
    "/manifest.webmanifest?token=abc",
    "/manifest.webmanifest?",
    "/manifest.webmanifest#install",
    "/pwa/../sw.js",
    "/pwa/./icon-192.png",
    "../pbatch/pwa_assets/sw.js",
    "pwa/icon-192.png",
    "//evil.example/sw.js",
    "https://evil.example/sw.js",
    "/home/u1/abr-wt-pwa/pbatch/pwa_assets/icon-192.png",
    "C:\\pbatch\\pwa_assets\\sw.js",
    "/pwa\\icon-192.png",
    "/manifest.webmanifest\x00.png",
    "/PWA/ICON-192.PNG",
])
def test_asset_rejects_unknown_traversal_query_and_fragment(url: str) -> None:
    with pytest.raises(ValueError):
        pwa_shell.asset(url)


@pytest.mark.parametrize("value", [None, b"/sw.js", 42, ["/sw.js"], {}])
def test_asset_rejects_non_string_paths(value) -> None:
    with pytest.raises(ValueError):
        pwa_shell.asset(value)


def test_asset_fails_closed_on_missing_empty_or_symlinked_asset(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pwa_shell, "_ASSET_DIR", tmp_path)
    with pytest.raises(ValueError):
        pwa_shell._load_assets()

    (tmp_path / "manifest.webmanifest").write_bytes(b"")
    with pytest.raises(ValueError):
        pwa_shell._load_assets()

    outside = tmp_path.parent / "outside-manifest.webmanifest"
    outside.write_text("{}", encoding="utf-8")
    target = tmp_path / "manifest.webmanifest"
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ValueError):
        pwa_shell._load_assets()


# ---------------------------------------------------------------------------
# page snippets
# ---------------------------------------------------------------------------


def test_page_head_contains_exactly_one_local_manifest_meta_and_icon() -> None:
    head = pwa_shell.page_head()
    assert head == pwa_shell.page_head(), "head must be static"
    assert head.count('<link rel="manifest" href="/manifest.webmanifest">') == 1
    assert head.count('<link rel="apple-touch-icon"') == 1
    assert head.count('<meta name="theme-color"') == 1
    assert head.count("<link") == 2 and head.count("<meta") == 1
    hrefs = re.findall(r'href="([^"]+)"', head)
    assert hrefs == ["/manifest.webmanifest", "/pwa/icon-192.png"]
    assert all(href in pwa_shell.ASSETS for href in hrefs)
    theme = re.search(r'<meta name="theme-color" content="([^"]+)">', head)
    assert theme is not None and theme.group(1) == _manifest()["theme_color"]
    for banned in ("://", "//", "token", "authorization", "cookie", "${", "{{", "?"):
        assert banned not in head.lower()


def test_page_script_guards_on_secure_context_and_service_worker() -> None:
    script = pwa_shell.page_script()
    assert script == pwa_shell.page_script(), "script must be static"
    assert script.startswith("(function () {")
    assert script.rstrip().endswith("}());")
    assert script.count("isSecureContext") == 1
    assert '!("serviceWorker" in navigator)' in script
    assert 'navigator.serviceWorker.register("/sw.js", { scope: "/" })' in script
    assert script.count("register(") == 1
    assert ".catch(" in script, "a rejected registration must not escape"
    assert script.count('document.documentElement.dataset.pwa = "unavailable"') == 3


def test_page_script_has_no_logging_storage_or_credential_access() -> None:
    script = pwa_shell.page_script()
    for banned in ("console.", "localStorage", "sessionStorage", "document.cookie",
                   "Authorization", "token", "indexedDB", "alert(", "eval(",
                   "fetch(", "XMLHttpRequest", "document.write", "${", "{{"):
        assert banned not in script, banned
    assert "navigator.serviceWorker.controller" not in script


# ---------------------------------------------------------------------------
# service worker
# ---------------------------------------------------------------------------


def test_service_worker_allowlist_is_closed_and_excludes_api_and_documents() -> None:
    worker = _service_worker()
    allowlist = re.search(r'var PWA_STATIC_PATHS = \[(.*?)\];', worker, re.S)
    assert allowlist is not None
    paths = re.findall(r'"([^"]*)"', allowlist.group(1))
    assert paths == ["/manifest.webmanifest", "/pwa/icon-192.png", "/pwa/icon-512.png"]
    assert set(paths) == {pwa_shell.MANIFEST_URL, pwa_shell.ICON_192_URL,
                          pwa_shell.ICON_512_URL}
    # The worker script itself is never precached or self-served from cache.
    assert pwa_shell.SERVICE_WORKER_URL not in paths
    assert '"/api' not in worker and "/api/" not in worker
    assert '"/"' not in worker and "index.html" not in worker
    assert worker.count("PWA_STATIC_PATHS") == 3


def test_service_worker_bypasses_everything_outside_the_allowlist() -> None:
    worker = _service_worker()
    assert 'request.method !== "GET"' in worker
    assert 'request.mode === "navigate"' in worker
    assert 'request.headers.has("Authorization")' in worker
    assert "url.origin !== self.location.origin" in worker
    assert 'request.url.indexOf("?") !== -1' in worker
    assert 'request.url.indexOf("#") !== -1' in worker
    assert "PWA_STATIC_PATHS.indexOf(url.pathname) !== -1" in worker
    guard = worker.index("if (!pwaOwnsRequest(event.request))")
    respond = worker.index("event.respondWith(")
    assert guard < respond
    assert worker.index("return;", guard) < respond
    assert worker.count("respondWith(") == 1


def test_service_worker_is_cache_first_with_network_fill() -> None:
    worker = _service_worker()
    assert worker.index("cache.match(event.request)") < worker.index(
        "fetch(event.request)")
    assert "cache.put(event.request, response.clone())" in worker
    assert "response.ok" in worker, "only successful responses may be cached"
    for banned in ("skipWaiting", "clientsClaim", "importScripts", "eval(",
                   "localStorage", "sessionStorage", "document.cookie",
                   "http://", "https://", "credentials: \"same-origin\""):
        assert banned not in worker, banned


def test_service_worker_owns_one_cache_and_prunes_only_its_prefix() -> None:
    worker = _service_worker()
    assert re.findall(r'var PWA_CACHE = "([^"]+)";', worker) == ["pi-batch-pwa-v1"]
    assert re.findall(r'var PWA_CACHE_PREFIX = "([^"]+)";', worker) == [
        "pi-batch-pwa-"]
    assert set(re.findall(r'"(pi-batch[^"]*)"', worker)) == {
        "pi-batch-pwa-v1", "pi-batch-pwa-"}
    assert worker.count("caches.open(PWA_CACHE)") == 2
    assert worker.count("caches.delete(") == 1
    prune = re.search(r"names\.filter\(function \(name\) \{(.*?)\}\)", worker, re.S)
    assert prune is not None
    assert "name.indexOf(PWA_CACHE_PREFIX) === 0" in prune.group(1)
    assert "name !== PWA_CACHE" in prune.group(1)
    assert "cache.addAll(PWA_STATIC_PATHS" in worker
    assert 'new Request(path, { cache: "no-store", credentials: "omit" })' in worker


# ---------------------------------------------------------------------------
# packaging + documentation
# ---------------------------------------------------------------------------


def _package_data_block() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(
        r"\[tool\.setuptools\.package-data\](.*?)(?=\n\[|\Z)", text, re.S)
    assert match, "missing [tool.setuptools.package-data] block"
    return match.group(1)


def test_package_data_declares_every_pwa_asset_and_keeps_prior_entries() -> None:
    block = _package_data_block()
    for name in ASSET_FILES:
        assert (ASSET_DIR / name).is_file(), name
        assert '"pwa_assets/%s"' % name in block, name
    for name in ("role_keywords.yaml", "task_keywords.yaml", "README.md"):
        assert '"%s"' % name in block, name
    # Repo invariant (tests/test_packaging.py): package-data stays explicit.
    assert "*" not in block


def test_manifest_in_recursive_include_covers_pwa_assets_and_prior_entries() -> None:
    lines = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
    assert "recursive-include pbatch/pwa_assets *.webmanifest *.js *.png" in lines
    for prefix in ("scripts", "evals", "examples", "agent-engineering", "pipelines",
                   "prompts", "product-specs", "evolution-specs", "ui-specs",
                   "backend-specs"):
        assert any(line.startswith("recursive-include " + prefix + " ")
                   for line in lines), prefix
    assert any(line.startswith("include pi-batch.py") for line in lines)


def test_pwa_shell_doc_separates_static_checks_from_browser_acceptance() -> None:
    doc = (REPO_ROOT / "docs" / "PWA_SHELL.md").read_text(encoding="utf-8")
    for needle in ("/manifest.webmanifest", "/sw.js", "/pwa/icon-192.png",
                   "/pwa/icon-512.png", "no-store", "public, max-age=86400",
                   "X-Content-Type-Options", "pi-batch-pwa-v1", "pi-batch-pwa-",
                   "isSecureContext", "Authorization", "token", "package-data",
                   "MANIFEST.in", "ops_serve.py"):
        assert needle in doc, needle
    lowered = doc.lower()
    assert "not prove" in lowered
    assert "real browser" in lowered
    assert "static" in lowered
