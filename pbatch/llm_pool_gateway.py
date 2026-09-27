"""Authenticated loopback Go gateway with pre-response quota failover only."""

from __future__ import annotations

import hashlib
import hmac
import http.client
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import socket
import threading
import time
from types import MappingProxyType

from .llm_pool_config import GO_ORIGIN, PoolConfig
from .llm_pool_go import PoolFailure, classify_failure
from .llm_pool_secrets import read_secret
from .llm_pool_snapshot import SNAPSHOT_HEADER, snapshot_tag
from .llm_pool_store import PoolStore
from .llm_pool_transport import UpstreamRequest, _origin
from .llm_pool_usage_refresh import prepare_usage


MAX_REQUEST = 2 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
_FORWARDED = ("x-opencode-session", "anthropic-version", "anthropic-beta")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")


def _validate_request(body: bytes, route: str, models: dict) -> str:
    data = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(data, dict) or not isinstance(data.get("model"), str):
        raise ValueError("request must include a configured model")
    model = data["model"]
    if models.get(model) != route:
        raise ValueError("model is not configured for this protocol")
    if "stream" in data and type(data["stream"]) is not bool:
        raise ValueError("stream must be boolean")
    if route == "responses":
        if data.get("previous_response_id") or data.get("conversation") or data.get("background"):
            raise ValueError("pool requires full-history foreground Responses requests")
        items = data.get("input", [])
        if isinstance(items, list) and any(
                isinstance(item, dict) and item.get("type") == "item_reference" for item in items):
            raise ValueError("stored response references cannot cross accounts")
    return model


class PoolGateway(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, settings: PoolConfig, *, port=None, upstream_url=GO_ORIGIN):
        self.settings = replace(settings, models=MappingProxyType(dict(settings.models)))
        _origin(upstream_url)
        self.token = read_secret(settings.token_file)
        self.keys = MappingProxyType(_load_keys(self.settings, token=self.token))
        self.store = PoolStore(settings.state_file)
        self.upstream_url = upstream_url
        self.slots = threading.BoundedSemaphore(settings.max_connections)
        super().__init__(("127.0.0.1", settings.port if port is None else port), PoolHandler)
        try:
            self.snapshot = snapshot_tag(self.settings, self.token, self.keys, port=self.server_port)
        except BaseException:
            self.server_close()
            raise

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        """Never leak request contents, provider credentials or traceback locals."""


def _load_keys(settings: PoolConfig, *, token=None) -> dict[str, str]:
    token = read_secret(settings.token_file) if token is None else token
    keys, groups = {}, {}
    for account in settings.accounts:
        if not account.enabled:
            continue
        key = read_secret(Path(account.credential_file))
        if hmac.compare_digest(key, token):
            raise ValueError("gateway token must be different from provider credentials")
        digest = hashlib.sha256(key.encode("ascii")).hexdigest()
        if digest in groups and groups[digest] != account.quota_group:
            raise ValueError("identical credentials cannot belong to different quota groups")
        keys[account.id], groups[digest] = key, account.quota_group
    return keys


class PoolHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PbatchLLMPool/1"

    def setup(self):
        self.request.settimeout(15)
        self.committed = False
        super().setup()

    def log_message(self, fmt, *args):
        """Disable default access logs: request paths and headers are untrusted."""

    def _json(self, status: int, value: dict, *, retry_at=None, snapshot=False):
        raw = json.dumps(value, allow_nan=False).encode("utf-8")
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        if snapshot:
            self.send_header(SNAPSHOT_HEADER, self.server.snapshot)
            self.send_header("Cache-Control", "no-store")
        if retry_at is not None:
            self.send_header("Retry-After", str(max(1, math.ceil(retry_at - time.time()))))
        self.end_headers()
        self.committed = True
        self.wfile.write(raw)

    def _error(self, status: int, code: str, message: str, *, retry_at=None):
        self._json(status, {"type": "error", "error": {
            "type": code, "code": code, "message": message}},
                   retry_at=retry_at)

    def _authenticated(self) -> bool:
        authorization = self.headers.get_all("Authorization", [])
        api_keys = self.headers.get_all("x-api-key", [])
        if self.headers.get("Origin"):
            self._error(403, "pool_origin_denied", "browser origins are not supported")
            return False
        candidates = [value.removeprefix("Bearer ") for value in authorization
                      if value.startswith("Bearer ")] + api_keys
        valid = (len(authorization) <= 1 and len(api_keys) <= 1 and bool(candidates)
                 and len(candidates) == len(authorization) + len(api_keys)
                 and all(hmac.compare_digest(value.encode(), self.server.token.encode())
                         for value in candidates))
        if not valid:
            self._error(401, "pool_unauthorized", "valid gateway credential required")
        return valid

    def do_GET(self):
        if not self._authenticated():
            return
        if self.path != "/v1/models":
            self._error(404, "pool_not_found", "unknown gateway endpoint")
            return
        self._json(200, {"object": "list", "data": [
            {"id": model, "object": "model", "owned_by": "opencode-go"}
            for model in self.server.settings.models]}, snapshot=True)

    def _body(self) -> bytes:
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") or len(lengths) != 1
                or not lengths[0].isdigit() or len(lengths[0]) > 8):
            raise ValueError("one bounded Content-Length is required")
        size = int(lengths[0])
        if not 1 <= size <= MAX_REQUEST:
            raise ValueError("request must contain 1 byte to 2 MiB")
        if self.headers.get_content_type() != "application/json":
            raise ValueError("application/json required")
        body = self.rfile.read(size)
        if len(body) != size:
            raise ValueError("incomplete request body")
        return body

    def _headers(self) -> dict:
        headers = {}
        for name in _FORWARDED:
            values = self.headers.get_all(name, [])
            if len(values) > 1:
                raise ValueError("duplicate protocol header")
            if values:
                value = values[0]
                if not 1 <= len(value) <= 512 or any(not 32 <= ord(c) <= 126 for c in value):
                    raise ValueError("invalid protocol header")
                headers[name] = value
        if not headers.get("x-opencode-session", "").strip():
            raise ValueError("stable x-opencode-session header required for each conversation")
        return headers

    def do_POST(self):
        if not self._authenticated():
            return
        route = self.path.removeprefix("/v1/")
        if self.path != "/v1/" + route or route not in {"chat/completions", "messages", "responses"}:
            self._error(404, "pool_not_found", "unknown gateway endpoint")
            return
        try:
            with UpstreamRequest(self.server.upstream_url, self.server.settings.request_timeout,
                                 self.connection) as upstream:
                body, headers = self._body(), self._headers()
                model = _validate_request(body, route, self.server.settings.models)
                self._proxy(upstream, route, body, headers, model)
        except (ValueError, UnicodeError, RecursionError):
            self._safe_error(400, "pool_invalid_request", "invalid request, model or session header")
        except (OSError, http.client.HTTPException):
            self._safe_error(502, "pool_transport_failed", "upstream transport failed; request not replayed")
        except Exception:
            self._safe_error(503, "pool_state_unavailable", "pool state unavailable; request not replayed")

    def _safe_error(self, status: int, code: str, message: str):
        if self.committed:
            self.close_connection = True
            return
        try:
            self._error(status, code, message)
        except OSError:
            self.close_connection = True

    def _proxy(self, upstream, route: str, body: bytes, headers: dict, model: str):
        settings, tried = self.server.settings, set()
        while len(tried) < len(settings.accounts):
            if settings.usage_enabled:
                prepare_usage(settings, self.server.store, self.server.keys, model, tried, upstream)
            lease = self.server.store.acquire(settings.accounts, model,
                                               upstream.remaining() + 5, exclude=tried,
                                               require_usage=settings.usage_enabled)
            if lease is None:
                break
            tried.add(lease.quota_group)
            failure = PoolFailure("service", "incomplete_response")
            response = None
            try:
                response = upstream.open(route, body, self.server.keys[lease.account_id], headers)
                if 200 <= response.status < 300:
                    self._forward(response, lease.account_id, upstream)
                    failure = None
                    return
                failure = self._failure(response)
                if failure.kind != "quota":
                    self._upstream_error(response.status, failure)
                    return
            finally:
                if response is not None:
                    response.close()
                upstream.close_attempt()
                self.server.store.finish(lease, failure, probe_after=settings.probe_after)
        self._unavailable(model)

    def _unavailable(self, model):
        settings = self.server.settings
        status = self.server.store.status(settings.accounts, model,
                                          require_usage=settings.usage_enabled)
        if any(row["state"] == "usage_unknown" for row in status["accounts"]):
            self._error(503, "pool_usage_unavailable", "fresh quota status unavailable; admission refused",
                        retry_at=status.get("next_available_at"))
            return
        self._error(429, "pool_unavailable", "no eligible account; retry later",
                    retry_at=status.get("next_available_at"))

    def _failure(self, response):
        if response.getheader("Content-Encoding", "identity") != "identity":
            return PoolFailure("service", "unsupported_upstream_encoding")
        raw = response.read(65537)
        values = response.headers.get_all("Retry-After", [])
        headers = {"Retry-After": values[0]} if len(values) == 1 else {}
        return classify_failure(response.status, headers, raw)

    def _upstream_error(self, status: int, failure):
        code = "pool_upstream_" + failure.kind
        public_status = status if status in (400, 401, 403, 404, 413, 422, 429) else 502
        self._error(public_status, code, "upstream rejected request; no automatic replay",
                    retry_at=failure.retry_at)

    def _forward(self, response, account_id: str, upstream):
        content_type = response.getheader("Content-Type", "")
        if not (content_type.startswith("application/json") or content_type.startswith("text/event-stream")):
            raise OSError("unsupported upstream response type")
        if response.getheader("Content-Encoding", "identity") != "identity":
            raise OSError("unsupported upstream response encoding")
        content_type = ("text/event-stream" if content_type.startswith("text/event-stream")
                        else "application/json")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or len(length) > 9
                                   or int(length) > MAX_RESPONSE):
            raise OSError("invalid or oversized upstream response length")
        self.close_connection = True
        self.send_response(response.status)
        self.send_header("Content-Type", content_type)
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Pbatch-Account", account_id)
        if length is not None:
            self.send_header("Content-Length", length)
        self.end_headers()
        self.committed = True
        size = 0
        while True:
            upstream.remaining()
            chunk = response.read1(min(8192, MAX_RESPONSE + 1 - size))
            upstream.remaining()
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE:
                raise OSError("upstream response exceeds limit")
            self.wfile.write(chunk)
            self.wfile.flush()
        if length is not None and size != int(length):
            raise OSError("truncated upstream response")
