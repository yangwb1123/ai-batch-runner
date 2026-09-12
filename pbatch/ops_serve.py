"""Authenticated operations and mobile control HTTP service.

The default listener is local and read-only.  ``--control`` enables bounded
server-owned agent sessions and prompt submission for a phone-sized web view;
it never exposes a shell or accepts a client-supplied workspace/agent binary.
Remote control requires a token and TLS.  All responses are bounded and use
``Cache-Control: no-store``.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from . import config
from . import ops as ops_mod
from .mobile_control import (
    MobileSessionManager,
    historical_detail,
    historical_sessions,
)


def _json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _error(handler: BaseHTTPRequestHandler, message: str, status: int = 400) -> None:
    _json_response(handler, {"error": message}, status)


def _check_token(handler: BaseHTTPRequestHandler, token: str, query: dict) -> bool:
    if not token:
        return True
    supplied = str(query.get("token", [""])[0])
    auth = handler.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        supplied = auth[len("Bearer "):]
    return hmac.compare_digest(supplied, token)


def _q(query: dict, key: str, default: str = "") -> str:
    values = query.get(key)
    return str(values[0]) if values else default


def _qi(query: dict, key: str, default: int, maximum: int) -> int:
    value = _q(query, key, str(default))
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{key} must be non-negative")
    return min(parsed, maximum)


def _json_body(handler: BaseHTTPRequestHandler, maximum: int = 32 * 1024) -> dict:
    raw_length = handler.headers.get("Content-Length", "")
    try:
        length = int(raw_length)
    except (TypeError, ValueError) as exc:
        raise ValueError("Content-Length is required") from exc
    if length < 0 or length > maximum:
        raise ValueError(f"request body exceeds {maximum} bytes")
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise ValueError("request body is truncated")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    return value


def _token_from_args(args) -> str:
    value = str(args.token or os.environ.get("PBATCH_OPS_TOKEN", "")).strip()
    if value:
        return value
    filename = str(args.token_file or os.environ.get("PBATCH_OPS_TOKEN_FILE", "")).strip()
    if not filename:
        return ""
    path = Path(filename).expanduser()
    if path.is_symlink() or not path.is_file():
        raise ValueError("ops token file must be a regular non-symlink file")
    raw = path.read_bytes()
    if len(raw) > 4096:
        raise ValueError("ops token file exceeds 4096 bytes")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("ops token file is not UTF-8") from exc
    if not value:
        raise ValueError("ops token must not be empty")
    return value


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _safe_log_file(cwd: str, configured: str) -> str:
    """Keep operator log reads inside the workspace or private runtime."""
    target = configured or ops_mod._default_log_file(cwd) or ""
    if not target:
        return ""
    path = Path(target).expanduser()
    if path.is_symlink():
        raise ValueError("log path must not be a symlink")
    resolved = path.resolve()
    roots = (Path(cwd).resolve(), config.data_dir(cwd).resolve())
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise ValueError("log path must stay inside workspace or runtime directory")
    return str(resolved)


class OpsHandler(BaseHTTPRequestHandler):
    """Read-only operations API plus an optional exact-session control API."""

    data_dir = ""
    cwd = ""
    token = ""
    control: Optional[MobileSessionManager] = None
    server_version = "pi-batch-ops/2"

    def log_message(self, fmt, *args):  # silence request noise
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not _check_token(self, self.token, query):
            _error(self, "unauthorized", 401)
            return
        try:
            self._get(parsed.path, query)
        except (TypeError, ValueError) as exc:
            _error(self, str(exc), 400)
        except KeyError as exc:
            _error(self, str(exc).strip("'"), 404)
        except RuntimeError as exc:
            _error(self, str(exc), 409)

    def _get(self, route: str, query: dict) -> None:
        if route == "/":
            self._serve_page()
        elif route == "/api/sessions" or route.startswith("/api/sessions/"):
            self._get_sessions(route, query)
        elif route.startswith("/api/jobs/"):
            self._get_job(route)
        else:
            self._get_standard(route, query)

    def _get_standard(self, route: str, query: dict) -> None:
        if route == "/api/overview":
            _json_response(self, ops_mod.overview(self.data_dir))
        elif route == "/api/runs":
            limit = _qi(query, "limit", 30, 200)
            _json_response(self, {"runs": ops_mod.runs(
                self.data_dir, agent=_q(query, "agent"), limit=limit)})
        elif route == "/api/log":
            self._get_log(query)
        elif route == "/api/messages":
            limit = _qi(query, "limit", 5, 50)
            _json_response(self, {"messages": ops_mod.message_recent(
                self.data_dir, limit)})
        else:
            _error(self, f"not found: {route}", 404)

    def _get_log(self, query: dict) -> None:
        tail = _qi(query, "tail", 80, 500)
        target = _safe_log_file(self.cwd, _q(query, "file"))
        if not target:
            _error(self, "no log file; start runs with --log-file", 404)
            return
        lines, error = ops_mod.log_tail(target, tail=tail, grep=_q(query, "grep"))
        if error:
            _error(self, error, 404)
            return
        _json_response(self, {"file": target, "lines": lines})

    def _get_sessions(self, route: str, query: dict) -> None:
        manager = self._sessions()
        if route == "/api/sessions":
            limit = _qi(query, "limit", 50, 100)
            sessions = manager.list(limit) if manager else historical_sessions(self.cwd, limit)
            _json_response(self, {"sessions": sessions})
            return
        session_id = unquote(route[len("/api/sessions/"):])
        if "/" in session_id or not session_id:
            _error(self, "invalid session path", 400)
            return
        detail = (manager.history_detail(session_id) if manager
                  else historical_detail(self.cwd, session_id))
        _json_response(self, detail)

    def _get_job(self, route: str) -> None:
        manager = self._sessions()
        if manager is None:
            _error(self, "mobile control is disabled", 404)
            return
        job_id = unquote(route[len("/api/jobs/"):])
        _json_response(self, manager.job(job_id))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not _check_token(self, self.token, query):
            _error(self, "unauthorized", 401)
            return
        if not self.token:
            _error(self, "control requires an access token", 403)
            return
        manager = self._sessions()
        if manager is None:
            _error(self, "mobile control is disabled", 403)
            return
        try:
            body = _json_body(self)
            route = parsed.path
            if route == "/api/sessions":
                _json_response(self, manager.create(body.get("name", "")), 201)
                return
            prefix = "/api/sessions/"
            if not route.startswith(prefix):
                _error(self, "not found: " + route, 404)
                return
            suffix = route[len(prefix):].split("/")
            if len(suffix) != 2 or not suffix[0] or suffix[1] not in {"turn", "cancel"}:
                _error(self, "not found: " + route, 404)
                return
            session_id = unquote(suffix[0])
            if suffix[1] == "turn":
                result = manager.start_turn(session_id, body.get("prompt", ""))
                _json_response(self, result, 202)
            else:
                _json_response(self, manager.cancel(session_id), 202)
        except (TypeError, ValueError) as exc:
            _error(self, str(exc), 400)
        except KeyError as exc:
            _error(self, str(exc).strip("'"), 404)
        except RuntimeError as exc:
            _error(self, str(exc), 409)

    def _sessions(self) -> Optional[MobileSessionManager]:
        return type(self).control

    def _serve_page(self) -> None:
        page = ops_mod.report_html(
            self.data_dir, self.cwd, control_enabled=self._sessions() is not None)
        live = page.replace(
            "</head>",
            "<script>setInterval(()=>location.reload(),30000)</script></head>",
            1)
        body = live.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-batch ops serve",
        description="手机/浏览器查看会话；--control 显式开启受保护指令入口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8731)
    parser.add_argument("--token", default="", help="访问令牌；也可用 PBATCH_OPS_TOKEN")
    parser.add_argument("--token-file", default="", help="从非符号链接文件读取访问令牌")
    parser.add_argument("--control", action="store_true",
                        help="开启受保护的移动会话/发送指令 API")
    parser.add_argument("--agent", default=config.AGENT_BIN,
                        help="control session 使用的 agent（仅服务端配置）")
    parser.add_argument("--model", default=config.AGENT_DEFAULT_MODEL)
    parser.add_argument("--provider", default="")
    parser.add_argument("--runtime", default="auto",
                        choices=("auto", "pi", "pi-rpc", "codex", "codex-exec", "legacy"))
    parser.add_argument("--control-timeout", type=int, default=900)
    parser.add_argument("--max-sessions", type=int, default=4)
    parser.add_argument("--tls-cert", default="", help="远程监听所需的 TLS 证书")
    parser.add_argument("--tls-key", default="", help="远程监听所需的 TLS 私钥")
    return parser


def _serve_options(args, parser: argparse.ArgumentParser) -> str:
    try:
        token = _token_from_args(args)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    remote = not _is_loopback(args.host)
    if (args.control or remote) and not token:
        parser.error("--token (or PBATCH_OPS_TOKEN) is required for control/remote access")
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error("--tls-cert and --tls-key must be supplied together")
    if remote and not (args.tls_cert and args.tls_key):
        parser.error("remote ops serve requires --tls-cert and --tls-key")
    return token


def _control_manager(args, cwd: str, parser: argparse.ArgumentParser):
    if not args.control:
        return None
    try:
        return MobileSessionManager(
            cwd, agent=args.agent, model=args.model, provider=args.provider,
            runtime=args.runtime, timeout=args.control_timeout,
            max_sessions=args.max_sessions)
    except (TypeError, ValueError) as exc:
        parser.error(str(exc))


def _run_server(args, cwd: str, token: str, manager) -> None:
    OpsHandler.data_dir = str(config.data_dir(cwd))
    OpsHandler.cwd, OpsHandler.token, OpsHandler.control = cwd, token, manager
    server = None
    try:
        server = ThreadingHTTPServer((args.host, args.port), OpsHandler)
        if args.tls_cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(args.tls_cert, args.tls_key)
            server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = "https" if args.tls_cert else "http"
        mode = "control" if manager is not None else "read-only"
        print(f"ops serve: {scheme}://{args.host}:{args.port}/  "
              f"(data_dir={OpsHandler.data_dir}, token={'on' if token else 'off'}, {mode})")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nops serve stopped")
    finally:
        if server is not None:
            server.server_close()
        OpsHandler.control = None


def serve_main(argv: Optional[list[str]] = None) -> int:
    """Start the local operations page and optional mobile control plane."""
    parser = _serve_parser()
    args = parser.parse_args(argv)
    token = _serve_options(args, parser)
    cwd = os.getcwd()
    manager = _control_manager(args, cwd, parser)
    try:
        _run_server(args, cwd, token, manager)
    finally:
        if manager is not None:
            manager.close()
    return 0