"""Real POSIX pipe transfer, initialization rollback and descriptor ownership."""

import errno
import io
import os
import queue
import subprocess
import sys
import threading
from contextlib import contextmanager

import pytest

from pbatch import tui_appserver_support as appserver
from pbatch import tui_transport_io as pipe_io
from pbatch.tui_pi_rpc_support import DefaultPiRpcTransport


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX nonblocking pipes")
ECHO = """
import os
while True:
    chunk = os.read(0, 4096)
    if not chunk:
        break
    while chunk:
        count = os.write(1, chunk[:29])
        chunk = chunk[count:]
"""


def _worker(operation):
    results = queue.Queue()

    def run():
        try:
            results.put((True, operation()))
        except BaseException as exc:
            results.put((False, exc))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, results


def _result(worker):
    thread, results = worker
    ok, value = results.get(timeout=5)
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert ok, repr(value)
    return value


def _reap(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=3)
    pipe_io.close_process_pipes(process)


def _drain(stream):
    chunks = []
    while True:
        chunk = stream.read1(8192)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _short_syscalls(monkeypatch, process):
    """Force actual OS transfers to split protocol bytes, without fake streams."""
    write_fd, read_fd = process.stdin.fileno(), process.stdout.fileno()
    original_write, original_read = os.write, os.read
    writes = []

    def write(fd, data):
        result = original_write(fd, data[:997] if fd == write_fd else data)
        if fd == write_fd:
            writes.append((len(data), result))
        return result

    def read(fd, size):
        return original_read(fd, min(size, 97) if fd == read_fd else size)

    monkeypatch.setattr(pipe_io.os, "write", write)
    monkeypatch.setattr(pipe_io.os, "read", read)
    return writes


