"""Bounded HTTP drain and process-local signal handling for the Hub listener."""
from __future__ import annotations

from contextlib import contextmanager
import math
import signal
import socket
import threading
import time


class RequestDrain:
    """Only track/interrupt transports admitted by this server, at most 32."""

    def __init__(self):
        self._condition = threading.Condition()
        self._requests = set()
        self._stopping = False

    def admit(self, request):
        with self._condition:
            if self._stopping or len(self._requests) >= 32 or request in self._requests:
                return False
            self._requests.add(request)
            return True

    def complete(self, request):
        with self._condition:
            self._requests.discard(request)
            self._condition.notify_all()

    def stop(self):
        with self._condition:
            self._stopping = True
            for request in self._requests:
                try:
                    # Serialize against close: the fd cannot be reused during shutdown().
                    request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # Already disconnected; completion must still be observed.

    def close_transport(self, request):
        with self._condition:
            request.close()

    def wait(self, deadline):
        with self._condition:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('Hub HTTP drain timed out; in-flight results may be unknown')
                if not self._requests:
                    return
                self._condition.wait(remaining)


def drain_deadline(timeout):
    if (type(timeout) not in (int, float) or not 0 < timeout <= 30
            or not math.isfinite(timeout)):
        raise ValueError('Hub HTTP drain timeout must be finite and within (0, 30] seconds')
    return time.monotonic() + timeout


class StopSignal:
    def __init__(self):
        self.number = 0

    def request(self, number, _frame):
        # No locks, I/O, thread joins or asynchronous exceptions inside the handler.
        if not self.number:
            self.number = number


@contextmanager
def termination_signals():
    if threading.current_thread() is not threading.main_thread():
        raise ValueError('Hub CLI signal handling requires the main thread')
    stop = StopSignal()
    previous = {}
    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, stop.request)
        yield stop
    finally:
        for number, handler in previous.items():
            if signal.getsignal(number) == stop.request:
                signal.signal(number, handler)


def serve_until_stopped(server, stop):
    """Do not call BaseServer.shutdown() from its own serving/signal thread."""
    previous = server.timeout
    server._stop_signal = stop
    server.timeout = 0.25
    try:
        while not stop.number:
            server.handle_request()
    finally:
        server.timeout = previous
