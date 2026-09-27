"""App Server startup has one finite budget across spawn, writes and replies."""
from __future__ import annotations

import json
import queue
import sys
import threading
import time

import pytest

from pbatch import tui_adapter_appserver, tui_appserver_support, tui_appserver_startup
from pbatch.tui_adapter_appserver import CodexAppServerAdapter


class DelayedTransport:
    def __init__(self, *, send_delay=0, reply_delay=0, block_method=""):
        self.send_delay, self.reply_delay = send_delay, reply_delay
        self.block_method = block_method
        self.closed = threading.Event()
        self.send_finished = threading.Event()
        self.incoming = queue.Queue()
        self.sent = []
        self.timers = []

    def send_line(self, line):
        command = json.loads(line)
        method = command["method"]
        delay = 10 if method == self.block_method else self.send_delay
        if self.closed.wait(delay):
            self.send_finished.set()
            raise EOFError("closed")
        self.sent.append(method)
        if "id" in command:
            response = json.dumps({"id": command["id"], "result": {}})
            timer = threading.Timer(self.reply_delay, self.incoming.put, args=(response,))
            self.timers.append(timer)
            timer.start()
        self.send_finished.set()

    def recv_line(self):
        return self.incoming.get()

    def close(self):
        self.closed.set()
        for timer in self.timers:
            timer.cancel()
            timer.join(timeout=1)
        self.incoming.put(None)


@pytest.mark.parametrize("kwargs, expected", [
    ({}, (5, 15)),
    ({"request_timeout": 0.1}, (0.1, 0.1)),
    ({"request_timeout": 0.1, "startup_timeout": 0.4}, (0.1, 0.4)),
    ({"startup_timeout": 0.4}, (5, 0.4)),
])
def test_startup_budget_defaults_and_explicit_request_compatibility(tmp_path, kwargs, expected):
    adapter = CodexAppServerAdapter(tmp_path, **kwargs)
    assert (adapter._timeout, adapter._startup_timeout) == expected
    adapter.close()


@pytest.mark.parametrize("name", ["request_timeout", "startup_timeout", "approval_ttl"])
@pytest.mark.parametrize("value", [
    True, False, 0, -1, float("nan"), float("inf"), -float("inf"), "1", [],
    threading.TIMEOUT_MAX * 2, 10 ** 1000,
])
def test_invalid_timeouts_fail_before_transport_creation(tmp_path, name, value, monkeypatch):
    def unexpected_transport(*_args, **_kwargs):
        pytest.fail("invalid timeout created a provider transport")
    monkeypatch.setattr(tui_adapter_appserver, "DefaultTransport", unexpected_transport)
    with pytest.raises(ValueError, match=name):
        CodexAppServerAdapter(tmp_path, **{name: value})


def test_longer_default_startup_does_not_extend_regular_rpc(tmp_path, monkeypatch):
    # Keep the production 3:1 budget ratio while avoiding multi-second sleeps.
    monkeypatch.setattr(tui_appserver_startup, "DEFAULT_TIMEOUT", 0.05)
    monkeypatch.setattr(tui_appserver_startup, "DEFAULT_STARTUP_TIMEOUT", 0.15)
    transport = DelayedTransport(reply_delay=0.08)
    adapter = CodexAppServerAdapter(tmp_path, transport=transport)
    try:
        adapter.initialize()
        assert adapter.runtime_info().capabilities.supports("message_stream")
        with pytest.raises(RuntimeError, match="thread/list timed out after 0.05s"):
            adapter.list_sessions({})
        assert transport.sent == ["initialize", "initialized", "thread/list"]
        assert not adapter._responses
    finally:
        adapter.close()


@pytest.mark.parametrize("transport_options", [
    {"reply_delay": 1}, {"block_method": "initialize"}, {"block_method": "initialized"},
    {"send_delay": 0.08, "reply_delay": 0.08},
])
def test_timeout_covers_send_and_reply_with_one_deadline(tmp_path, transport_options):
    transport = DelayedTransport(**transport_options)
    adapter = CodexAppServerAdapter(tmp_path, transport=transport, startup_timeout=0.12)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="initialize timed out after 0.12s"):
        adapter.initialize()
    assert time.monotonic() - started < 0.8
    assert transport.closed.is_set()
    assert "initialized" not in transport.sent
    assert not adapter._initialized and not adapter._responses
    assert adapter._reader is not None and not adapter._reader.is_alive()
    assert not adapter.runtime_info().capabilities.supports("message_stream")


def test_explicit_request_timeout_still_limits_initialize(tmp_path):
    transport = DelayedTransport(reply_delay=0.15)
    adapter = CodexAppServerAdapter(tmp_path, transport=transport, request_timeout=0.03)
    with pytest.raises(RuntimeError, match="initialize timed out after 0.03s"):
        adapter.initialize()
    assert transport.sent == ["initialize"]
    assert transport.closed.is_set() and not adapter._responses


def test_late_created_transport_is_closed_without_starting_protocol(tmp_path, monkeypatch):
    transport = DelayedTransport()
    returned = threading.Event()
    def create(*_args, **_kwargs):
        time.sleep(0.12)
        returned.set()
        return transport
    monkeypatch.setattr(tui_adapter_appserver, "DefaultTransport", create)
    adapter = CodexAppServerAdapter(tmp_path, startup_timeout=0.03)
    with pytest.raises(RuntimeError, match="initialize timed out"):
        adapter.initialize()
    assert returned.is_set() and transport.closed.is_set()
    assert not transport.sent and adapter._reader is None
    assert not adapter._initialized and not adapter._responses


@pytest.mark.parametrize("phase", ["blocked_write", "delayed_spawn"])
def test_timeout_reaps_owned_process_during_initialization(tmp_path, monkeypatch, phase):
    argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    monkeypatch.setattr(tui_appserver_support, "app_server_argv", lambda _sock: argv)
    if phase == "blocked_write":
        # Fill a real pipe while the owned process never reads.
        monkeypatch.setattr(tui_adapter_appserver, "VERSION", "x" * (512 * 1024))
    else:
        original = tui_adapter_appserver.DefaultTransport
        def delayed_transport(*args, **kwargs):
            time.sleep(0.12)
            return original(*args, **kwargs)
        monkeypatch.setattr(tui_adapter_appserver, "DefaultTransport", delayed_transport)
    adapter = CodexAppServerAdapter(tmp_path, startup_timeout=0.08)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="initialize timed out"):
            adapter.initialize()
        assert time.monotonic() - started < 2
        transport = adapter._transport
        assert transport is not None and transport._process.poll() is not None
        assert not transport._stderr_thread.is_alive()
        if adapter._reader is not None:
            assert not adapter._reader.is_alive()
        assert not adapter._responses and not adapter._initialized
        assert not any(t.name == "codex-app-server-initialize" for t in threading.enumerate())
    finally:
        adapter.close()
