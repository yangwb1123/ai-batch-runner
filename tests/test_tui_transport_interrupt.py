"""Closing owned POSIX pipes interrupts I/O even while escaped peers live."""

from __future__ import annotations

import array
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import sys
import threading
import time

import pytest

from pbatch.tui_codex_support import _stop_process
from test_tui_transport_resources import (
    _escaped_provider_code, _transport, _wait_until, adopt_escaped_child,
)


pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Linux pipe capacity and owned-grandchild reaping")


def _worker(operation, name, results, entered):
    def run():
        entered.set()
        try:
            results[name] = operation()
        except (EOFError, OSError, ValueError) as exc:
            results[name] = exc
    thread = threading.Thread(target=run, name=f"interrupt-test-{name}", daemon=True)
    thread.start()
    return thread


def _full_pipe(descriptor):
    import fcntl
    import termios
    available = array.array("i", [0])
    fcntl.ioctl(descriptor, termios.FIONREAD, available, True)
    return available[0] == fcntl.fcntl(descriptor, fcntl.F_GETPIPE_SZ)


def _alive_child(pid):
    return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"


def _join(threads, timeout):
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    return [thread.name for thread in threads if thread.is_alive()]


def _begin_io(transport, case):
    operations = {
        "recv_line": transport.recv_line,
        "send_line": lambda: transport.send_line("x" * (1024 * 1024) + "\n"),
    }
    for name, operation in operations.items():
        entered = threading.Event()
        case["threads"].append(_worker(operation, name, case["results"], entered))
        assert entered.wait(2), f"{name} never entered"
    case["threads"].append(transport._stderr_thread)
    _wait_until(lambda: _full_pipe(transport._process.stdin.fileno()), "real stdin backpressure")
    assert all(thread.is_alive() for thread in case["threads"])


def _close(case):
    entered = threading.Event()
    closer = _worker(case["transport"].close, "close", case["results"], entered)
    case["closers"].append(closer)
    assert entered.wait(2)
    assert not _join([closer], 2), "transport.close exceeded its teardown budget"
    assert case["results"].get("close") is None
    assert case["transport"]._process.poll() is not None
    assert _alive_child(case["child"]), "the escaped child was killed to unblock I/O"


def _cleanup(case, ready, children):
    _stop_process(case["transport"]._process)
    if ready.exists() and ready.stat().st_size:
        child = int(ready.read_text())
        if child not in children:
            children.append(child)
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if not case["closers"]:
        entered = threading.Event()
        case["closers"].append(_worker(
            case["transport"].close, "close", case["results"], entered))
    pending = _join(case["threads"] + case["closers"], 3)
    assert not pending, f"test cleanup left owned threads: {pending}"


@contextmanager
def _held_pipes(kind, tmp_path, monkeypatch, children, cycle):
    ready = tmp_path / f"escaped-{cycle}.pid"
    transport = _transport(kind, tmp_path, monkeypatch, _escaped_provider_code(ready))
    case = {"transport": transport, "threads": [], "closers": [], "results": {}}
    try:
        _wait_until(lambda: ready.exists() and ready.stat().st_size, "escaped provider child")
        case["child"] = int(ready.read_text())
        children.append(case["child"])
        assert os.getpgid(case["child"]) == case["child"] != transport._process.pid
        _begin_io(transport, case)
        yield case
    finally:
        _cleanup(case, ready, children)


@pytest.mark.parametrize("kind", ["pi-rpc", "app-server"])
def test_close_interrupts_active_io_before_escaped_child_exits(
        tmp_path, monkeypatch, kind, adopt_escaped_child):
    with _held_pipes(kind, tmp_path, monkeypatch, adopt_escaped_child, 0) as case:
        _close(case)
        pending = _join(case["threads"], 1)
        assert not pending, f"close left blocked I/O with a live escaped child: {pending}"
        assert _alive_child(case["child"])
        assert isinstance(case["results"]["send_line"], (EOFError, OSError, ValueError))
        assert case["results"]["recv_line"] is None or isinstance(
            case["results"]["recv_line"], (EOFError, OSError, ValueError))


@pytest.mark.parametrize("kind", ["pi-rpc", "app-server"])
def test_reconnecting_transports_do_not_accumulate_blocked_io_threads(
        tmp_path, monkeypatch, kind, adopt_escaped_child):
    from contextlib import ExitStack
    with ExitStack() as sessions:
        cases = []
        for cycle in range(3):
            case = sessions.enter_context(
                _held_pipes(kind, tmp_path, monkeypatch, adopt_escaped_child, cycle))
            cases.append(case)
            _close(case)
        threads = [thread for case in cases for thread in case["threads"]]
        pending = _join(threads, 1)
        assert not pending, f"three retained transports accumulated I/O threads: {pending}"
        assert all(_alive_child(case["child"]) for case in cases)
        assert all(isinstance(case["results"]["send_line"], (EOFError, OSError, ValueError))
                   for case in cases)
