"""Typed-event turn execution for :mod:`pbatch.tui_backend`."""

from __future__ import annotations

import inspect
import json
import threading
import time
from typing import Any, Callable, ContextManager, Optional

from . import config
from .lock import LOCK_EXIT_CODE, acquire_lock, release_lock
from .models import Task, TaskResult


AdapterFactory = Callable[..., Any]
Notify = Callable[[str, str], None]


class _StructuredState:
    def __init__(self) -> None:
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.terminal_kind = ""
        self.terminal_payload: dict[str, Any] = {}
        self.error: Optional[Exception] = None
        self.done = threading.Event()

    @property
    def terminal(self) -> bool:
        return bool(self.terminal_kind)


class _InterruptController:
    """Coordinate one adapter handle without leaking it into TUIBackend."""

    def __init__(self, adapter: Any, notify: Notify):
        self._adapter = adapter
        self._notify = notify
        self._handle: Any = None
        self._sent = False
        self._lock = threading.Lock()

    def set_handle(self, handle: Any) -> None:
        with self._lock:
            self._handle = handle

    def interrupt(self) -> None:
        with self._lock:
            if self._handle is None or self._sent:
                return
            self._sent = True
            handle = self._handle
        try:
            self._adapter.interrupt_turn(handle)
        except Exception as exc:
            self._notify("stderr", f"structured interrupt failed: {exc}")


