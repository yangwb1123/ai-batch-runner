"""Typed-event turn execution for :mod:`pbatch.tui_backend`."""

from __future__ import annotations

import inspect
import json
import shlex
import threading
import time
import uuid
from typing import Any, Callable, ContextManager, Optional

from . import config
from .lock import LOCK_EXIT_CODE, acquire_lock, release_lock
from .models import Task, TaskResult
from .tui_lifecycle import RuntimeOperation, RuntimeOperationStopped, require_terminal_projection
from .tui_messages import MessageProjection


AdapterFactory = Callable[..., Any]
Notify = Callable[[str, str], None]
EventSink = Callable[[Any], Optional[bool]]


class _StructuredState:
    def __init__(self, operation: RuntimeOperation) -> None:
        self.operation = operation
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.messages = MessageProjection(config.OUTPUT_MAX_BYTES)
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
        notify: Notify, is_cancelled: Callable[[], bool],
        event_sink: Optional[EventSink] = None) -> TaskResult:
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
            record_result, install_interrupt, notify, is_cancelled, event_sink)
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
        notify: Notify, is_cancelled: Callable[[], bool],
        event_sink: Optional[EventSink]) -> TaskResult:
    try:
        with runtime_config():
            return _execute_locked(
                backend, task, turn, factory, record_result,
                install_interrupt, notify, is_cancelled, event_sink)
    except Exception as exc:
        return TaskResult(
            task, False, stderr=str(exc), reason=str(exc),
            session_id=backend.session_id, session_name=backend.session_name)


def _execute_locked(
        backend: Any, task: Task, turn: int,
        factory: Optional[AdapterFactory],
        record_result: Callable[[TaskResult], None],
        install_interrupt: Callable[[Optional[Callable[[], None]]], None],
        notify: Notify, is_cancelled: Callable[[], bool],
        event_sink: Optional[EventSink]) -> TaskResult:
    lock_path = None
    try:
        lock_path = acquire_lock(
            backend.cwd,
            session_name=getattr(backend, "session_name", "") or "")
    except SystemExit as exc:
        reason = f"workspace is locked (exit {exc.code or LOCK_EXIT_CODE})"
        return TaskResult(task, False, returncode=LOCK_EXIT_CODE,
                          stderr=reason, reason=reason)
    try:
        result = _invoke_structured(
            backend, task, turn, factory, install_interrupt, notify,
            is_cancelled, event_sink)
        record_result(result)
        return result
    finally:
        release_lock(lock_path)


def _invoke_structured(
        backend: Any, task: Task, turn: int,
        factory: Optional[AdapterFactory],
        install_interrupt: Callable[[Optional[Callable[[], None]]], None],
        notify: Notify, is_cancelled: Callable[[], bool],
        event_sink: Optional[EventSink]) -> TaskResult:
    adapter = _new_adapter(factory, backend)
    control = _InterruptController(adapter, notify)
    install_interrupt(control.interrupt)
    operation = RuntimeOperation(adapter, backend.timeout, "agent")
    started = operation.deadline - backend.timeout
    state = _StructuredState(operation)
    consumer = None
    try:
        operation.check()
        adapter.initialize({"workspace_id": backend.cwd})
        operation.check()
        _adopt_provider_session(adapter, backend, notify)
        operation.check()
        if is_cancelled():
            return _cancelled_result(task)
        handle = adapter.start_turn({
            "prompt": task.prompt, "session_id": backend.session_id,
            "run_id": f"tui-{uuid.uuid4().hex}",
        })
        control.set_handle(handle)
        operation.bind(handle)
        consumer = threading.Thread(
            target=_consume_events, args=(adapter, state, notify, event_sink),
            name=f"tui-structured-{turn}", daemon=True)
        consumer.start()
        timed_out = _await_structured(adapter, control, state, is_cancelled)
        # The provider thread id is only known AFTER the turn: real adapters
        # capture it from the first thread.started event while consuming
        # events (fake adapters preset it at construction — a trap). Without
        # this post-turn read, multi-turn chats never resume.
        _store_provider_thread(adapter, backend)
        return _structured_result(
            backend, task, state, started, timed_out, is_cancelled)
    except RuntimeOperationStopped:
        return _structured_result(backend, task, state, started, True, is_cancelled)
    finally:
        operation.finish()
        try:
            adapter.close()
        except Exception as exc:
            notify("stderr", f"structured adapter close failed: {exc}")
        if consumer is not None:
            consumer.join(timeout=1.0)


