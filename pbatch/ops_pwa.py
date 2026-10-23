"""Public PWA shell routes for ``pi-batch ops serve``.

Exactly two kinds of GET are reachable without the ops token:

* the **global** task-center document ``/`` (static markup, no task data);
* the four frozen ``pwa_shell.ASSETS`` byte paths, and only when the request
  path is exact (no query string, fragment, trailing slash or prefix).

Everything else -- workspace ``/``, every ``/api/*`` route and any asset-like
path with extra segments or a query -- stays behind the existing token check.
The assets are static bytes: a manifest, a service worker and two icons.  They
never contain a token, session, tenant, workspace, task or runtime fact.
"""

from __future__ import annotations

from typing import Optional, Tuple

from . import pwa_shell

#: Exact static byte routes; membership is checked against the frozen table.
ASSET_PATHS = frozenset(pwa_shell.ASSETS)

#: CSP for the global shell.  Same-origin only; the service worker, manifest
#: and icons are the minimal additions needed for installability.
GLOBAL_CSP = (
    "default-src 'none'; script-src 'self' 'unsafe-inline'; "
    "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; "
    "manifest-src 'self'; worker-src 'self'"
)

#: Workspace page CSP stays at its pre-PWA surface.
WORKSPACE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'"
)

_PAGE = "page"
_ASSET = "asset"


def public_get(route: str, has_suffix: bool, global_tasks: bool) -> Optional[Tuple[str, str]]:
    """Return ``(kind, route)`` for an unauthenticated GET, else ``None``.

    ``has_suffix`` must be true when the raw request target carried ``?`` or
    ``#``.  Asset queries/fragments are never public; the global document may
    carry the legacy ``?token=`` and is still only static markup.
    """
    if global_tasks and route == "/":
        return (_PAGE, route)
    if not has_suffix and route in ASSET_PATHS:
        return (_ASSET, route)
    return None


def csp(global_tasks: bool) -> str:
    """Return the Content-Security-Policy for the page in this mode."""
    return GLOBAL_CSP if global_tasks else WORKSPACE_CSP


def send_asset(handler, route: str) -> bool:
    """Write one PWA asset; return False when the route is not servable."""
    try:
        body, headers = pwa_shell.asset(route)
    except ValueError:
        return False
    handler.send_response(200)
    for name, value in headers.items():
        handler.send_header(name, value)
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    return True


def send_html(handler, body: str, csp_value: str) -> None:
    """Write the page document with no-store and the requested CSP."""
    payload = body.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Security-Policy", csp_value)
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)
