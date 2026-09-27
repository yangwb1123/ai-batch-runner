"""Bounded HTTP transport for a fixed Go origin, with no redirect or proxy lookup."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import threading
import time
from urllib.parse import urlsplit

from .llm_pool_config import GO_ORIGIN


def _origin(value: str):
    parsed = urlsplit(value)
    if value != GO_ORIGIN:
        try:
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            local = False
        if not local or parsed.scheme != "http":
            raise ValueError("upstream must be OpenCode Go or an explicit loopback test server")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid upstream origin")
    return parsed


class UpstreamRequest:
    """Own one deadline across all attempts; do not retry ambiguous transport errors."""

    def __init__(self, origin: str, timeout: float, client_socket=None):
        self.origin = _origin(origin)
        self.deadline = time.monotonic() + timeout
        self.connection = None
        self.active_socket = None
        self.client_socket = client_socket
        self.timer = threading.Timer(timeout, self.abort)
        self.timer.daemon = True

    def __enter__(self):
        self.timer.start()
        return self

    def remaining(self) -> float:
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("LLM pool request deadline exceeded")
        return seconds

    def open(self, route: str, body: bytes, key: str, headers: dict):
        return self._open("POST", route, body, key, headers)

    def usage(self, key: str):
        """GET the fixed sibling usage endpoint; never forward client headers."""
        return self._open("GET", "usage", None, key, {})

    def models(self, key: str):
        """GET the sibling models endpoint for explicit local gateway diagnostics."""
        return self._open("GET", "models", None, key, {})

    def _open(self, method, route, body, key, headers):
        self.close_attempt()
        connection_type = (http.client.HTTPSConnection if self.origin.scheme == "https"
                           else http.client.HTTPConnection)
        self.connection = connection_type(self.origin.hostname, self.origin.port,
                                           timeout=self.remaining())
        outgoing = {
            "Content-Type": "application/json", "Accept-Encoding": "identity",
            "User-Agent": "ai-batch-runner/0.3.0 llm-pool/1",
            "Authorization": "Bearer " + key, **headers,
        }
        if route == "messages":
            outgoing["x-api-key"] = key
            outgoing.setdefault("anthropic-version", "2023-06-01")
        path = self.origin.path.rstrip("/") + "/" + route
        self.connection.request(method, path, body=body, headers=outgoing)
        self.active_socket = self.connection.sock
        self.connection.sock.settimeout(self.remaining())
        return self.connection.getresponse()

    def close_attempt(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.active_socket = None

    def abort(self):
        upstream = self.active_socket or getattr(self.connection, "sock", None)
        for active in (upstream, self.client_socket):
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.close_attempt()

    def __exit__(self, *exc):
        self.timer.cancel()
        self.close_attempt()
