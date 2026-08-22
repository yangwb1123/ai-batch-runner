"""ops serve — 只读 HTTP 监控服务（T16）。

在浏览器/手机实时查看 7×24 任务的运行状态：账本统计、最近 runs、
运营日志、message 索引。纯标准库（http.server + ThreadingHTTPServer），
默认只监听 127.0.0.1；可选 --token 做最简访问控制（只读端点，无写操作）。

端点：
  GET /                       自动刷新监控页（30s 轮询 /api/overview /api/runs）
  GET /api/overview           账本统计 JSON
  GET /api/runs?agent=&limit= 最近 runs JSON
  GET /api/log?grep=&tail=    运营日志尾部 JSON
  GET /api/messages?limit=    message 索引 JSON
所有响应均带 Cache-Control: no-store；路径外请求 404；--token 开启时
要求 ?token= 或 Authorization: Bearer 匹配（恒定时间比较）。
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import config
from . import ops as ops_mod


def _json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
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


class OpsHandler(BaseHTTPRequestHandler):
    """Read-only API + auto-refresh page for the shared run ledger."""

    data_dir = ""
    cwd = ""
    token = ""
    server_version = "pi-batch-ops/1"

    def log_message(self, fmt, *args):  # silence request noise
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not _check_token(self, self.token, query):
            _error(self, "unauthorized", 401)
            return
        route = parsed.path
        if route == "/":
            self._serve_page()
        elif route == "/api/overview":
            _json_response(self, ops_mod.overview(self.data_dir))
        elif route == "/api/runs":
            limit = min(int(_q(query, "limit", "30") or "30"), 200)
            _json_response(self, {"runs": ops_mod.runs(
                self.data_dir, agent=_q(query, "agent"), limit=limit)})
        elif route == "/api/log":
            tail = min(int(_q(query, "tail", "80") or "80"), 500)
            target = _q(query, "file") or ops_mod._default_log_file(self.cwd) or ""
            if not target:
                _error(self, "no log file; start runs with --log-file", 404)
                return
            lines, error = ops_mod.log_tail(target, tail=tail,
                                            grep=_q(query, "grep"))
            if error:
                _error(self, error, 404)
                return
            _json_response(self, {"file": target, "lines": lines})
        elif route == "/api/messages":
            limit = min(int(_q(query, "limit", "5") or "5"), 50)
            _json_response(self, {"messages": ops_mod.message_recent(
                self.data_dir, limit)})
        else:
            _error(self, f"not found: {route}", 404)

    def _serve_page(self) -> None:
        page = ops_mod.report_html(self.data_dir, self.cwd)
        live = page.replace(
            "</head>",
            "<script>setInterval(()=>location.reload(),30000)</script></head>",
            1)
        body = live.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_main(argv: Optional[list[str]] = None) -> int:
    """pi-batch ops serve [--host] [--port] [--token] — 只读监控 HTTP 服务。"""
    parser = argparse.ArgumentParser(
        prog="pi-batch ops serve", description="只读 HTTP 监控服务（web/app 查看任务）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8731)
    parser.add_argument("--token", default="", help="访问令牌（可选）")
    args = parser.parse_args(argv)

    # 只读类属性注入：单进程单服务，生命周期内不变
    OpsHandler.data_dir = str(config.data_dir(os.getcwd()))
    OpsHandler.cwd = os.getcwd()
    OpsHandler.token = args.token
    server = ThreadingHTTPServer((args.host, args.port), OpsHandler)
    print(f"ops serve: http://{args.host}:{args.port}/  "
          f"(data_dir={OpsHandler.data_dir}, "
          f"token={'on' if args.token else 'off'}, read-only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nops serve stopped")
    return 0