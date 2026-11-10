"""Bounded HTTP listener for the opt-in Agent Hub service.

All business endpoints require a header bearer token. Development credentials
may only bind a literal loopback address; remote listeners require JWT and TLS.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sqlite3
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .hub_auth import HubAuth
from .hub_errors import HubAccessError
from .hub_service import HubService
from .hub_server_config import read_settings as _settings, prepared_components, check_report
from .hub_server_lifecycle import RequestDrain, drain_deadline, termination_signals, serve_until_stopped


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _listener_policy(host, auth, tls_context):
    if auth.mode == "development" and not _loopback(host):
        raise ValueError("development authentication requires a literal loopback listener")
    if not _loopback(host) and tls_context is None:
        raise ValueError("remote Agent Hub listeners require TLS")


def _body(handler: BaseHTTPRequestHandler) -> dict:
    if handler.headers.get("Transfer-Encoding"):
        raise ValueError("transfer encoding is not supported")
    lengths = handler.headers.get_all("Content-Length", [])
    if len(lengths) != 1 or not lengths[0].isdigit() or len(lengths[0]) > 6:
        raise ValueError("one bounded Content-Length is required")
    size = int(lengths[0])
    from .hub_workspace import request_limit
    if size > request_limit(getattr(handler, "path", "")):
        raise HubAccessError("too_large", "request exceeds route size limit", 413)
    if handler.headers.get_content_type() != "application/json":
        raise HubAccessError("unsupported_media_type", "application/json required", 415)
    from .hub_file_snapshot_http import is_upload_path, read_upload_bytes
    raw = read_upload_bytes(handler, size) if is_upload_path(getattr(handler, "path", "")) else handler.rfile.read(size)
    if len(raw) != size:
        raise ValueError("truncated request body")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    path = urlsplit(getattr(handler, "path", "")).path.rstrip("/")
    if size > 65536 and path.endswith("/gateway/compute") and value.get("operation") != "snapshot-upload":
        raise HubAccessError("too_large", "compute operation exceeds 64 KiB", 413)
    return value


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _query(raw: str, *, strict=False) -> dict:
    values = parse_qs(raw, keep_blank_values=True, max_num_fields=10,
                      errors="strict" if strict else "replace")
    if any(len(items) != 1 for items in values.values()):
        raise ValueError("duplicate query parameter")
    if "token" in values or "access_token" in values:
        raise ValueError("credentials must only appear in Authorization headers")
    return {key: items[0] for key, items in values.items()}


def _strict_query_path(path, method="GET"):
    from .hub_compute_placement import is_path as placement_path
    from .hub_task_sessions import is_path as task_path
    from .hub_file_snapshot_api import is_path as file_path
    from .hub_turn_history import is_path as history_path
    return placement_path(path) or task_path(path) or file_path(path) or (method == "GET" and history_path(path))


class HubHandler(BaseHTTPRequestHandler):
    server_version = "AgentHub/1"

    def setup(self):
        self.request.settimeout(15)
        super().setup()

    def log_message(self, fmt, *args):
        """Never copy URL parameters or credentials into access logs."""

    def _origin(self) -> str:
        origin = self.headers.get("Origin", "")
        if origin and origin not in self.server.allowed_origins:
            raise HubAccessError("forbidden_origin", "origin is not allowed")
        return origin

    def _write(self, status: int, payload: dict, *, sse: bool = False) -> None:
        if sse:
            body = self._event_page(payload)
            content_type = "text/event-stream; charset=utf-8"
        else:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            content_type = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(body)))
        if status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="agent-hub"')
        origin = self.headers.get("Origin", "")
        if origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _public_read(self, path: str, query: dict) -> None:
        if path == "/healthz":
            self._write(200, {"data": {"status": "ok"}})
            return
        from .hub_task_center_api import validate_read_headers
        from .hub_task_center_report import page_html, security_policy
        if query:
            raise ValueError("task viewer shell does not accept query parameters or credentials")
        validate_read_headers(path, self.headers)
        compute = path.startswith("/api/v1/agent/compute-task-center/")
        source_sessions = path == "/api/v1/agent/compute-task-center/source-sessions/view"
        body = page_html(compute=compute, source_sessions=source_sessions).encode("utf-8")  # no principal/store/controller
        self.send_response(200)
        for key, value in {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(body)),
                           "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                           "X-Content-Type-Options": "nosniff", "Content-Security-Policy": security_policy(compute=compute, source_sessions=source_sessions)}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _event_page(payload: dict) -> bytes:
        # Finite replay pages bound server threads. Clients reconnect using the
        # last received id; JSON polling and SSE expose identical durable cursors.
        lines = ["retry: 1000\n\n"]
        for event in payload["data"]:
            serialized = json.dumps(event, ensure_ascii=False, allow_nan=False)
            lines.append(f"id: {event['cursor']}\nevent: agent.event\ndata: {serialized}\n\n")
        if not payload["data"]:
            lines.append(": no new events\n\n")
        return "".join(lines).encode("utf-8")

    def _request(self, method: str) -> None:
        from .hub_store import HubStoreError
        try:
            from .hub_web_http import handle
            if handle(self, method):
                return
            self._origin()
            parsed = urlsplit(self.path)
            query = _query(parsed.query, strict=_strict_query_path(parsed.path, method))
            if method == "GET" and parsed.path in {"/healthz", "/api/v1/agent/task-center/view",
                    "/api/v1/agent/compute-task-center/view", "/api/v1/agent/compute-task-center/source-sessions/view"}:
                self._public_read(parsed.path, query)
                return
            if len(self.headers.get_all("Authorization", [])) != 1:
                raise HubAccessError("unauthorized", "one Bearer token is required", 401)
            principal = self.server.auth.authenticate(self.headers.get("Authorization", ""))
            from .hub_file_snapshot_http import validate_headers
            validate_headers(self.path, self.headers, method)
            if method == "GET":
                from .hub_session_request_api import validate_read_headers
                from .hub_compute_placement import validate_read_headers as validate_placement_headers
                from .hub_task_center_api import validate_read_headers as validate_task_headers
                validate_read_headers(parsed.path, self.headers)
                validate_placement_headers(parsed.path, self.headers)
                from .hub_task_sessions import validate_read_headers as validate_discovery_headers
                validate_task_headers(parsed.path, self.headers)
                validate_discovery_headers(parsed.path, self.headers)
            sse = (method == "GET" and parsed.path.endswith("/events")
                   and "text/event-stream" in self.headers.get("Accept", ""))
            if sse and "after" not in query and self.headers.get("Last-Event-ID"):
                query["after"] = self.headers["Last-Event-ID"]
            body, principal = self._request_body(method, principal)
            keys = self.headers.get_all("Idempotency-Key", [])
            if len(keys) > 1:
                raise ValueError("duplicate Idempotency-Key")
            status, result = self.server.service.dispatch(
                method, parsed.path, query, body, principal, keys[0] if keys else "")
            self._write(status, result, sse=sse)
        except (HubAccessError, HubStoreError) as exc:
            self._write(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            self._write(400, {"error": {"code": "invalid_request", "message": "invalid request"}})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except (OSError, sqlite3.Error):
            self._write(503, {"error": {"code": "store_unavailable", "message": "Hub unavailable"}})

    def _request_body(self, method, principal):
        if method != "POST":
            return {}, principal
        body = _body(self)
        from .hub_file_snapshot_http import is_upload_path
        if is_upload_path(self.path):
            principal = self.server.auth.authenticate(self.headers.get("Authorization", ""))
        return body, principal

    def do_GET(self):
        self._request("GET")

    def do_POST(self):
        self._request("POST")

    def do_PUT(self):
        from .hub_web_http import owns
        if self.server.web is not None and owns(self.path):
            self._request(self.command)
            return
        from .hub_session_close_api import is_route
        from .hub_compute_placement import is_route as placement_route
        from .hub_task_center_api import is_route as observer_route
        from .hub_file_snapshot_api import is_route as file_route
        from .hub_service import PREFIX
        from urllib.parse import unquote
        path = urlsplit(self.path).path
        parts = [unquote(part) for part in path[len(PREFIX) + 1:].split("/")]
        if path.startswith(PREFIX + "/") and (
                is_route(parts) or placement_route(parts) or observer_route(parts, include_views=True) or file_route(parts)
                or parts in (["session-operations"], ["task-sessions"], ["gateway", "session-close-requests"])):
            self._request(self.command)
        else:
            self.send_error(501, "Unsupported method")

    do_PATCH = do_DELETE = do_HEAD = do_PUT

    def do_OPTIONS(self):
        from .hub_web_http import owns
        if self.server.web is not None and owns(self.path):
            self._request("OPTIONS")
            return
        try:
            self._origin()
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", ""))
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             "Authorization, Content-Type, Idempotency-Key, Last-Event-ID")
            self.send_header("Content-Length", "0")
            self.end_headers()
        except HubAccessError as exc:
            self._write(exc.status, {"error": {"code": exc.code, "message": str(exc)}})


class HubHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, auth: HubAuth, *, allowed_origins=(), tls_context=None,
                 compute=None, integrations=None, identity=None, web=None):
        host, _port = address
        _listener_policy(host, auth, tls_context)
        self.service = HubService(store, compute, integrations=integrations, identity=identity)
        self.auth = auth
        self.allowed_origins = frozenset(allowed_origins)
        if web is not None and (web.auth is not auth or web.origin not in self.allowed_origins):
            raise ValueError("browser facade must share Hub auth and its allowed origin")
        self.web = web
        self._requests = RequestDrain()
        self._stop_signal = None
        super().__init__(address, HubHandler)
        if tls_context is not None:
            self.socket = tls_context.wrap_socket(self.socket, server_side=True,
                                                do_handshake_on_connect=False)

    def process_request(self, request, client_address):
        if (self._stop_signal is not None and self._stop_signal.number) or not self._requests.admit(request):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._requests.complete(request)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._requests.complete(request)

    def close_request(self, request):
        self._requests.close_transport(request)

    def handle_error(self, request, client_address):
        # Exceptions must not expose prompts, auth headers or database paths.
        self.shutdown_request(request)

    def server_close(self, *, drain_timeout=15.0):
        deadline = drain_deadline(drain_timeout)
        self._requests.stop()
        try:
            super().server_close()
            self._requests.wait(deadline)
        finally:
            if self.web is not None:
                self.web.close()


def _tls(args):
    if bool(args.cert_file) != bool(args.key_file):
        raise ValueError("TLS requires both cert-file and key-file")
    if not args.cert_file:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert_file, args.key_file)
    return context


def hub_serve_main(argv=None) -> int:
    from . import config
    parser = argparse.ArgumentParser(prog="abr fleet serve", description=__doc__)
    parser.add_argument("--config", required=True, help="Hub identity/resource grant YAML")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--database", default="", help="private SQLite store path")
    parser.add_argument("--cert-file", default="")
    parser.add_argument("--key-file", default="")
    parser.add_argument("--check-config", action="store_true",
                        help="validate local settings/assets only; no network, listener or database")
    parser.add_argument("--json", action="store_true", help="JSON result for --check-config")
    args = parser.parse_args(argv)
    try:
        if args.json and not args.check_config:
            raise ValueError("--json requires --check-config")
        if not 1 <= args.port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        settings = _settings(args.config)
        auth = HubAuth(settings["auth"])
        tls_context = _tls(args)
        _listener_policy(args.host, auth, tls_context)
        with prepared_components(settings, auth) as prepared:
            if args.check_config:
                report = check_report(prepared, auth)
                print(json.dumps(report) if args.json else "Hub config: valid locally; external acceptance not executed")
                return 0
            path = args.database or str(config.data_dir(os.getcwd()) / "hub" / "hub.sqlite3")
            _run_server(args, path, settings, auth, tls_context, prepared)
        return 0
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        if args.check_config:
            report = {"schema": "abr.hub.config-check/v1", "result": "failed",
                      "error": "invalid_local_configuration",
                      "reason": "Configuration, local credentials, listener policy or Web assets are invalid/unavailable"}
            print(json.dumps(report) if args.json else report["reason"])
            return 2
        parser.error(str(exc))


def _run_server(args, path, settings, auth, tls_context, prepared) -> None:
    from .hub_store import HubStore
    from .hub_compute import HubCompute
    from .hub_integrations import HubIntegrations
    with termination_signals() as stop:
        store = HubStore(path)
        compute = integrations = None
        try:
            if stop.number:
                return
            compute = HubCompute(store, configured=prepared.compute)
            integrations = HubIntegrations(store, configured=prepared.sinks)
            with HubHTTPServer((args.host, args.port), store, auth, tls_context=tls_context,
                               allowed_origins=settings.get("allowed_origins", []), compute=compute,
                               integrations=integrations, identity=prepared.identity, web=prepared.web) as server:
                if stop.number:
                    return
                compute.start()
                integrations.start()
                scheme = "https" if tls_context else "http"
                print(f"Agent Hub listening on {scheme}://{args.host}:{args.port}", flush=True)
                serve_until_stopped(server, stop)
        finally:
            try:
                if compute is not None:
                    compute.close()
            finally:
                try:
                    if integrations is not None:
                        integrations.close()
                finally:
                    store.close()
                    store.require_drained()
