"""Legacy CLI adapter: map the callback-based backend to trusted runtime events.

Design (docs/CODEX_CLI_TUI_DESIGN.md §9.2): stdout -> message.delta,
stderr/log -> runtime.warning, TaskResult -> turn.completed|failed,
workspace delta -> workspace.snapshot / file.changed; capabilities stay
honest (only message_stream + interrupt).
"""

from __future__ import annotations

import hashlib
import math
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Mapping, Optional

from .tui_protocol import (
    RUNTIME_PROTOCOL_VERSION,
    RuntimeCapabilities,
    RuntimeEvent,
    TERMINAL_TURN_STATES,
    TurnState,
)
from .tui_runtime import (
    ApprovalDecision,
    ForkSessionRequest,
    ResumeSessionRequest,
    RuntimeInfo,
    RuntimeSession,
    StartTurnRequest,
    TurnHandle,
    UnsupportedOperation,
)


LEGACY_RUNTIME_NAME = "legacy"
DEFAULT_RUNTIME_QUEUE_SIZE = 256
MAX_RUN_SEQUENCES = 4_096
OUTPUT_SUMMARY_CHARS = 2_000




class _BoundedRuntimeQueue:
    """A closeable queue which only sacrifices warning events on pressure."""

    def __init__(self, maxsize: int):
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize <= 0:
            raise ValueError("maxsize must be a positive integer")
        self.maxsize = maxsize
        self._items: deque[RuntimeEvent] = deque()
        self._reserved = 0
        self._closed = False
        self._condition = threading.Condition()

    def put(self, event: RuntimeEvent) -> bool:
        with self._condition:
            if self._closed:
                return False
            if (len(self._items) + self._reserved >= self.maxsize
                    and not self._discard_warning()):
                if event.kind == "runtime.warning":
                    return False
                raise OverflowError("legacy runtime event queue is full")
            self._items.append(event)
            self._condition.notify()
            return True

    def reserve(self) -> None:
        with self._condition:
            if self._closed:
                raise RuntimeError("legacy runtime event queue is closed")
            if (len(self._items) + self._reserved >= self.maxsize
                    and not self._discard_warning()):
                raise OverflowError("legacy runtime event queue is full")
            self._reserved += 1

    def put_reserved(self, event: RuntimeEvent) -> None:
        with self._condition:
            if self._reserved <= 0:
                raise RuntimeError("legacy runtime queue reservation is missing")
            self._reserved -= 1
            if self._closed:
                raise RuntimeError("legacy runtime event queue is closed")
            self._items.append(event)
            self._condition.notify()

    def release_reservation(self) -> None:
        with self._condition:
            if self._reserved:
                self._reserved -= 1

    def _discard_warning(self) -> bool:
        for index, queued in enumerate(self._items):
            if queued.kind == "runtime.warning":
                del self._items[index]
                return True
        return False

    def get(self, timeout: Optional[float] = None) -> Optional[RuntimeEvent]:
        with self._condition:
            deadline = None if timeout is None else time.monotonic() + max(0, timeout)
            while not self._items and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            if self._items:
                return self._items.popleft()
            return None

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


class _RuntimeEventStream:
    """Iterator-compatible queue view used by incremental TUI consumers."""

    def __init__(self, source: _BoundedRuntimeQueue):
        self._source = source

    def get(self, timeout: Optional[float] = None) -> Optional[RuntimeEvent]:
        return self._source.get(timeout)

    def __iter__(self) -> _RuntimeEventStream:
        return self

    def __next__(self) -> RuntimeEvent:
        event = self._source.get()
        if event is None:
            raise StopIteration
        return event



