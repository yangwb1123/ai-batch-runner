"""Bounded byte framing for a provider's local protocol pipe."""

from __future__ import annotations

import io
import os
import select
import threading
from typing import Any, Optional


def close_process_pipes(process: Any) -> None:
    """Release owned Popen descriptors without buffered I/O locks or flushing.

    A descendant outside the child's process group may still hold a pipe open.
    BufferedReader/Writer.close() can then wait forever for an in-flight read
    or write. Closing the raw FileIO releases our descriptor and marks the
    wrappers closed without waiting for that operation. Use the owning object,
    never os.close(fileno()), so later cleanup cannot close a reused descriptor.
    Unadapted blocking streams may leave a read waiting for the peer; real
    POSIX transports use InterruptiblePipe to keep their workers cancellable.
    """
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(process, name, None)
        stream = getattr(stream, "buffer", stream)
        stream = getattr(stream, "raw", stream)
        close = getattr(stream, "close", None)
        if callable(close):
            try:
                close()
            except (OSError, ValueError):
                pass


def read_chunk(stream: Any, size: int = 8192) -> bytes:
    """Read available pipe bytes without waiting to fill a buffered read."""
    reader = getattr(stream, "read1", None)
    value = reader(size) if callable(reader) else stream.read(size)
    return value.encode("utf-8") if isinstance(value, str) else bytes(value or b"")


class InterruptiblePipe:
    """Own a POSIX nonblocking pipe; serialize syscalls with descriptor closure."""

    def __init__(self, raw: io.FileIO):
        self._raw = raw
        self._fd = raw.fileno()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        os.set_blocking(self._fd, False)

    def _wait(self, event: int) -> None:
        # poll has no FD_SETSIZE ceiling and allocates no extra descriptor.
        # The FD may close/recycle during poll; every syscall rechecks ownership
        # under _lock, so a readiness result never authorizes use of a stale FD.
        poller = select.poll()
        poller.register(self._fd, event)
        while not self._closed.is_set():
            if poller.poll(50):
                return

    def read1(self, size: int) -> bytes:
        while True:
            with self._lock:
                if self._closed.is_set():
                    return b""
                try:
                    return os.read(self._fd, size)
                except BlockingIOError:
                    pass
            self._wait(select.POLLIN)

    def write(self, data: bytes) -> int:
        remaining = memoryview(data)
        while remaining:
            with self._lock:
                if self._closed.is_set():
                    raise BrokenPipeError("provider input is closed")
                try:
                    written = os.write(self._fd, remaining[:65536])
                except BlockingIOError:
                    written = 0
            if written:
                remaining = remaining[written:]
            else:
                self._wait(select.POLLOUT)
        return len(data)

    def flush(self) -> None:
        """Writes go directly to the pipe and have no user-space buffer."""

    def close(self) -> None:
        with self._lock:
            self._closed.set()
            self._raw.close()


def _interruptible(stream: Any) -> Any:
    raw = getattr(stream, "buffer", stream)
    raw = getattr(raw, "raw", raw)
    if os.name == "posix" and isinstance(raw, io.FileIO):
        return InterruptiblePipe(raw)
    return stream


class ProcessPipes:
    """Adapt real POSIX pipes while preserving injected streams and other OSes."""

    def __init__(self, process: Any):
        self.stdin = _interruptible(process.stdin)
        self.stdout = _interruptible(process.stdout)
        self.stderr = _interruptible(process.stderr)

    def close(self) -> None:
        close_process_pipes(self)


def open_process_pipes(process: Any, stop) -> ProcessPipes:
    """Roll back a spawned child if any pipe cannot be made interruptible."""
    try:
        return ProcessPipes(process)
    except BaseException:
        try:
            stop(process)
        finally:
            close_process_pipes(process)
        raise


class BoundedLineReader:
    """Require complete UTF-8 LF frames, including the delimiter in the budget."""

    def __init__(self, stream: Any, maximum: int, label: str):
        if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
            raise ValueError("max_line_bytes must be a positive integer")
        self._stream, self._maximum, self._label = stream, maximum, label
        self._buffer = bytearray()

    def read_line(self) -> Optional[str]:
        scanned = 0
        while True:
            end = self._buffer.find(b"\n", scanned)
            if end >= 0:
                return self._take_line(end + 1)
            if len(self._buffer) >= self._maximum:
                raise ValueError(f"{self._label} record exceeds {self._maximum} bytes")
            scanned = len(self._buffer)
            chunk = read_chunk(self._stream, min(8192, self._maximum - scanned))
            if not chunk:
                if self._buffer:
                    raise ValueError(f"{self._label} stream ended with an unterminated record")
                return None
            self._buffer.extend(chunk)

    def _take_line(self, size: int) -> str:
        if size > self._maximum:
            raise ValueError(f"{self._label} record exceeds {self._maximum} bytes")
        raw = self._buffer[:size]
        del self._buffer[:size]
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{self._label} record is not valid UTF-8") from exc
