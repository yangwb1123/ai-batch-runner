"""Retained transports release real pipe handles without blocking teardown."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from pbatch import tui_appserver_support as appserver
from pbatch import tui_pi_rpc_support as pi_rpc
from pbatch.tui_codex_support import _stop_process


def _transport(kind, tmp_path, monkeypatch, code="import time; time.sleep(30)"):
    argv = [sys.executable, "-c", code]
    if kind == "app-server":
        monkeypatch.setattr(appserver, "app_server_argv", lambda _sock: argv)
        return appserver.DefaultTransport(cwd=str(tmp_path))

    def spawn(_argv, cwd):
        return subprocess.Popen(
            argv, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True)
    return pi_rpc.DefaultPiRpcTransport(str(tmp_path), spawn=spawn)


def _pipes(transport):
    return [getattr(transport._process, name) for name in ("stdin", "stdout", "stderr")]


@pytest.mark.parametrize("kind", ["pi-rpc", "app-server"])
@pytest.mark.parametrize("exit_code", [None, 7])
def test_close_releases_all_pipes_while_transport_is_retained(
        tmp_path, monkeypatch, kind, exit_code):
    code = "import time; time.sleep(30)" if exit_code is None else "raise SystemExit(7)"
    transport = _transport(kind, tmp_path, monkeypatch, code)
    pipes = _pipes(transport)
    descriptors = [pipe.fileno() for pipe in pipes]
    try:
        if exit_code is not None:
            assert transport._process.wait(timeout=3) == exit_code
        transport.close()
        transport.close()
        assert all(pipe.closed for pipe in pipes)
        assert transport._process.poll() is not None
        assert not transport._stderr_thread.is_alive()
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
    finally:
        transport.close()
        for pipe in pipes:
            pipe.close()


@pytest.mark.parametrize("kind", ["pi-rpc", "app-server"])
def test_concurrent_close_waits_for_the_same_cleanup(tmp_path, monkeypatch, kind):
    transport = _transport(kind, tmp_path, monkeypatch)
    module = appserver if kind == "app-server" else pi_rpc
    stopped, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def stop(process):
        calls.append(process)
        stopped.set()
        assert release.wait(3)
        _stop_process(process)
    monkeypatch.setattr(module, "_stop_process", stop)
    first = threading.Thread(target=transport.close, daemon=True)
    second = threading.Thread(target=lambda: (transport.close(), finished.set()), daemon=True)
    try:
        first.start()
        assert stopped.wait(1)
        second.start()
        assert not finished.wait(0.05), "concurrent close returned before pipe cleanup"
        release.set()
        first.join(2)
        second.join(2)
        assert finished.is_set() and not first.is_alive()
        assert len(calls) == 1 and all(pipe.closed for pipe in _pipes(transport))
    finally:
        release.set()
        first.join(3)
        second.join(3)
        transport.close()


@pytest.mark.parametrize("kind", ["pi-rpc", "app-server"])
def test_process_stop_exception_still_releases_all_pipes(tmp_path, monkeypatch, kind):
    transport = _transport(kind, tmp_path, monkeypatch)
    module = appserver if kind == "app-server" else pi_rpc

    def stop(process):
        _stop_process(process)
        raise RuntimeError("injected stop failure")
    monkeypatch.setattr(module, "_stop_process", stop)
    try:
        with pytest.raises(RuntimeError, match="injected stop failure"):
            transport.close()
        assert all(pipe.closed for pipe in _pipes(transport))
        transport.close()
        assert not transport._stderr_thread.is_alive()
    finally:
        _stop_process(transport._process)


@pytest.mark.parametrize("kind", ["pi-rpc", "app-server"])
def test_close_preserves_final_stderr_diagnostics(tmp_path, monkeypatch, kind):
    module = appserver if kind == "app-server" else pi_rpc
    transport_class = module.DefaultTransport if kind == "app-server" else module.DefaultPiRpcTransport
    read_stderr = transport_class._read_stderr
    release, stopped = threading.Event(), threading.Event()

    def delayed_stderr(self):
        assert release.wait(3)
        read_stderr(self)

    def stop(process):
        _stop_process(process)
        stopped.set()
    monkeypatch.setattr(transport_class, "_read_stderr", delayed_stderr)
    monkeypatch.setattr(module, "_stop_process", stop)
    transport = _transport(kind, tmp_path, monkeypatch,
        "import os; os.write(2,b'final provider error'); raise SystemExit(7)")
    closer = threading.Thread(target=transport.close, daemon=True)
    try:
        assert transport._process.wait(timeout=3) == 7
        closer.start()
        assert stopped.wait(1)
        release.set()
        closer.join(2)
        assert not closer.is_alive()
        assert "final provider error" in transport.unavailable_reason()
        assert all(pipe.closed for pipe in _pipes(transport))
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(2)
        transport.close()


@pytest.mark.parametrize("kind", ["pi-rpc", "app-server"])
def test_later_close_does_not_close_reused_descriptor(tmp_path, monkeypatch, kind):
    transport = _transport(kind, tmp_path, monkeypatch)
    descriptor = transport._process.stdout.fileno()
    transport.close()
    replacement = os.open(tmp_path / "replacement", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if replacement != descriptor:
            os.dup2(replacement, descriptor)
        transport.close()
        for pipe in _pipes(transport):
            pipe.close()
        os.write(descriptor, b"still owned")
        assert (tmp_path / "replacement").read_bytes() == b"still owned"
    finally:
        os.close(replacement)
        if replacement != descriptor:
            os.close(descriptor)


def test_missing_pi_pipe_releases_the_remaining_real_pipes(tmp_path):
    processes = []

    def spawn(_argv, cwd):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], cwd=cwd,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        processes.append(process)
        return process
    try:
        with pytest.raises(RuntimeError, match="pipes are unavailable"):
            pi_rpc.DefaultPiRpcTransport(str(tmp_path), spawn=spawn)
        assert processes[0].poll() is not None
        assert processes[0].stdout.closed and processes[0].stderr.closed
    finally:
        for process in processes:
            _stop_process(process)
            process.stdout.close()
            process.stderr.close()


def _wait_until(predicate, description, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail(f"timed out waiting for {description}")


def _io_threads(transport):
    results = []

    def call(operation):
        try:
            results.append(operation())
        except (OSError, ValueError) as exc:
            results.append(exc)
    reader = threading.Thread(target=call, args=(transport.recv_line,), daemon=True)
    writer = threading.Thread(target=call, args=(
        lambda: transport.send_line("x" * (1024 * 1024) + "\n"),), daemon=True)
    reader.start()
    writer.start()
    return reader, writer, results


@pytest.fixture
def adopt_escaped_child():
    """Make this Linux test own and reap its intentionally escaped grandchild."""
    import ctypes
    library = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert library.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert library.prctl(36, 1, 0, 0, 0) == 0
    children = []
    try:
        yield children
    finally:
        try:
            for child in children:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _wait_until(lambda: os.waitpid(child, os.WNOHANG)[0] == child, "escaped child reap")
        finally:
            assert library.prctl(36, previous.value, 0, 0, 0) == 0


def _escaped_provider_code(ready):
    return ("import os,pathlib,time\n"
            "child=os.fork()\n"
            "if child == 0:\n"
            " os.setsid()\n"
            f" pathlib.Path({str(ready)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(30)\n")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pipe/subreaper evidence")
@pytest.mark.parametrize("kind", ["pi-rpc", "app-server"])
def test_escaped_child_cannot_block_close_or_retain_our_pipe_descriptors(
        tmp_path, monkeypatch, kind, adopt_escaped_child):
    ready = tmp_path / "escaped.pid"
    transport = _transport(kind, tmp_path, monkeypatch, _escaped_provider_code(ready))
    pipes = _pipes(transport)
    descriptors = [pipe.fileno() for pipe in pipes]
    reader, writer, results = _io_threads(transport)
    closer = threading.Thread(target=transport.close, daemon=True)
    try:
        _wait_until(lambda: ready.exists() and ready.stat().st_size, "escaped provider")
        escaped = int(ready.read_text())
        adopt_escaped_child.append(escaped)
        assert os.getpgid(escaped) == escaped != transport._process.pid
        time.sleep(0.05)
        assert all(thread.is_alive() for thread in (writer, reader, transport._stderr_thread))
        closer.start()
        closer.join(2)
        assert not closer.is_alive(), "close blocked on an escaped child's inherited pipe"
        assert all(pipe.closed for pipe in pipes)
        for descriptor in descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        assert transport._process.poll() is not None
        os.kill(escaped, 0)
        reader.join(1)
        writer.join(1)
        assert not reader.is_alive() and not writer.is_alive()
        assert not transport._stderr_thread.is_alive()
    finally:
        _stop_process(transport._process)
        if ready.exists() and ready.stat().st_size:
            child = int(ready.read_text())
            if child not in adopt_escaped_child:
                adopt_escaped_child.append(child)
            os.kill(child, signal.SIGKILL)
        if closer.ident is not None:
            closer.join(2)
        reader.join(2)
        writer.join(2)
        transport._stderr_thread.join(2)
        transport.close()
    assert not reader.is_alive() and not writer.is_alive()
    assert not transport._stderr_thread.is_alive() and len(results) == 2
