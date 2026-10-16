"""Absolute deadlines and scoped teardown for synchronous runtime adapters."""

from __future__ import annotations

import threading
import time
import queue
from typing import Any

from .tui_signals import tui_signal_handlers


INTERRUPT_GRACE_SECONDS = 0.2
CLOSE_WAIT_SECONDS = 3.0


class RuntimeOperationStopped(RuntimeError):
    """A local operation stopped without claiming a provider terminal result."""


class RuntimeOperation:
    """Own one adapter operation, including its start/handle publication race.

    The watchdog also runs while a synchronous start RPC is waiting. Closing the
    transport releases those RPC waiters and reaps its local process group. No
    terminal event is invented: the caller reconciles missing evidence as UNKNOWN.
    """

    def __init__(self, adapter: Any, timeout: float, label: str):
        self.adapter = adapter
        self.timeout = timeout
        self.label = label
        self.deadline = time.monotonic() + timeout
        self.reason = ""
        self.close_error = ""
        self._handle = None
        self._finished = False
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._timer = threading.Timer(timeout, self._expire)
        self._timer.daemon = True
        self._timer.start()

    def bind(self, handle: Any) -> None:
        with self._lock:
            self._handle = handle
        self.check()

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            self._expire()
        with self._lock:
            if self.reason:
                raise RuntimeOperationStopped(self.reason)

    def complete(self) -> None:
        """Atomically accept a validated terminal event before its deadline."""
        with self._lock:
            self.check()
            self._finished = True
            self._timer.cancel()

    def cancel(self) -> bool:
        return self.stop(f"{self.label} cancelled; provider completion is unconfirmed")

    def stop(self, reason: str, *, force: bool = False) -> bool:
        with self._lock:
            if self._finished and not force:
                return False
            if self.reason:
                return True
            self.reason = reason
            self._timer.cancel()
            worker = threading.Thread(
                target=self._close, name="tui-runtime-stop", daemon=True)
            worker.start()
        return True

    def finish(self) -> None:
        with self._lock:
            self._finished = True
            self._timer.cancel()
            stopped = bool(self.reason)
        if stopped:
            self._closed.wait(CLOSE_WAIT_SECONDS)

    @property
    def closed(self) -> bool:
        return self._closed.is_set() and not self.close_error

    def _expire(self) -> None:
        self.stop(f"{self.label} timed out after {self.timeout:g}s")

    def _close(self) -> None:
        try:
            with self._lock:
                handle = self._handle
            if handle is not None and not getattr(handle, "done", False):
                interrupt = threading.Thread(
                    target=self._interrupt, args=(handle,), daemon=True,
                    name="tui-runtime-interrupt")
                interrupt.start()
                interrupt.join(INTERRUPT_GRACE_SECONDS)
            self.adapter.close()
        except Exception as exc:
            self.close_error = str(exc)
        finally:
            self._closed.set()

    def _interrupt(self, handle: Any) -> None:
        try:
            self.adapter.interrupt_turn({"turn_id": handle.turn_id})
        except Exception:
            pass  # close is the bounded fallback when interrupt is unsupported


def require_terminal_projection(kind: str, admitted: Any) -> None:
    """Refuse explicitly unadmitted terminals; retain legacy void receipts."""
    if kind in {"turn.completed", "turn.failed", "turn.interrupted"} and admitted is False:
        raise RuntimeError("runtime terminal event was not admitted by required projection")


def operation_timeout(app: Any, fallback: float = 0) -> float:
    """Resolve the configured turn deadline without depending on a fake backend."""
    from . import config
    return float(app.options.timeout or fallback or config.AGENT_DEFAULT_TIMEOUT)


def next_runtime_event(stream: Any, handle: Any, label: str = "runtime turn") -> Any:
    """Recheck the queue after observing done across a timed-out read.

    A producer may enqueue its terminal event and set done after get() raises
    Empty but before the consumer checks done. The final nonblocking read
    distinguishes that completed publication from an actually missing event.
    """
    try:
        return stream.get(timeout=0.1)
    except queue.Empty:
        if not getattr(handle, "done", False):
            raise
    try:
        return stream.get(timeout=0)
    except queue.Empty as exc:
        raise RuntimeError(f"{label} ended without a terminal event") from exc