def test_large_binary_payload_survives_partial_writes_and_bidirectional_backpressure(
        tmp_path, monkeypatch):
    process = subprocess.Popen(
        [sys.executable, "-c", ECHO], cwd=tmp_path, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    pipes = pipe_io.ProcessPipes(process)
    writes = _short_syscalls(monkeypatch, process)
    payload = bytes(range(256)) * 1024 + b"\x00tail\xff"

    def send():
        count = pipes.stdin.write(payload)
        pipes.stdin.flush()
        pipes.stdin.close()
        return count

    reader, writer = _worker(lambda: _drain(pipes.stdout)), _worker(send)
    try:
        assert len(payload) > 65536
        assert _result(reader) == payload
        assert _result(writer) == len(payload)
        assert any(requested > written > 0 for requested, written in writes)
        assert sum(written for _, written in writes) == len(payload)
        assert process.wait(timeout=3) == 0
    finally:
        _reap(process)
        pipes.close()
        for thread, _ in (reader, writer):
            thread.join(timeout=1)


def _transport(kind, tmp_path, monkeypatch, code=ECHO):
    argv = [sys.executable, "-c", code]
    if kind == "app-server":
        monkeypatch.setattr(appserver, "app_server_argv", lambda _socket: argv)
        return appserver.DefaultTransport(cwd=str(tmp_path))

    def spawn(_argv, cwd):
        return subprocess.Popen(argv, cwd=cwd, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)

    return DefaultPiRpcTransport(str(tmp_path), spawn=spawn)


@pytest.mark.parametrize("kind", ["app-server", "pi-rpc"])
def test_transport_preserves_large_utf8_lf_frames_across_partial_syscalls(
        kind, tmp_path, monkeypatch):
    transport = _transport(kind, tmp_path, monkeypatch)
    writes = _short_syscalls(monkeypatch, transport._process)
    lines = ['{"text":"' + "中文🙂é" * 12000 + '"}\n', '{"text":"结束✅"}\n']

    def send():
        for line in lines:
            transport.send_line(line)
        transport._pipes.stdin.close()

    reader = _worker(lambda: [transport.recv_line() for _ in range(3)])
    writer = _worker(send)
    try:
        assert len(lines[0].encode("utf-8")) > 65536
        assert _result(reader) == [*lines, None]
        assert _result(writer) is None
        assert any(requested > written > 0 for requested, written in writes)
        assert sum(written for _, written in writes) == len("".join(lines).encode("utf-8"))
    finally:
        transport.close()
        for thread, _ in (reader, writer):
            thread.join(timeout=1)
    assert not transport._stderr_thread.is_alive()
    assert not reader[0].is_alive() and not writer[0].is_alive()


@pytest.mark.parametrize("kind", ["app-server", "pi-rpc"])
@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_nonblocking_initialization_failure_reaps_spawned_child_and_closes_all_pipes(
        kind, fail_at, tmp_path, monkeypatch):
    original_spawn, original_blocking = subprocess.Popen, os.set_blocking
    spawned, configured = [], []

    def spawn(*args, **kwargs):
        process = original_spawn(*args, **kwargs)
        spawned.append(process)
        return process

    def set_blocking(fd, blocking):
        configured.append(fd)
        if len(configured) == fail_at:
            raise OSError(errno.EIO, "injected nonblocking initialization failure")
        original_blocking(fd, blocking)

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(pipe_io.os, "set_blocking", set_blocking)
    try:
        with pytest.raises(OSError, match="injected nonblocking initialization failure"):
            _transport(kind, tmp_path, monkeypatch, "import os; os.read(0, 1)")
        assert len(spawned) == 1 and len(configured) == fail_at
        process = spawned[0]
        assert process.poll() is not None, "pipe initialization leaked the spawned child"
        for stream in (process.stdin, process.stdout, process.stderr):
            assert stream.closed, "pipe initialization leaked an owned descriptor"
        for fd in configured:
            with pytest.raises(OSError) as error:
                os.fstat(fd)
            assert error.value.errno == errno.EBADF
    finally:
        for process in spawned:
            _reap(process)


@contextmanager
def _pipe_pair():
    descriptors = os.pipe()
    try:
        yield descriptors
    finally:
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError as exc:
                if exc.errno != errno.EBADF:
                    raise


class _GatedPoll:
    def __init__(self, factory, entered, resume):
        self._poller = factory()
        self._entered, self._resume = entered, resume

    def register(self, *arguments):
        self._poller.register(*arguments)

    def poll(self, timeout):
        self._entered.set()
        if not self._resume.wait(3):
            raise TimeoutError("test did not release the paused readiness poll")
        return self._poller.poll(timeout)


def _fill_pipe(fd):
    while True:
        try:
            os.write(fd, b"occupied" * 1024)
        except BlockingIOError:
            return


@pytest.mark.parametrize("direction", ["read", "write"])
def test_close_and_descriptor_reuse_never_accesses_replacement_pipe(direction, monkeypatch):
    entered, resume = threading.Event(), threading.Event()
    original_poll = pipe_io.select.poll
    monkeypatch.setattr(pipe_io.select, "poll",
                        lambda: _GatedPoll(original_poll, entered, resume))
    with _pipe_pair() as original, _pipe_pair() as replacement:
        reading = direction == "read"
        target = original[0 if reading else 1]
        raw = io.FileIO(target, "rb" if reading else "wb", closefd=True)
        stream = pipe_io.InterruptiblePipe(raw)
        os.set_blocking(replacement[0], False)
        if reading:
            os.write(replacement[1], b"replacement-sentinel")
        else:
            _fill_pipe(target)
        worker = _worker(lambda: stream.read1(100) if reading else stream.write(b"pending"))
        try:
            assert entered.wait(2), "operation never reached nonblocking readiness wait"
            stream.close()
            os.dup2(replacement[0 if reading else 1], target)
            resume.set()
            ok, value = worker[1].get(timeout=2)
            worker[0].join(timeout=1)
            assert not worker[0].is_alive()
            if reading:
                assert ok and value == b""
                assert os.read(replacement[0], 100) == b"replacement-sentinel"
            else:
                assert not ok and isinstance(value, BrokenPipeError)
                with pytest.raises(BlockingIOError):
                    os.read(replacement[0], 1)
            stream.close()
            assert os.fstat(target).st_ino == os.fstat(replacement[0]).st_ino
            if not reading:
                assert os.write(target, b"still-owned") == 11
                assert os.read(replacement[0], 11) == b"still-owned"
        finally:
            resume.set()
            stream.close()
            worker[0].join(timeout=1)


def test_readiness_poll_supports_descriptors_above_select_limit(monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    resource = pytest.importorskip("resource")
    minimum = 1088
    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft_limit != resource.RLIM_INFINITY and soft_limit <= minimum:
        pytest.skip("open-file limit does not permit a descriptor above FD_SETSIZE")
    waiting = threading.Event()
    original_read = os.read
    with _pipe_pair() as (read_fd, write_fd):
        try:
            high_fd = fcntl.fcntl(read_fd, fcntl.F_DUPFD, minimum)
        except OSError as exc:
            if exc.errno in (errno.EMFILE, errno.ENFILE):
                pytest.skip("system open-file limit prevents allocating a high descriptor")
            raise
        stream = pipe_io.InterruptiblePipe(io.FileIO(high_fd, "rb", closefd=True))

        def read(fd, size):
            try:
                return original_read(fd, size)
            except BlockingIOError:
                if fd == high_fd:
                    waiting.set()
                raise

        monkeypatch.setattr(pipe_io.os, "read", read)
        worker = _worker(lambda: stream.read1(8))
        try:
            assert high_fd > 1024 and waiting.wait(2)
            os.write(write_fd, b"high-fd")
            assert _result(worker) == b"high-fd"
        finally:
            stream.close()
            worker[0].join(timeout=1)
