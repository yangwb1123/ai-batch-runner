"""Finite browser task facade. HubService alone owns authorization and execution rules."""

import hmac
import re
import time
from urllib.parse import unquote

from .hub_errors import HubAccessError
from .hub_oauth_config import strict_json
from .hub_service import PREFIX
from .hub_store_helpers import identifier

_ROUTES = {
    r"/web/api/access": ("GET",),
    r"/web/api/session-operations": ("GET",),
    r"/web/api/task-sessions": ("GET",),
    r"/web/api/(?:sessions|turns|session-requests|session-close-requests)/[^/]+": ("GET",),
    r"/web/api/sessions/[^/]+/events": ("GET",),
    r"/web/api/instances/[^/]+/sessions": ("POST",),
    r"/web/api/sessions/[^/]+/turns": ("GET", "POST"),
    r"/web/api/sessions/[^/]+/turns/[^/]+/file-snapshots(?:/[^/]+)?": ("GET",),
    r"/web/api/sessions/[^/]+/close": ("POST",),
    r"/web/api/turns/[^/]+/cancel": ("POST",),
}


def route_methods(path):
    return next((methods for pattern, methods in _ROUTES.items() if re.fullmatch(pattern, path)), ())


def _query(path, query, method):
    allowed = set()
    tasks = path == "/web/api/task-sessions"
    history = method == "GET" and re.fullmatch(r"/web/api/sessions/[^/]+/turns", path)
    if path.endswith("/events"):
        allowed = {"after", "limit", "turn_id"}
    if history:
        allowed = {"before", "limit", "q", "state", "turn_id"}
    if path == "/web/api/session-operations":
        allowed = {"instance_id", "before", "limit"}
    if tasks:
        allowed = {"instance_id", "after", "limit", "state", "q"}
    if set(query) - allowed:
        raise ValueError("unsupported browser task query")
    if allowed:
        limit = query.get("limit", "20" if history or tasks else "50")
        maximum = 20 if history else 50
        if not re.fullmatch(r"[0-9]{1,2}", limit) or not 1 <= int(limit) <= maximum:
            raise ValueError(f"browser task page limit must be 1..{maximum}")
        query = dict(query, limit=limit)
    return query


def _body_bytes(handler, size):
    deadline = time.monotonic() + 5
    previous = handler.connection.gettimeout()
    chunks, total = [], 0
    try:
        while total < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("browser body deadline exhausted")
            handler.connection.settimeout(remaining)
            chunk = handler.rfile.read1(min(8192, size - total))
            if not chunk:
                raise ValueError("truncated browser body")
            chunks.append(chunk)
            total += len(chunk)
        if time.monotonic() >= deadline:
            raise TimeoutError("browser body deadline exhausted")
        return b"".join(chunks)
    finally:
        handler.connection.settimeout(previous)


def _body(handler):
    lengths = handler.headers.get_all("Content-Length", [])
    if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0]):
        raise ValueError("one bounded Content-Length is required")
    size = int(lengths[0])
    if not 2 <= size <= 65536:
        raise HubAccessError("too_large", "browser JSON body must be 2..65536 bytes", 413)
    types = handler.headers.get_all("Content-Type", [])
    if len(types) != 1 or types[0].lower() not in {"application/json", "application/json; charset=utf-8"}:
        raise HubAccessError("unsupported_media_type", "application/json required", 415)
    if handler.headers.get_all("Content-Encoding", []):
        raise ValueError("browser request encoding is not supported")
    value = strict_json(_body_bytes(handler, size))
    if not isinstance(value, dict):
        raise ValueError("browser task body must be an object")
    return value


def _write_binding(handler, web, key):
    contexts = handler.headers.get_all("X-ABR-Context", [])
    if (len(contexts) != 1 or not re.fullmatch(r"[a-f0-9]{64}", contexts[0])
            or not hmac.compare_digest(contexts[0], web.context(key))):
        raise HubAccessError("web_context_changed", "reload task access before submitting")
    keys = handler.headers.get_all("Idempotency-Key", [])
    if len(keys) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", keys[0]):
        raise ValueError("one bounded task idempotency key is required")
    return keys[0]


def handle(handler, web, method, path, query, key):
    if method not in route_methods(path):
        raise HubAccessError("method_not_allowed", "browser task method not allowed", 405)
    for part in path.split("/")[1:]:
        decoded = identifier(unquote(part, errors="strict"), "route segment")
        if "/" in decoded or "\\" in decoded or decoded in {".", ".."}:
            raise ValueError("invalid browser task path")
    query = _query(path, query, method)
    if "/file-snapshots" in path:
        if not web.file_snapshots:
            raise HubAccessError("web_files_disabled", "browser file snapshots are not enabled")
        if "?" in handler.path or "#" in handler.path:
            raise ValueError("file snapshot reads do not accept query or fragment")
        from .hub_file_snapshot_api import validate_read_headers
        validate_read_headers(PREFIX + path[len("/web/api"):], handler.headers)
    if path == "/web/api/task-sessions":
        from .hub_task_sessions import validate_read_headers
        validate_read_headers(PREFIX + "/task-sessions", handler.headers)
    if path == "/web/api/access":
        principal = web.principal(key)
        if handler.server.service.identity is not None:
            handler.server.service.identity.check(principal)
        handler._write(200, {"data": web.access(key)})
        return
    # No tenant-wide metadata fallback for task content or control, even with a write token.
    principal = web.principal(key, control=True)
    body, idem = {}, ""
    if method == "POST":
        idem = _write_binding(handler, web, key)
        body = _body(handler)
        principal = web.principal(key, control=True)  # Revoke/logout during body upload must win.
    target = PREFIX + path[len("/web/api"):]
    status, payload = handler.server.service.dispatch(method, target, query, body, principal, idem)
    handler._write(status, payload)
