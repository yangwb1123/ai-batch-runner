"""Progressive Web App shell assets for the operations Web UI.

The module owns the *closed* set of PWA HTTP routes, the response headers
those routes must carry, and the two HTML snippets an ops page needs to
install the shell.  It does no rendering, no filesystem writes, no network
work and no authentication: the assets are static, same-origin and
token-free by construction.

Invariants (all fail closed):

- ``asset()`` accepts only an exact member of the frozen ``ASSETS`` table.
  Unknown paths, absolute filesystem paths, ``..``/``.`` segments, query
  strings and fragments raise ``ValueError``; there is no path arithmetic
  that could resolve outside ``pbatch/pwa_assets/``.
- An asset that is missing, unreadable, a symlink or empty raises instead
  of producing an empty ``200`` response.  ``ASSETS`` is loaded once at
  import time, so a broken install fails at import rather than silently
  serving nothing.
- No credential, token, cookie or per-request interpolation appears in the
  manifest, the head snippet, the page script or the service worker.

Wiring lives in ``pbatch/ops_pwa.py`` and ``pbatch/ops_serve.py``: the two
HTML snippets are embedded in the global shell, and the four byte routes are
served unauthenticated because they contain no request, task or credential
facts.  See ``docs/PWA_SHELL.md`` for the route/header/CSP contract.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import Dict, Mapping, Tuple

MANIFEST_URL = "/manifest.webmanifest"
SERVICE_WORKER_URL = "/sw.js"
ICON_192_URL = "/pwa/icon-192.png"
ICON_512_URL = "/pwa/icon-512.png"

# Single source of truth for the two colours that also appear in the
# manifest and in the page head; tests keep them in sync.
THEME_COLOR = "#0b1624"
BACKGROUND_COLOR = "#0b1624"

_NO_STORE = "no-store"
_ICON_CACHE = "public, max-age=86400"

#: Directory the asset bytes are read from; never derived from user input.
_ASSET_DIR = Path(__file__).resolve().parent / "pwa_assets"

#: URL path -> (file name inside pbatch/pwa_assets, Content-Type, Cache-Control).
_ROUTES = {
    MANIFEST_URL: ("manifest.webmanifest", "application/manifest+json", _NO_STORE),
    SERVICE_WORKER_URL: ("sw.js", "text/javascript; charset=utf-8", _NO_STORE),
    ICON_192_URL: ("icon-192.png", "image/png", _ICON_CACHE),
    ICON_512_URL: ("icon-512.png", "image/png", _ICON_CACHE),
}  # type: Dict[str, Tuple[str, str, str]]


def _read_asset(name: str) -> bytes:
    """Read one packaged asset, refusing anything that escapes the directory."""
    directory = _ASSET_DIR.resolve()
    path = directory / name
    if path.is_symlink():
        raise ValueError("pwa asset must not be a symlink: " + name)
    resolved = path.resolve()
    if resolved.parent != directory:
        raise ValueError("pwa asset escapes its directory: " + name)
    if not resolved.is_file():
        raise ValueError("pwa asset is missing: " + name)
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise ValueError("pwa asset is unreadable: " + name) from exc
    if not data:
        raise ValueError("pwa asset is empty: " + name)
    return data


def _load_assets() -> Mapping[str, Tuple[bytes, str]]:
    """Build the frozen URL -> (bytes, Content-Type) snapshot."""
    loaded = {}  # type: Dict[str, Tuple[bytes, str]]
    for url in sorted(_ROUTES):
        file_name, content_type, _cache = _ROUTES[url]
        loaded[url] = (_read_asset(file_name), content_type)
    return MappingProxyType(loaded)


#: Frozen mapping of URL path -> (asset bytes, Content-Type).
ASSETS: Mapping[str, Tuple[bytes, str]] = _load_assets()


def _validate_path(url: str) -> str:
    """Return *url* when it is an exact static route, else raise ValueError."""
    if not isinstance(url, str) or not url:
        raise ValueError("pwa asset path must be a non-empty string")
    if "?" in url or "#" in url:
        raise ValueError("pwa asset path must not carry a query or fragment")
    if "\\" in url or "\x00" in url:
        raise ValueError("pwa asset path must not contain backslashes or NUL")
    if not url.startswith("/") or url.startswith("//"):
        raise ValueError("pwa asset path must be root-relative and same-origin")
    if any(segment in (".", "..") for segment in url.split("/")):
        raise ValueError("pwa asset path must not contain relative segments")
    if url not in _ROUTES:
        raise ValueError("unknown pwa asset path: " + url)
    return url


def asset(url: str) -> Tuple[bytes, Dict[str, str]]:
    """Return ``(bytes, headers)`` for a PWA route; raise on anything else.

    Headers always carry ``X-Content-Type-Options: nosniff``.  The manifest
    and the service worker are ``no-store`` (they are the update trigger);
    the icons are immutable-ish and cacheable for one day.
    """
    path = _validate_path(url)
    file_name, content_type, cache_control = _ROUTES[path]
    data, declared_type = ASSETS[path]
    if not data:
        raise ValueError("pwa asset is empty: " + file_name)
    if declared_type != content_type:
        raise ValueError("pwa asset content type drift: " + file_name)
    headers = {
        "Content-Type": content_type,
        "Cache-Control": cache_control,
        "X-Content-Type-Options": "nosniff",
    }
    return data, headers


def page_head() -> str:
    """Return the static head markup an ops page must embed verbatim."""
    return "".join((
        '<link rel="manifest" href="/manifest.webmanifest">',
        '<meta name="theme-color" content="#0b1624">',
        '<link rel="apple-touch-icon" sizes="192x192" href="/pwa/icon-192.png">',
    ))


def page_script() -> str:
    """Return the self-contained service-worker registration snippet.

    Registration happens only in a secure context with a service worker
    API.  Unsupported contexts and rejections are silent by design: the
    only signal is ``document.documentElement.dataset.pwa = "unavailable"``.
    The snippet never logs, never touches storage, cookies or tokens, and
    never throws.
    """
    return "\n".join((
        "(function () {",
        '  "use strict";',
        '  if (!("serviceWorker" in navigator) || !window.isSecureContext) {',
        '    document.documentElement.dataset.pwa = "unavailable";',
        "    return;",
        "  }",
        "  try {",
        '    navigator.serviceWorker.register("/sw.js", { scope: "/" })',
        "      .catch(function (error) {",
        "        /* no logging: the failure marker is the only signal */",
        '        document.documentElement.dataset.pwa = "unavailable";',
        "      });",
        "  } catch (error) {",
        '    document.documentElement.dataset.pwa = "unavailable";',
        "  }",
        "}());",
    ))