def _adopt_provider_session(adapter: Any, backend: Any, notify: Notify) -> None:
    """Adopt an explicitly forked/resumed provider thread or start fresh."""
    thread_id = str(getattr(backend, "provider_thread_id", "") or "").strip()
    required = bool(getattr(backend, "provider_resume_required", False))
    fork_required = bool(getattr(backend, "provider_fork_required", False))
    if required and fork_required:
        raise RuntimeError("provider resume and fork cannot both be pending")
    if required and not thread_id:
        raise RuntimeError("required provider resume handle is missing")
    if fork_required and not thread_id:
        raise RuntimeError("required provider fork handle is missing")
    if fork_required:
        _fork_provider_session(adapter, backend, notify, thread_id)
        return
    if thread_id and hasattr(adapter, "resume_session"):
        try:
            adapter.resume_session({
                "provider_session_id": thread_id,
                "session_id": backend.session_id,
                "workspace_id": backend.cwd,
            })
            setattr(backend, "provider_resume_required", False)
            notify("status", f"resuming provider thread {thread_id[:12]}…")
            return
        except Exception as exc:
            if required:
                raise RuntimeError(f"required provider resume failed: {exc}") from exc
            notify("stderr", f"provider resume failed ({exc}); starting fresh")
    elif required:
        raise RuntimeError("active runtime does not support required provider resume")
    adapter.start_session({
        "session_id": backend.session_id, "workspace_id": backend.cwd,
        "name": backend.session_name,
    })


def _fork_provider_session(
        adapter: Any, backend: Any, notify: Notify, thread_id: str) -> None:
    fork = getattr(adapter, "fork_session", None)
    if not callable(fork):
        raise RuntimeError("active runtime does not support required provider fork")
    try:
        fork({
            "provider_session_id": thread_id,
            "session_id": backend.session_id,
            "workspace_id": backend.cwd,
        })
    except Exception as exc:
        raise RuntimeError(f"required provider fork failed: {exc}") from exc
    notify("status", f"forking provider thread {thread_id[:12]}…")


def _store_provider_thread(adapter: Any, backend: Any) -> None:
    """Persist the provider thread id observed during this turn so later
    turns can resume it. ``session`` may be a property (CodexExecAdapter)
    or a callable (fakes); both are handled. Best effort: any failure
    only logs."""
    try:
        session = getattr(adapter, "session", None)
        if callable(session):
            session = session()
        if session is None:
            return
        thread_id = str(getattr(session, "provider_session_id", "") or "")
        if thread_id:
            previous = str(getattr(backend, "provider_thread_id", "") or "")
            setattr(backend, "provider_thread_id", thread_id)
            if (getattr(backend, "provider_fork_required", False)
                    and thread_id != previous):
                setattr(backend, "provider_fork_required", False)
    except Exception as exc:
        try:
            config.log.warning("provider thread capture skipped: %s", exc)
        except Exception:
            pass


def _new_adapter(factory: Optional[AdapterFactory], backend: Any) -> Any:
    cwd = backend.cwd
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
    from .tui_adapter_codex import CodexExecAdapter, DEFAULT_ARGV_PREFIX

    provider = str(getattr(backend, "provider", "") or "").strip()
    if provider:
        raise ValueError("provider override is unsupported by Codex Structured Exec")
    prefix = list(DEFAULT_ARGV_PREFIX)
    agent = str(getattr(backend, "agent", "") or prefix[0]).strip()
    agent_argv = shlex.split(agent)
    if not agent_argv:
        raise ValueError("structured agent binary must not be empty")
    prefix[0:1] = agent_argv
    model = str(getattr(backend, "model", "") or "").strip()
    if model:
        prefix[-1:-1] = ["--model", model]
    return CodexExecAdapter(cwd, argv_prefix=prefix)