class TUIRuntimeLifecycleMixin:
    """Keep managed runtime workers bounded, including the start RPC."""

    def run_interactive(self) -> int:
        """Own OS shutdown handlers for exactly one interactive app lifetime."""
        with tui_signal_handlers(self) as stopped:
            self.run()
        return stopped.return_code or self.return_code

    def _begin_runtime_operation(self, label: str, fallback: float = 0) -> None:
        self._runtime_operation = (RuntimeOperation(
            self._adapter, operation_timeout(self, fallback), label)
            if self._strict_adapter and self._adapter is not None else None)

    def _run_turn_worker(self, prompt: str) -> None:
        result, error = None, None
        adapter = self._adapter
        operation = self._runtime_operation
        if adapter is None:
            result, error = self._run_backend_turn(prompt)
        else:
            try:
                result = self._run_adapter_turn(adapter, operation, prompt)
            except Exception as exc:
                error = exc
        error = self._finish_runtime_operation(operation, error)
        self._turn_handle = None
        self._capture_runtime_finish()
        try:
            self.call_from_thread(self._finish_turn, result, error)
        except RuntimeError:
            pass  # application closed while the backend was unwinding

    def _run_adapter_turn(self, adapter: Any, operation: Any, prompt: str) -> Any:
        try:
            if operation is not None:
                operation.check()
            stream = adapter.events()
            handle = adapter.start_turn({
                "prompt": prompt, "session_id": self._adapter_session_id,
                "model": self._option_value("model"),
                **self._runtime_submission_request(),
            })
            self._bind_runtime_submission(handle)
            self._turn_handle = handle
            if operation is not None:
                operation.bind(handle)
        except Exception:
            if self._strict_adapter:
                raise
            self._disable_adapter(adapter)
            result, error = self._run_backend_turn(prompt)
            if error is not None:
                raise error
            return result
        try:
            return self._consume_adapter_turn(stream, handle, operation)
        except Exception as exc:
            if operation is not None:
                raise
            raise self._abort_runtime_stream(adapter, handle, exc)
        finally:
            self._turn_handle = None

    def _consume_adapter_turn(self, stream: Any, handle: Any,
                              operation: Any = None) -> dict[str, Any]:
        while True:
            if operation is not None:
                operation.check()
            try:
                event = next_runtime_event(stream, handle)
            except queue.Empty:
                continue
            if operation is not None:
                operation.check()
            if event is None:
                raise RuntimeError("runtime event stream closed during a turn")
            kind, payload = self._runtime_event_context(handle, event)
            terminal = kind in {"turn.completed", "turn.failed", "turn.interrupted"}
            admitted = self._ingest_runtime_event(event)
            if terminal:
                require_terminal_projection(kind, admitted)
                if operation is not None:
                    operation.complete()
                return {"success": kind == "turn.completed", "stdout": "", "stderr": "",
                        "reason": payload.get("reason", ""),
                        "returncode": payload.get("returncode", 0)}

    def _finish_runtime_operation(self, operation: Any, error: Any) -> Any:
        if operation is None:
            return error
        if error is not None:
            operation.stop(str(error), force=True)
        operation.finish()
        if operation.reason:
            self._runtime_blocked_reason = (
                f"{operation.reason}. Start `/new` to reconnect the runtime.")
            return RuntimeOperationStopped(self._runtime_blocked_reason)
        if self._runtime_operation is operation:
            self._runtime_operation = None
        return error

    def _restart_stopped_runtime(self) -> bool:
        operation = self._runtime_operation
        stopped = operation is not None and bool(operation.reason)
        # Both managed RPC adapters latch this fact on EOF/protocol failure,
        # including while idle, when no RuntimeOperation exists to stop.
        disconnected = (self._strict_adapter and
                        getattr(self._adapter, "_stream_ended", False) is True)
        retrying = getattr(self, "_runtime_reconnect_pending", False)
        if not (stopped or disconnected or retrying):
            return False
        if stopped and not operation.closed:
            raise RuntimeError("runtime teardown is not confirmed; restart the TUI")
        if stopped:
            self._save_pending_runtime_failure()
        # Preserve the explicit recovery intent if constructing the replacement
        # fails. The next /new may retry; turns must remain blocked meanwhile.
        self._runtime_reconnect_pending = True
        # A stopped operation already reconciled UNKNOWN; its late events
        # cannot be folded over that terminal result during recovery.
        if not stopped:
            self._prepare_runtime_session_switch()
        self._rebind_runtime()
        if self._runtime_blocked_reason:
            raise RuntimeError(self._runtime_blocked_reason)
        self._runtime_operation = None
        self._runtime_reconnect_pending = False
        return True
