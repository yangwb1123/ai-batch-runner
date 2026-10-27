"""Same-origin browser facade; never add cookie authority to the bearer API."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from .hub_errors import HubAccessError
from .hub_web import PENDING_TTL, SESSION_TTL

COOKIE = "__Host-abr_session"
_READS = {"/web/api/instances": "/api/v1/agent/instances",
          "/web/api/sessions": "/api/v1/agent/sessions"}


def owns(path):
    path = urlsplit(path).path
    return path == "/" or path.startswith(("/auth/", "/web/"))


def _cookie(headers):
    values = headers.get_all("Cookie", [])
    if len(values) > 1 or sum(map(len, values)) > 4096:
        raise ValueError("invalid cookie header")
    parts = values[0].split(";") if values else []
    if len(parts) > 32:
        raise ValueError("too many cookies")
    matches = [part.strip().partition("=")[2] for part in parts
               if part.strip().partition("=")[0] == COOKIE]
    if len(matches) > 1 or (matches and not re.fullmatch(r"[A-Za-z0-9_-]{43}", matches[0])):
        raise ValueError("invalid browser session cookie")
    return matches[0] if matches else ""


def _headers(handler, web, method):
    if handler.headers.get_all("Host", []) != [web.host]:
        raise HubAccessError("forbidden_host", "browser Host does not match public origin")
    if handler.headers.get_all("Authorization", []):
        raise ValueError("browser routes do not accept bearer headers")
    origins = handler.headers.get_all("Origin", [])
    if origins and origins != [web.origin]:
        raise HubAccessError("forbidden_origin", "browser origin is not allowed")
    if method == "POST" and origins != [web.origin]:
        raise HubAccessError("forbidden_origin", "same-origin POST required")
    lengths = handler.headers.get_all("Content-Length", [])
    if handler.headers.get("Transfer-Encoding") or lengths not in ([], ["0"]):
        handler.close_connection = True
        raise ValueError("browser routes accept no request body")


def _query(raw):
    values = parse_qs(raw, keep_blank_values=True, max_num_fields=8, errors="strict")
    if any(len(items) != 1 for items in values.values()):
        raise ValueError("duplicate browser query parameter")
    return {key: items[0] for key, items in values.items()}


def _redirect(handler, key, age, location="/"):
    handler.send_response(303)
    for name, value in {"Location": location, "Content-Length": "0", "Cache-Control": "no-store",
                        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                        "Set-Cookie": f"{COOKIE}={key}; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age={age}"}.items():
        handler.send_header(name, value)
    handler.end_headers()


def _page(handler):
    from .hub_web_page import page, security_policy
    body = page().encode("utf-8")
    handler.send_response(200)
    # This credential/query-free page needs a same-origin native form Origin.
    # no-referrer makes Chrome send Origin:null. Callbacks/redirects remain no-referrer.
    for name, value in {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body)),
                        "Cache-Control": "no-store", "Referrer-Policy": "same-origin",
                        "X-Content-Type-Options": "nosniff",
                        "Content-Security-Policy": security_policy(handler.server.web.login_origin)}.items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def _read(handler, web, path, query, key):
    allowed = {"after", "limit"} | ({"instance_id"} if path.endswith("sessions") else set())
    limit = query.get("limit", "100")
    if (set(query) - allowed or not limit.isdigit() or len(limit) > 3
            or not 1 <= int(limit) <= 100):
        raise ValueError("invalid browser read query")
    principal = web.principal(key)
    status, payload = handler.server.service.dispatch("GET", _READS[path], query, {}, principal)
    handler._write(status, payload)


def handle(handler, method):
    web = handler.server.web
    if web is None or not owns(handler.path):
        return False
    if len(handler.path) > 8192:
        raise ValueError("browser URL exceeds limit")
    parsed = urlsplit(handler.path)
    if parsed.scheme or parsed.netloc or parsed.fragment or "\\" in handler.path:
        raise ValueError("browser URL must be origin-relative")
    _headers(handler, web, method)
    query, key = _query(parsed.query), _cookie(handler.headers)
    path = parsed.path
    if path == "/auth/callback":
        if method != "GET":
            raise HubAccessError("method_not_allowed", "callback requires GET", 405)
        if set(query) - {"code", "state", "iss", "error", "error_description"}:
            raise ValueError("unsupported callback parameter")
        _redirect(handler, web.complete(key, query), SESSION_TTL)
    elif path in _READS:
        if method != "GET":
            raise HubAccessError("method_not_allowed", "browser data is read-only", 405)
        _read(handler, web, path, query, key)
    else:
        _navigation(handler, web, method, path, query, key)
    return True


def _navigation(handler, web, method, path, query, key):
    if query:
        raise ValueError("browser navigation does not accept query parameters")
    if path == "/" and method == "GET":
        _page(handler)
    elif path == "/auth/login" and method == "POST":
        pending, url = web.begin(key)
        _redirect(handler, pending, PENDING_TTL, url)
    elif path == "/auth/logout" and method == "POST":
        web.clear(key)
        _redirect(handler, "", 0)
    elif path in {"/", "/auth/login", "/auth/logout"}:
        raise HubAccessError("method_not_allowed", "method not allowed", 405)
    else:
        raise HubAccessError("not_found", "browser route not found", 404)