def _consume_events(adapter: Any, state: _StructuredState, notify: Notify,
                    event_sink: Optional[EventSink]) -> None:
    try:
        for event in adapter.events():
            state.operation.check()
            if event_sink is not None:
                admitted = event_sink(event)
                require_terminal_projection(str(getattr(event, "kind", "")), admitted)
                if admitted is False:
                    continue
            if _project_event(event, state, notify, present=event_sink is None):
                state.operation.complete()
                return
        if not state.terminal:
            state.error = RuntimeError(
                "structured runtime event stream closed without a terminal event")
    except Exception as exc:
        state.error = exc
    finally:
        state.done.set()


def _project_event(event: Any, state: _StructuredState, notify: Notify, *,
                   present: bool = True) -> bool:
    kind = str(getattr(event, "kind", ""))
    payload = getattr(event, "payload", {})
    payload = payload if isinstance(payload, dict) else {}
    emit = notify if present else lambda _kind, _text: None
    if kind in {"command.started", "command.completed"}:
        emit("command", _command_notice(kind, payload))
    elif kind == "command.output":
        emit("command-output", _command_output_notice(payload))
    elif kind == "file.changed":
        emit("file", _file_notice(payload))
    elif kind == "plan.updated":
        emit("plan", _plan_notice(payload))
    elif kind == "usage.updated":
        emit("usage", _usage_notice(payload))
    elif kind in {"message.started", "message.delta", "message.completed"}:
        text = _payload_text(payload, "text")
        previous = state.messages.snapshot()
        view = state.messages.consume(kind, payload.get("item_id", ""), text)
        state.stdout[:] = [view.text] if view.text else []
        callback_kind, visible = _message_callback(
            kind, previous.text, view.text, text)
        if callback_kind:
            emit(callback_kind, visible)
    elif kind in {"runtime.warning", "runtime.error"}:
        text = _payload_text(payload, "text", "reason", "raw")
        _append_capped(state.stderr, text, 64 * 1024)
        emit("stderr", text)
    if kind in {"turn.completed", "turn.failed", "turn.interrupted"}:
        if payload.get("usage"):
            emit("usage", _usage_notice(payload))
        state.terminal_kind, state.terminal_payload = kind, payload
        return True
    return False


def _await_structured(adapter: Any, control: _InterruptController,
                      state: _StructuredState,
                      is_cancelled: Callable[[], bool]) -> bool:
    timed_out = False
    while not state.done.wait(0.05):
        if is_cancelled():
            control.interrupt()
        try:
            state.operation.check()
        except RuntimeOperationStopped:
            timed_out = True
        if (timed_out or is_cancelled()) and not state.done.wait(1.0):
            adapter.close()
            state.done.wait(1.0)
            break
    try:
        state.operation.check()
    except RuntimeOperationStopped:
        timed_out = True
    return timed_out


def _message_callback(kind: str, previous: str, current: str,
                      delta: str) -> tuple[str, str]:
    if kind == "message.delta":
        return "stdout", delta
    if kind != "message.completed":
        return "", ""
    if not previous:
        return "stdout", current
    if current.startswith(previous):
        return "stdout", current[len(previous):]
    return "stdout-replace", current


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


def _plan_notice(payload: dict[str, Any]) -> str:
    """Project provider plan data into a small, provider-neutral notice."""
    raw_items = payload.get("items", [])
    raw_items = raw_items if isinstance(raw_items, list) else []
    items = [_plan_notice_item(item) for item in raw_items[:64]]
    return _json_notice({
        "item_id": _bound_utf8(payload.get("item_id"), 128),
        "status": _bound_utf8(payload.get("status"), 64),
        "items": items,
        "truncated": bool(payload.get("truncated") or len(raw_items) > len(items)),
    })


def _plan_notice_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"text": _bound_utf8(item, 256)}
    value: dict[str, Any] = {}
    for name in ("text", "title", "name"):
        if item.get(name) is not None:
            value[name] = _bound_utf8(item[name], 256)
            break
    for name in ("status", "state"):
        if item.get(name) is not None:
            value[name] = _bound_utf8(item[name], 64)
            break
    if isinstance(item.get("completed"), bool):
        value["completed"] = item["completed"]
    return value


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