class LegacyAdapter:
    """Map the existing callback-based backend to trusted runtime events."""

    _REQUIRED_METHODS = (
        "new_session", "run_turn", "cancel", "workspace_status",
        "workspace_diff",
    )

    def __init__(self, backend: Any, *, maxsize: int = DEFAULT_RUNTIME_QUEUE_SIZE):
        missing = [name for name in self._REQUIRED_METHODS
                   if not callable(getattr(backend, name, None))]
        if missing:
            raise TypeError(f"backend is missing callable methods: {', '.join(missing)}")
        self._backend = backend
        self._queue = _BoundedRuntimeQueue(maxsize)
        self._lock = threading.RLock()
        self._cancel_condition = threading.Condition(self._lock)
        self._closed = False
        self._session: Optional[RuntimeSession] = None
        self._active: Optional[TurnHandle] = None
        self._interrupt_requested: set[str] = set()
        self._cancel_accepted: set[str] = set()
        self._cancel_inflight: set[str] = set()
        self._start_committed: set[str] = set()
        self._seq_by_run: dict[str, int] = {}
        self._stream_seen: dict[str, bool] = {"stdout": False, "stderr": False}
        self._info = RuntimeInfo(
            runtime=LEGACY_RUNTIME_NAME,
            protocol_version=RUNTIME_PROTOCOL_VERSION,
            capabilities=RuntimeCapabilities(runtime=LEGACY_RUNTIME_NAME),
        )
        self._attach_callback()

    def _attach_callback(self) -> None:
        setter = getattr(self._backend, "set_emit", None)
        if callable(setter):
            setter(self._backend_emit)
            return
        try:
            setattr(self._backend, "emit", self._backend_emit)
        except (AttributeError, TypeError) as exc:
            raise TypeError("backend does not expose a writable emit callback") from exc

    def runtime_info(self) -> RuntimeInfo:
        """Return immutable local capabilities without reinitializing backend state."""
        return self._info

    def initialize(self, request: InitializeRequest) -> RuntimeInfo:
        self._ensure_open()
        return self._info

    def start_session(self, request: StartSessionRequest) -> RuntimeSession:
        self._ensure_open()
        with self._lock:
            if self._active is not None and not self._active.done:
                raise RuntimeError("cannot replace an active runtime session")
            run_id = _id("run")
            if (run_id not in self._seq_by_run
                    and len(self._seq_by_run) >= MAX_RUN_SEQUENCES):
                raise OverflowError("legacy runtime run sequence index is full")
            self._queue.reserve()
            reserved = True
            try:
                session, provider_id = self._new_runtime_session(request)
                self._publish(
                    "session.created", session=session, run_id=run_id,
                    turn_id="", payload={
                        "provider_session_id": provider_id,
                        "state": session.state,
                    }, reserved=True)
                reserved = False
            finally:
                if reserved:
                    self._queue.release_reservation()
            self._session = session
        return session

    def _new_runtime_session(
            self, request: StartSessionRequest) -> tuple[RuntimeSession, str]:
        name = str(_request_value(request, "name", ""))
        response = self._backend.new_session(name)
        provider_id = str(_value(response, "session_id", "") or
                          getattr(self._backend, "session_id", ""))
        session = RuntimeSession(
            session_id=str(_request_value(request, "session_id", "") or _id("ses")),
            provider_session_id=provider_id,
            workspace_id=str(_request_value(request, "workspace_id", "") or
                             self._workspace_id()),
            started_at=_now(), state="ACTIVE",
        )
        return session, provider_id

    def _adopt_session(self, request: StartSessionRequest) -> RuntimeSession:
        """Bind the adapter to a backend session without resetting it."""
        self._ensure_open()
        with self._lock:
            if self._active is not None and not self._active.done:
                raise RuntimeError("cannot replace an active runtime session")
        provider_id = str(getattr(self._backend, "session_id", ""))
        session = RuntimeSession(
            session_id=str(_request_value(request, "session_id", "") or _id("ses")),
            provider_session_id=provider_id,
            workspace_id=str(_request_value(request, "workspace_id", "") or
                             self._workspace_id()),
            started_at=_now(), state="ACTIVE",
        )
        with self._lock:
            self._session = session
        self._publish("session.created", session=session,
                      run_id=_id("run"), turn_id="", payload={
                          "provider_session_id": provider_id,
                          "state": session.state,
                      })
        return session

    def resume_session(self, request: ResumeSessionRequest) -> RuntimeSession:
        raise UnsupportedOperation("resume_session", "session_resume")

    def read_session(self, request: Any):
        raise UnsupportedOperation("read_session", "session_read")

    def list_sessions(self, request: Any = None):
        raise UnsupportedOperation("list_sessions", "session_list")

    def fork_session(self, request: ForkSessionRequest) -> RuntimeSession:
        raise UnsupportedOperation("fork_session", "session_fork")

    def start_turn(self, request: StartTurnRequest) -> TurnHandle:
        self._ensure_open()
        prompt = str(_request_value(request, "prompt", ""))
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        with self._lock:
            session = self._session
            if session is None:
                raise RuntimeError("start_session must be called before start_turn")
            requested_session = str(_request_value(request, "session_id", ""))
            if requested_session and requested_session != session.session_id:
                raise ValueError("turn request does not belong to the active session")
            if self._active is not None and not self._active.done:
                raise RuntimeError("a legacy turn is already running")
            handle = TurnHandle(
                session_id=session.session_id,
                run_id=str(_request_value(request, "run_id", "") or _id("run")),
                turn_id=str(_request_value(request, "turn_id", "") or _id("turn")),
                actor_id=str(_request_value(request, "actor_id", "agent_main") or
                             "agent_main"),
                started_at=_now(),
            )
            self._active = handle
            self._interrupt_requested.clear()
            self._cancel_accepted.clear()
            self._cancel_inflight.clear()
            self._start_committed.clear()
            self._stream_seen = {"stdout": False, "stderr": False}
        try:
            self._publish_for(handle, "turn.queued", {"state": TurnState.QUEUED})
        except Exception:
            with self._lock:
                if self._active is handle:
                    self._active = None
            handle._done.set()
            raise
        worker = threading.Thread(
            target=self._run_turn, args=(handle, prompt),
            name=f"legacy-turn-{handle.turn_id}", daemon=True)
        worker.start()
        return handle

    def _run_turn(self, handle: TurnHandle, prompt: str) -> None:
        try:
            self._ensure_not_interrupted(handle)
            before = self._workspace_status()
            self._ensure_not_interrupted(handle)
            self._publish_for(handle, "workspace.snapshot",
                              {"phase": "before", "workspace": before})
            self._commit_start(handle)
            result = self._backend.run_turn(prompt)
            self._raise_if_cancelled(handle)
            self._emit_result_fallback(handle, result)
            after = self._workspace_status()
            self._raise_if_cancelled(handle)
            diff = self._workspace_diff(handle)
            after_payload = {"phase": "after", "workspace": after}
            authoritative_delta = self._last_turn_workspace()
            if authoritative_delta is not None:
                after_payload["last_turn_workspace"] = authoritative_delta
            self._publish_for(handle, "workspace.snapshot", after_payload)
            for path in _changed_paths(before, after, diff):
                self._publish_for(handle, "file.changed", {"path": path})
            self._publish_result(handle, result)
        except Exception as exc:
            with self._cancel_condition:
                self._wait_for_cancel_decision(handle)
                interrupted = handle.turn_id in self._cancel_accepted
                handle.state = (TurnState.INTERRUPTED if interrupted
                                else TurnState.FAILED)
                self._safe_publish_for(handle, (
                    "turn.interrupted" if interrupted else "turn.failed"), {
                    "state": handle.state,
                    "reason": str(exc)[:OUTPUT_SUMMARY_CHARS],
                })
        finally:
            handle._done.set()

    def _ensure_not_interrupted(self, handle: TurnHandle) -> None:
        with self._lock:
            if handle.turn_id in self._interrupt_requested:
                raise RuntimeError("turn interrupted before backend execution")

    def _commit_start(self, handle: TurnHandle) -> None:
        """Linearization point after which backend invocation may occur."""
        with self._lock:
            if handle.turn_id in self._interrupt_requested:
                raise RuntimeError("turn interrupted before backend execution")
            self._start_committed.add(handle.turn_id)
            handle.state = TurnState.RUNNING
            self._publish_for(handle, "turn.started", {"state": handle.state})

    def _raise_if_cancelled(self, handle: TurnHandle) -> None:
        with self._cancel_condition:
            self._wait_for_cancel_decision(handle)
            if handle.turn_id in self._cancel_accepted:
                raise RuntimeError("backend accepted turn cancellation")

    def _wait_for_cancel_decision(self, handle: TurnHandle) -> None:
        """Wait with ``_cancel_condition`` held until cancel() has decided."""
        while handle.turn_id in self._cancel_inflight:
            self._cancel_condition.wait()

    def _workspace_status(self) -> dict[str, Any]:
        value = self._backend.workspace_status()
        if not isinstance(value, Mapping):
            raise TypeError("backend.workspace_status() must return a mapping")
        return dict(value)

    def _workspace_diff(self, handle: TurnHandle) -> dict[str, Any]:
        try:
            value = self._backend.workspace_diff()
            if not isinstance(value, Mapping):
                raise TypeError("workspace_diff() must return a mapping")
            return dict(value)
        except Exception as exc:
            self._safe_publish_for(handle, "runtime.warning", {
                "stream": "workspace", "text": str(exc)[:OUTPUT_SUMMARY_CHARS],
            })
            return {}

    def _last_turn_workspace(self) -> Optional[dict[str, Any]]:
        snapshot = getattr(self._backend, "snapshot", None)
        if not callable(snapshot):
            return None
        try:
            value = snapshot()
        except Exception:
            return None
        delta = (value.get("last_turn_workspace")
                 if isinstance(value, Mapping) else None)
        return dict(delta) if isinstance(delta, Mapping) else None

    def _emit_result_fallback(self, handle: TurnHandle, result: Any) -> None:
        stdout = str(_value(result, "stdout", "") or "")
        stderr = str(_value(result, "stderr", "") or "")
        with self._lock:
            if stdout and not self._stream_seen["stdout"]:
                self._publish_for(handle, "message.delta", {"text": stdout})
            if stderr and not self._stream_seen["stderr"]:
                self._publish_for(handle, "runtime.warning",
                                  {"stream": "stderr", "text": stderr})

    def _publish_result(self, handle: TurnHandle, result: Any) -> None:
        success = _value(result, "success", None) is True
        elapsed = _finite_number(_value(result, "elapsed", 0.0))
        with self._cancel_condition:
            self._wait_for_cancel_decision(handle)
            interrupted = handle.turn_id in self._cancel_accepted
            payload = {
                "state": (TurnState.INTERRUPTED if interrupted else
                          (TurnState.SUCCEEDED if success else TurnState.FAILED)),
                "output_summary": str(_value(result, "stdout", "") or "")[
                    :OUTPUT_SUMMARY_CHARS],
                "elapsed_seconds": elapsed,
                "returncode": _value(result, "returncode", -1),
                "reason": str(_value(result, "reason", "") or "")[
                    :OUTPUT_SUMMARY_CHARS],
            }
            handle.state = payload["state"]
            kind = ("turn.interrupted" if interrupted else
                    ("turn.completed" if success else "turn.failed"))
            self._publish_for(handle, kind, payload)

    def steer_turn(self, request: SteerRequest) -> None:
        raise UnsupportedOperation("steer_turn", "turn_steer")

    def interrupt_turn(self, request: InterruptRequest) -> None:
        self._ensure_open()
        turn_id = str(_request_value(request, "turn_id", ""))
        with self._cancel_condition:
            handle = self._active
            if handle is None or handle.turn_id != turn_id:
                raise ValueError("interrupt request does not match the active turn")
            if turn_id in self._interrupt_requested:
                return
            if handle.done or handle.state in TERMINAL_TURN_STATES:
                raise RuntimeError("cannot interrupt a terminal turn")
            self._interrupt_requested.add(turn_id)
            committed = turn_id in self._start_committed
            if not committed:
                self._accept_interrupt(handle)
            else:
                self._cancel_inflight.add(turn_id)
        if not committed:
            return
        accepted = False
        retryable = False
        try:
            accepted = bool(self._backend.cancel())
        except Exception:
            retryable = True
            raise
        finally:
            self._settle_interrupt(handle, accepted, retryable=retryable)

    def _settle_interrupt(self, handle: TurnHandle, accepted: bool, *,
                          retryable: bool = False) -> None:
        with self._cancel_condition:
            self._cancel_inflight.discard(handle.turn_id)
            try:
                if retryable:
                    self._interrupt_requested.discard(handle.turn_id)
                if handle.done or handle.state in TERMINAL_TURN_STATES:
                    return
                if accepted:
                    self._accept_interrupt(handle)
                else:
                    self._publish_for(handle, "runtime.warning", {
                        "stream": "cancel",
                        "text": "cancel requested but backend did not accept it",
                    })
            finally:
                self._cancel_condition.notify_all()

    def _accept_interrupt(self, handle: TurnHandle) -> None:
        self._cancel_accepted.add(handle.turn_id)
        handle.state = TurnState.INTERRUPTING
        self._publish_for(handle, "turn.interrupting", {"state": handle.state})

    def resolve_approval(self, decision: ApprovalDecision) -> None:
        raise UnsupportedOperation("resolve_approval", "typed_approvals")

    def events(self) -> Iterator[RuntimeEvent]:
        return _RuntimeEventStream(self._queue)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._queue.close()

    def _backend_emit(self, kind: str, text: str) -> None:
        with self._lock:
            handle = self._active
            if (handle is None or handle.done or self._closed
                    or handle.state in TERMINAL_TURN_STATES):
                return
            normalized = str(kind).lower()
            if normalized == "stdout":
                self._stream_seen["stdout"] = True
                event_kind, payload = "message.delta", {"text": str(text)}
            elif normalized in {"stderr", "log"}:
                if normalized == "stderr":
                    self._stream_seen["stderr"] = True
                event_kind = "runtime.warning"
                payload = {"stream": normalized, "text": str(text)}
            else:
                return
            self._publish_for(handle, event_kind, payload)

    def _publish_for(self, handle: TurnHandle, kind: str,
                     payload: dict[str, Any]) -> None:
        session = self._session
        if session is None:
            raise RuntimeError("runtime session is unavailable")
        self._publish(kind, session=session, run_id=handle.run_id,
                      turn_id=handle.turn_id, actor_id=handle.actor_id,
                      payload=payload)

    def _safe_publish_for(self, handle: TurnHandle, kind: str,
                          payload: dict[str, Any]) -> None:
        try:
            self._publish_for(handle, kind, payload)
        except (OverflowError, RuntimeError, ValueError):
            pass

    def _publish(self, kind: str, *, session: RuntimeSession, run_id: str,
                 turn_id: str, payload: dict[str, Any],
                 actor_id: str = "agent_main", reserved: bool = False) -> None:
        with self._lock:
            if (run_id not in self._seq_by_run
                    and len(self._seq_by_run) >= MAX_RUN_SEQUENCES):
                raise OverflowError("legacy runtime run sequence index is full")
            seq = self._seq_by_run.get(run_id, 0) + 1
            self._seq_by_run[run_id] = seq
            extensions = {"runtime": LEGACY_RUNTIME_NAME}
            if (kind == "workspace.snapshot" and payload.get("phase") == "after"
                    and isinstance(payload.get("last_turn_workspace"), dict)):
                extensions["authoritative"] = True
            event = RuntimeEvent(
                event_id=_id("evt"), seq=seq, occurred_at=_now(),
                workspace_id=session.workspace_id, session_id=session.session_id,
                run_id=run_id, turn_id=turn_id, actor_id=actor_id,
                kind=kind, payload=payload, extensions=extensions,
            )
            if reserved:
                self._queue.put_reserved(event)
            else:
                self._queue.put(event)

    def _workspace_id(self) -> str:
        try:
            status = self._workspace_status()
        except (TypeError, ValueError, OSError):
            status = {}
        root = str(status.get("root", "") or getattr(self._backend, "cwd", ""))
        digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:16]
        return f"ws_{digest}"

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("legacy runtime is closed")


def _request_value(request: Any, name: str, default: Any) -> Any:
    if isinstance(request, Mapping):
        return request.get(name, default)
    return getattr(request, name, default)


def _value(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _finite_number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _changed_paths(before: Mapping[str, Any], after: Mapping[str, Any],
                   diff: Mapping[str, Any]) -> list[str]:
    old = _file_statuses(before)
    new = _file_statuses(after)
    paths = {path for path in old.keys() | new.keys()
             if old.get(path) != new.get(path)}
    for item in diff.get("files", []):
        path = item.get("path", "") if isinstance(item, Mapping) else item
        if path:
            paths.add(str(path))
    for line in str(diff.get("patch", "")).splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            paths.add(line[6:])
    return sorted(path for path in paths if path and path != "/dev/null")


def _file_statuses(snapshot: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in snapshot.get("files", []):
        if isinstance(item, Mapping) and item.get("path"):
            result[str(item["path"])] = str(item.get("status", ""))
    return result


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
