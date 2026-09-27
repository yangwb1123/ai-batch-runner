"""Real pipe framing and diagnostics must stay bounded without a newline."""

from __future__ import annotations

import queue
import io
import sys
import threading
import time

import pytest

from pbatch import tui_appserver_support as support
from pbatch.tui_adapter_appserver import CodexAppServerAdapter
from pbatch.tui_transport_io import BoundedLineReader


def _transport(tmp_path, monkeypatch, code):
    argv = [sys.executable, "-c", code]
    monkeypatch.setattr(support, "app_server_argv", lambda _sock: argv)
    return support.DefaultTransport(cwd=str(tmp_path))


def test_unterminated_oversize_stdout_fails_while_provider_is_alive(tmp_path, monkeypatch):
    transport = _transport(tmp_path, monkeypatch,
        "import sys,time; sys.stdout.buffer.write(b'x'*(8*1024*1024+1)); "
        "sys.stdout.buffer.flush(); time.sleep(30)")
    result = queue.Queue()
    def read():
        try:
            result.put(transport.recv_line())
        except Exception as exc:
            result.put(exc)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        try:
            value = result.get(timeout=2)
        except queue.Empty:
            pytest.fail("protocol reader waited for a newline beyond its byte budget")
        assert isinstance(value, ValueError) and "exceeds" in str(value)
        assert transport._process.poll() is None
    finally:
        transport.close()
        reader.join(timeout=1)
    assert not reader.is_alive()


def test_stderr_tail_is_available_before_newline_or_process_exit(tmp_path, monkeypatch):
    transport = _transport(tmp_path, monkeypatch,
        "import sys,time; sys.stderr.buffer.write(b'OLD_PREFIX'+b'x'*200000+b'LATEST_TAIL'); "
        "sys.stderr.buffer.flush(); print('{}',flush=True); time.sleep(30)")
    try:
        assert transport.recv_line() == "{}\n"
        deadline = time.monotonic() + 1
        while "LATEST_TAIL" not in transport.unavailable_reason() and time.monotonic() < deadline:
            time.sleep(0.01)
        reason = transport.unavailable_reason()
        assert "LATEST_TAIL" in reason
        assert "OLD_PREFIX" not in reason
        assert len(reason.encode()) < 9 * 1024
        assert len(transport._stderr) <= support.MAX_STDERR_BYTES
        assert transport._process.poll() is None
    finally:
        transport.close()
    assert not transport._stderr_thread.is_alive()


@pytest.mark.parametrize("raw,limit,expected", [
    (b"{}\n", 3, ["{}\n"]),
    (b"{}\r\n{}\n", 4, ["{}\r\n", "{}\n"]),
    ('{"text":"中文"}\n'.encode(), 18, ['{"text":"中文"}\n']),
    (b"\n" * 30, 1, ["\n"] * 30),
])
def test_complete_frames_preserve_utf8_and_consecutive_records(raw, limit, expected):
    reader = BoundedLineReader(io.BytesIO(raw), limit, "App Server")
    assert [reader.read_line() for _ in expected] == expected
    assert reader.read_line() is None


@pytest.mark.parametrize("raw,limit,reason", [
    (b"{}\n", 2, "exceeds"),
    (b"{}", 8, "unterminated"),
    (b"\xff\n", 8, "UTF-8"),
    ('中\n'.encode(), 3, "exceeds"),
])
def test_invalid_frames_are_rejected_without_publishing_a_partial_record(raw, limit, reason):
    reader = BoundedLineReader(io.BytesIO(raw), limit, "App Server")
    with pytest.raises(ValueError, match=reason):
        reader.read_line()


def test_reader_bounds_each_read_and_does_not_read_entire_stream():
    class InfiniteStream:
        received = 0
        def read1(self, size):
            assert 0 < size <= 8192
            self.received += size
            return b"x" * size
    stream = InfiniteStream()
    reader = BoundedLineReader(stream, 16385, "App Server")
    with pytest.raises(ValueError, match="exceeds"):
        reader.read_line()
    assert stream.received == 16385


def test_multibyte_character_can_span_pipe_chunks():
    class ByteStream:
        def __init__(self):
            self.values = iter('"中文"\n'.encode())
        def read1(self, size):
            return bytes([next(self.values)])
    reader = BoundedLineReader(ByteStream(), 32, "App Server")
    assert reader.read_line() == '"中文"\n'


def test_startup_protocol_overflow_reaps_child_and_releases_pending_request(tmp_path, monkeypatch):
    monkeypatch.setattr(support, "MAX_RPC_LINE_BYTES", 1024)
    transport = _transport(tmp_path, monkeypatch,
        "import sys,time; sys.stdin.buffer.readline(); "
        "sys.stdout.buffer.write(b'x'*1024); sys.stdout.buffer.flush(); time.sleep(30)")
    adapter = CodexAppServerAdapter(tmp_path, transport=transport, startup_timeout=2)
    try:
        with pytest.raises(RuntimeError, match="App Server record exceeds 1024 bytes"):
            adapter.initialize()
        assert not adapter._initialized and not adapter._responses
        assert transport._process.poll() is not None
        assert not transport._stderr_thread.is_alive()
        assert not adapter._reader.is_alive()
    finally:
        adapter.close()