def run_turn_structured(
        backend: Any, prompt: str, *, adapter_factory: Optional[AdapterFactory],
        turn_lock: Any, begin_turn: Callable[[], tuple[int, str]],
        make_task: Callable[[str], Task],
        runtime_config: Callable[[], ContextManager[Any]],
        finish_turn: Callable[[TaskResult], TaskResult],
        record_result: Callable[[TaskResult], None], end_turn: Callable[[], None],
        install_interrupt: Callable[[Optional[Callable[[], None]]], None],
        notify: Notify, is_cancelled: Callable[[], bool]) -> TaskResult:
    """Execute one structured turn while the backend owns shared governance."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must not be empty")
    if not turn_lock.acquire(blocking=False):
        raise RuntimeError("another TUI turn is already running")
    try:
        turn, _flags_kind = begin_turn()
        task = make_task(prompt)
        result = _execute_structured(
            backend, task, turn, adapter_factory, runtime_config,
            record_result, install_interrupt, notify, is_cancelled)
        return finish_turn(result)
    finally:
        install_interrupt(None)
        end_turn()
        turn_lock.release()


def _execute_structured(
        backend: Any, task: Task, turn: int,
        factory: Optional[AdapterFactory],
        runtime_config: Callable[[], ContextManager[Any]],
        record_result: Callable[[TaskResult], None],
        install_interrupt: Callable[[Optional[Callable[[], None]]], None],
        notify: Notify, is_cancelled: Callable[[], bool]) -> TaskResult:
    try:
        with runtime_config():
            return _execute_locked(
                backend, task, turn, factory, record_result,
                install_interrupt, notify, is_cancelled)
    except Exception as exc:
        return TaskResult(
            task, False, stderr=str(exc), reason=str(exc),
            session_id=backend.session_id, session_name=backend.session_name)


def _execute_locked(
        backend: Any, task: Task, turn: int,
        factory: Optional[AdapterFactory],
        record_result: Callable[[TaskResult], None],
        install_interrupt: Callable[[Optional[Callable[[], None]]], None],
        notify: Notify, is_cancelled: Callable[[], bool]) -> TaskResult:
    lock_path = None
    try:
        lock_path = acquire_lock(backend.cwd)
    except SystemExit as exc:
        reason = f"workspace is locked (exit {exc.code or LOCK_EXIT_CODE})"
        return TaskResult(task, False, returncode=LOCK_EXIT_CODE,
                          stderr=reason, reason=reason)
    try:
        result = _invoke_structured(
            backend, task, turn, factory, install_interrupt, notify, is_cancelled)
        record_result(result)
        return result
    finally:
        release_lock(lock_path)


def _invoke_structured(
        backend: Any, task: Task, turn: int,
        factory: Optional[AdapterFactory],
        install_interrupt: Callable[[Optional[Callable[[], None]]], None],
        notify: Notify, is_cancelled: Callable[[], bool]) -> TaskResult:
    adapter = _new_adapter(factory, backend.cwd)
    control = _InterruptController(adapter, notify)
    install_interrupt(control.interrupt)
    started = time.monotonic()
    state = _StructuredState()
    consumer = None
    try:
        adapter.initialize({"workspace_id": backend.cwd})
        _adopt_provider_session(adapter, backend, notify)
        if is_cancelled():
            return _cancelled_result(task)
        handle = adapter.start_turn({
            "prompt": task.prompt, "session_id": backend.session_id,
            "run_id": f"tui-{backend.session_id}-{turn}",
        })
        control.set_handle(handle)
        consumer = threading.Thread(
            target=_consume_events, args=(adapter, state, notify),
            name=f"tui-structured-{turn}", daemon=True)
        consumer.start()
        timed_out = _await_structured(
            adapter, control, state, started, backend.timeout, is_cancelled)
        return _structured_result(
            backend, task, state, started, timed_out, is_cancelled)
    finally:
        try:
            adapter.close()
        except Exception as exc:
            notify("stderr", f"structured adapter close failed: {exc}")
        if consumer is not None:
            consumer.join(timeout=1.0)


def _adopt_provider_session(adapter: Any, backend: Any, notify: Notify) -> None:
    """Resume the previous turn's provider thread when the adapter supports
    it (CodexExecAdapter persists its thread id in ``session()``); otherwise
    start a fresh session. The captured thread id is stored back on the
    backend so the NEXT turn can resume the same conversation — this is
    what makes multi-turn UI chats carry context (T5/T6 headless semantics,
    now wired into the TUI path)."""
    thread_id = str(getattr(backend, "provider_thread_id", "") or "").strip()
    if thread_id and hasattr(adapter, "resume_session"):
        try:
            adapter.resume_session({
                "provider_session_id": thread_id,
                "workspace_id": backend.cwd,
            })
            notify("status", f"resuming provider thread {thread_id[:12]}…")
            return
        except Exception as exc:
            notify("stderr", f"provider resume failed ({exc}); starting fresh")
    adapter.start_session({
        "session_id": backend.session_id, "workspace_id": backend.cwd,
        "name": backend.session_name,
    })
    if hasattr(adapter, "session") and callable(adapter.session):
        _store_provider_thread(adapter, backend)


def _store_provider_thread(adapter: Any, backend: Any) -> None:
    """Persist the provider thread id observed during this turn so later
    turns can resume it. Best effort: any failure only logs."""
    try:
        session = adapter.session()
        thread_id = str(getattr(session, "provider_session_id", "") or "")
        if thread_id:
            setattr(backend, "provider_thread_id", thread_id)
    except Exception as exc:
        try:
            config.log.warning("provider thread capture skipped: %s", exc)
        except Exception:
            pass


def _new_adapter(factory: Optional[AdapterFactory], cwd: str) -> Any:
    if factory is not None:
        try:
            parameters = inspect.signature(factory).parameters.values()
        except (TypeError, ValueError):
            return factory(cwd)
        positional = [item for item in parameters if item.kind in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.VAR_POSITIONAL,
        }]
        return factory(cwd) if positional else factory()
    from .tui_adapter_codex import CodexExecAdapter

    return CodexExecAdapter(cwd)


def _consume_events(adapter: Any, state: _StructuredState, notify: Notify) -> None:
    try:
        for event in adapter.events():
            if _project_event(event, state, notify):
                return
        if not state.terminal:
            state.error = RuntimeError(
                "structured runtime event stream closed without a terminal event")
    except Exception as exc:
        state.error = exc
    finally:
        state.done.set()


def _project_event(event: Any, state: _StructuredState, notify: Notify) -> bool:
    kind = str(getattr(event, "kind", ""))
    payload = getattr(event, "payload", {})
    payload = payload if isinstance(payload, dict) else {}
    if kind in {"command.started", "command.completed"}:
        notify("command", _command_notice(kind, payload))
    elif kind == "command.output":
        notify("command-output", _command_output_notice(payload))
    elif kind == "file.changed":
        notify("file", _file_notice(payload))
    elif kind == "usage.updated":
        notify("usage", _usage_notice(payload))
    elif kind == "message.delta":
        text = _payload_text(payload, "text")
        _append_capped(state.stdout, text, config.OUTPUT_MAX_BYTES)
        notify("stdout", text)
    elif kind in {"runtime.warning", "runtime.error"}:
        text = _payload_text(payload, "text", "reason", "raw")
        _append_capped(state.stderr, text, 64 * 1024)
        notify("stderr", text)
    if kind in {"turn.completed", "turn.failed", "turn.interrupted"}:
        if payload.get("usage"):
            notify("usage", _usage_notice(payload))
        state.terminal_kind, state.terminal_payload = kind, payload
        return True
    return False


def _await_structured(adapter: Any, control: _InterruptController,
                      state: _StructuredState, started: float, timeout: int,
                      is_cancelled: Callable[[], bool]) -> bool:
    deadline = started + timeout
    timed_out = False
    while not state.done.wait(0.05):
        if is_cancelled():
            control.interrupt()
        if time.monotonic() >= deadline:
            timed_out = True
            control.interrupt()
        if (timed_out or is_cancelled()) and not state.done.wait(1.0):
            adapter.close()
            state.done.wait(1.0)
            break
    return timed_out


def _structured_result(backend: Any, task: Task, state: _StructuredState,
                       started: float, timed_out: bool,
                       is_cancelled: Callable[[], bool]) -> TaskResult:
    elapsed = max(0.0, time.monotonic() - started)
    if is_cancelled():
        result = _cancelled_result(task)
    elif timed_out:
        reason = f"agent timed out after {backend.timeout}s"
        result = TaskResult(task, False, returncode=-1, reason=reason)
    elif state.error is not None:
        reason = str(state.error)
        result = TaskResult(task, False, returncode=-1,
                            stderr=reason, reason=reason)
    else:
        result = _terminal_result(task, state)
    result.stdout = "".join(state.stdout)
    result.stderr = result.stderr or "".join(state.stderr)
    result.elapsed = elapsed
    result.session_id = backend.session_id
    result.session_name = backend.session_name
    result.streamed_output = bool(state.stdout or state.stderr)
    return result


def _cancelled_result(task: Task) -> TaskResult:
    return TaskResult(task, False, returncode=-1, reason="cancelled")


def _json_notice(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _bound_utf8(value: Any, maximum: int) -> str:
    text = str(value or "")
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text
    marker = "…"
    marker_size = len(marker.encode())
    if maximum < marker_size:
        return encoded[:maximum].decode("utf-8", "ignore")
    prefix = encoded[:maximum - marker_size].decode("utf-8", "ignore")
    return prefix + marker


def _payload_text(payload: dict[str, Any], *names: str) -> str:
    return next((_bound_utf8(payload.get(name), 48 * 1024) for name in names
                 if payload.get(name)), "")


def _command_notice(kind: str, payload: dict[str, Any]) -> str:
    value: dict[str, Any] = {
        "phase": kind.rsplit(".", 1)[-1],
        "item_id": _bound_utf8(payload.get("item_id"), 128),
        "command": _bound_utf8(payload.get("command"), 512),
    }
    for name in ("exit_code", "elapsed", "elapsed_ms", "status"):
        if payload.get(name) is not None:
            value[name] = payload[name]
    return _json_notice(value)


def _command_output_notice(payload: dict[str, Any]) -> str:
    notice = {
        "item_id": _bound_utf8(payload.get("item_id"), 128),
        "output": _bound_utf8(payload.get("aggregated_output", payload.get("output", "")),
                              1800),
    }
    encoded = _json_notice(notice)
    if len(encoded.encode("utf-8")) <= 2 * 1024:
        return encoded
    output = notice["output"]
    low, high, best = 0, len(output), ""
    while low <= high:
        middle = (low + high) // 2
        candidate = output[:middle]
        candidate_notice = _json_notice({**notice, "output": candidate})
        if len(candidate_notice.encode("utf-8")) <= 2 * 1024:
            best, low = candidate_notice, middle + 1
        else:
            high = middle - 1
    return best


def _file_notice(payload: dict[str, Any]) -> str:
    paths = []
    changes = payload.get("changes", [])
    if isinstance(changes, list):
        for change in changes[:16]:
            path = change.get("path", "") if isinstance(change, dict) else change
            if path:
                paths.append(_bound_utf8(path, 256))
    if payload.get("path"):
        paths.append(_bound_utf8(payload["path"], 256))
    return _json_notice({"paths": list(dict.fromkeys(paths)),
                         "truncated": bool(payload.get("truncated"))})


def _usage_notice(payload: dict[str, Any]) -> str:
    usage = payload.get("usage", payload)
    usage = usage if isinstance(usage, dict) else {}
    clean = {str(key): value for key, value in usage.items()
             if isinstance(value, (int, float)) and not isinstance(value, bool)}
    total = clean.get("total_tokens")
    if total is None:
        primary = [clean[key] for key in ("input_tokens", "output_tokens")
                   if key in clean]
        if primary:
            total = sum(primary)
        else:
            total = sum(value for key, value in clean.items()
                        if "token" in key and not key.startswith("cached_"))
    return _json_notice({"usage": clean, "total_tokens": total})


def _append_capped(parts: list[str], value: str, maximum: int) -> None:
    if not value:
        return
    used = sum(len(part.encode("utf-8")) for part in parts)
    if used >= maximum:
        return
    parts.append(_bound_utf8(value, maximum - used))


def _terminal_result(task: Task, state: _StructuredState) -> TaskResult:
    payload = state.terminal_payload
    success = state.terminal_kind == "turn.completed"
    returncode = payload.get("returncode", 0 if success else -1)
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        returncode = 0 if success else -1
    reason = _payload_text(payload, "reason")
    if not success and not reason:
        reason = ("cancelled" if state.terminal_kind == "turn.interrupted"
                  else "structured turn failed")
    return TaskResult(task, success, returncode=returncode, reason=reason)
