"""把 Codex ``exec --json`` JSONL 映射为统一 TUI Runtime 事件。
未知 provider item 发出有界占位；正文不提升为审批、Gate、Review 或命令事件。
"""
from __future__ import annotations

from collections import deque
import json
import os
import queue
import threading
import time
from typing import Any, Callable, Iterator, Mapping, Sequence

from .tui_codex_support import (
    CODEX_EXEC_RUNTIME_NAME,
    _bound,
    _bounded_id,
    _bounded_changes,
    _bounded_list,
    _default_spawn,
    _id,
    _now,
    _number,
    _option_tail_index,
    _request_value,
    _runtime_info,
    _stop_process,
    _text,
    _workspace_id,
)
from .tui_protocol import RuntimeEvent, TurnState
from .tui_command_artifacts import (
    CommandArtifactContext,
    CommandArtifactStore,
    CommandArtifactOutcome,
)
from .tui_runtime import (
    RuntimeInfo,
    RuntimeSession,
    StartTurnRequest,
    TurnHandle,
    UnsupportedOperation,
)

DEFAULT_ARGV_PREFIX = ("codex", "exec", "--approve-for-me", "--json")
# Order matters: resume/fork insert before the TAIL options (the first
# "-" token after "exec" stays in the head). `codex exec resume` rejects
# a trailing --approve-for-me but accepts a trailing --json, so approve
# must be in the head and --json in the tail (T5 measured contract).
DEFAULT_QUEUE_SIZE = 256
_IGNORED_STDERR = "Reading additional input from stdin..."
_ITEM_TYPES = frozenset({"agent_message", "command_execution", "file_change", "todo_list"})
Spawn = Callable[[Sequence[str], str], Any]


class _EventQueue:
    """有界可关闭队列；压力下只牺牲 warning，重要事件使用背压。"""
    def __init__(self, maxsize: int):
        if isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize <= 0:
            raise ValueError("maxsize must be a positive integer")
        self.maxsize = maxsize
        self._items: deque[RuntimeEvent] = deque()
        self._closed = False
        self._condition = threading.Condition()
    def put(self, event: RuntimeEvent) -> tuple[bool, bool]:
        with self._condition:
            dropped = False
            while len(self._items) >= self.maxsize and not self._closed:
                warning = next((index for index, queued in enumerate(self._items)
                                if queued.kind == "runtime.warning"), None)
                if warning is not None:
                    del self._items[warning]
                    dropped = True
                    break
                if event.kind == "runtime.warning":
                    return False, True
                self._condition.wait()
            if self._closed:
                return False, dropped
            self._items.append(event)
            self._condition.notify_all()
            return True, dropped
    def get(self, timeout: float | None = None) -> RuntimeEvent | None:
        with self._condition:
            deadline = None if timeout is None else time.monotonic() + max(0, timeout)
            while not self._items and not self._closed:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            if not self._items:
                return None
            event = self._items.popleft()
            self._condition.notify_all()
            return event
    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
class CodexExecAdapter:
    """执行一次 Codex JSONL turn，并投影为 provider-neutral 事件。"""
    def __init__(self, cwd: str | os.PathLike[str], *,
                 argv_prefix: Sequence[str] = DEFAULT_ARGV_PREFIX,
                 maxsize: int = DEFAULT_QUEUE_SIZE, spawn: Spawn | None = None):
        prefix = tuple(str(part) for part in argv_prefix)
        if not prefix or any(not part for part in prefix):
            raise ValueError("argv_prefix must contain non-empty arguments")
        self._cwd, self._argv_prefix = os.fspath(cwd), prefix
        self._spawn, self._queue = spawn or _default_spawn, _EventQueue(maxsize)
        self._lock, self._publish_lock, self._closed = threading.RLock(), threading.RLock(), False
        self._initialized = self._session_announced = False
        self._overflow_reported = self._terminal = self._interrupt_requested = False
        self._session_ready = threading.Event()
        self._session = RuntimeSession(
            session_id=_id("ses"), provider_session_id="",
            workspace_id=_workspace_id(self._cwd), started_at=_now(), state="ACTIVE")
        self._provider_session_id = ""
        self._session_mode = self._turn_mode = "normal"
        self._active, self._process = None, None
        self._threads: list[threading.Thread] = []
        self._seq_by_run: dict[str, int] = {}
        self._usage: dict[str, int | float] = {}
        self._protocol_errors = 0
        self._started_monotonic = 0.0
        self._info = _runtime_info()
        self._command_artifacts = CommandArtifactStore(self._cwd)
    @property
    def session(self) -> RuntimeSession:
        return self._session
    def runtime_info(self) -> RuntimeInfo:
        """Return immutable local capabilities without starting provider I/O."""
        return self._info
    def initialize(self, request: Any = None) -> RuntimeInfo:
        self._ensure_open()
        workspace_id = str(_request_value(request, "workspace_id", "") or "")
        if workspace_id:
            self._session.workspace_id = workspace_id
        self._initialized = True
        return self._info
    def start_session(self, request: Any) -> RuntimeSession:
        self._ensure_session_replaceable()
        session = RuntimeSession(
            session_id=str(_request_value(request, "session_id", "") or _id("ses")),
            provider_session_id="",
            workspace_id=str(_request_value(request, "workspace_id", "") or
                             self._session.workspace_id),
            started_at=_now(), state="ACTIVE")
        with self._lock:
            self._session = session
            self._provider_session_id = ""
            self._session_mode = self._turn_mode = "normal"
            self._reset_session_announcement()
        return session
    def resume_session(self, request: Any) -> RuntimeSession:
        return self._continue_session(request, "resumed")
    def read_session(self, request: Any):
        raise UnsupportedOperation("read_session", "session_read")
    def list_sessions(self, request: Any = None):
        raise UnsupportedOperation("list_sessions", "session_list")
    def fork_session(self, request: Any) -> RuntimeSession:
        return self._continue_session(request, "forked")
    def _continue_session(self, request: Any, mode: str) -> RuntimeSession:
        self._ensure_session_replaceable()
        provider_id = str(_request_value(request, "provider_session_id", "") or
                          self._provider_session_id)
        if not provider_id:
            raise ValueError(f"{mode} session requires provider_session_id")
        workspace_id = str(_request_value(request, "workspace_id", "") or "")
        session_id = str(_request_value(request, "session_id", "") or "")
        with self._lock:
            self._provider_session_id = provider_id
            self._session.provider_session_id = provider_id
            self._session_mode = mode
            if workspace_id:
                self._session.workspace_id = workspace_id
            # A freshly constructed adapter owns a random session id; the
            # coordinator's session id must win so start_turn's session
            # check accepts the turn (resume via a NEW adapter instance —
            # the TUI multi-turn path).
            if session_id:
                self._session.session_id = session_id
            self._reset_session_announcement()
        return self._session
    def _ensure_session_replaceable(self) -> None:
        self._ensure_open()
        with self._lock:
            if self._active is not None and not self._active.done:
                raise RuntimeError("cannot replace an active runtime session")
    def _reset_session_announcement(self) -> None:
        self._session_announced = False
        self._session_ready.clear()
    def steer_turn(self, request: Any) -> None:
        raise UnsupportedOperation("steer_turn", "turn_steer")
    def resolve_approval(self, decision: Any) -> None:
        raise UnsupportedOperation("resolve_approval", "typed_approvals")
    def start_turn(self, request: StartTurnRequest | str | Mapping[str, Any], *,
                   argv: Sequence[str] | None = None) -> TurnHandle:
        self._ensure_open()
        if not self._initialized:
            raise RuntimeError("initialize must be called before start_turn")
        default = request if isinstance(request, str) else ""
        prompt = str(_request_value(request, "prompt", default))
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        handle = self._new_handle(request)
        with self._lock:
            mode = self._session_mode
            self._turn_mode = mode
        command = self._spawn_argv(prompt, mode, argv)
        process = self._spawn(command, self._cwd)
        with self._lock:
            self._active, self._process = handle, process
            self._terminal = self._interrupt_requested = self._overflow_reported = False
            self._usage, self._protocol_errors, self._started_monotonic = {}, 0, time.monotonic()
        self._start_workers(handle, process)
        return handle
    def _spawn_argv(self, prompt: str, mode: str,
                    argv: Sequence[str] | None = None) -> tuple[str, ...]:
        base = tuple(str(part) for part in (self._argv_prefix if argv is None else argv))
        if mode == "normal":
            return base + (prompt,)
        subcommand = "resume" if mode == "resumed" else "fork"
        tail_index = _option_tail_index(base)
        return (base[:tail_index] +
                (subcommand, self._provider_session_id, prompt) +
                base[tail_index:])
    def _new_handle(self, request: Any) -> TurnHandle:
        with self._lock:
            if self._active is not None and not self._active.done:
                raise RuntimeError("a codex exec turn is already running")
            requested = str(_request_value(request, "session_id", "") or "")
            if requested and requested != self._session.session_id:
                raise ValueError("turn request does not belong to the active session")
        return TurnHandle(
            session_id=self._session.session_id,
            run_id=str(_request_value(request, "run_id", "") or _id("run")),
            turn_id=str(_request_value(request, "turn_id", "") or _id("turn")),
            actor_id=str(_request_value(request, "actor_id", "agent_main") or
                         "agent_main"), started_at=_now())
    def _start_workers(self, handle: TurnHandle, process: Any) -> None:
        stdout = threading.Thread(target=self._read_stdout, args=(handle, process),
                                  name=f"codex-out-{handle.turn_id}", daemon=True)
        stderr = threading.Thread(target=self._read_stderr, args=(handle, process),
                                  name=f"codex-err-{handle.turn_id}", daemon=True)
        monitor = threading.Thread(
            target=self._monitor, args=(handle, process, stdout, stderr),
            name=f"codex-wait-{handle.turn_id}", daemon=True)
        self._threads = [stdout, stderr, monitor]
        stdout.start()
        stderr.start()
        monitor.start()
    def _read_stdout(self, handle: TurnHandle, process: Any) -> None:
        for source_line in getattr(process, "stdout", ()) or ():
            raw = str(source_line).rstrip("\r\n")
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                self._warn_protocol(handle, raw)
                continue
            if not isinstance(value, Mapping):
                self._warn_protocol(handle, raw)
                continue
            self._dispatch(handle, dict(value), raw)
    def _warn_protocol(self, handle: TurnHandle, raw: str) -> None:
        self._protocol_errors += 1
        self._ensure_session_event(handle)
        self._start_provider_turn(handle)
        self._publish(handle, "runtime.warning", {
            "stream": "stdout", "category": "protocol_error",
            "protocol_error_count": self._protocol_errors, **_text("raw", raw)})
    def _read_stderr(self, handle: TurnHandle, process: Any) -> None:
        for source_line in getattr(process, "stderr", ()) or ():
            raw = str(source_line).strip()
            if not raw or raw == _IGNORED_STDERR:
                continue
            self._session_ready.wait(0.05)
            self._ensure_session_event(handle)
            self._start_provider_turn(handle)
            self._publish(handle, "runtime.warning",
                          {"stream": "stderr", **_text("text", raw)})
    def _dispatch(self, handle: TurnHandle, value: dict[str, Any], raw: str) -> None:
        provider_type = str(value.get("type", ""))
        if provider_type == "thread.started":
            provider_id = str(value.get("thread_id", "") or "")
            with self._lock:
                self._provider_session_id = provider_id
                self._session.provider_session_id = provider_id
                if self._turn_mode == "forked":
                    self._session_mode = "resumed"
            self._ensure_session_event(handle)
            return
        self._ensure_session_event(handle)
        if self._is_terminal(handle):
            return
        if provider_type == "turn.started":
            self._start_provider_turn(handle)
        else:
            self._start_provider_turn(handle)
            if provider_type in {"item.started", "item.completed", "item.updated"}:
                self._dispatch_item(handle, provider_type, value.get("item"), raw)
            elif provider_type == "turn.completed":
                self._complete_provider_turn(handle, value.get("usage"))
                return
            elif provider_type == "turn.failed":
                self._finish(handle, "turn.failed", TurnState.FAILED,
                             {**_text("reason", value.get("error", ""))})
                return
            else:
                self._unknown(handle, provider_type, raw)
        if "usage" in value:
            self._emit_usage(handle, value.get("usage"))
    def _start_provider_turn(self, handle: TurnHandle) -> None:
        with self._publish_lock:
            with self._lock:
                if self._terminal or handle.state != TurnState.QUEUED:
                    return
            self._publish(handle, "turn.queued", {
                "turn_id": handle.turn_id, "state": TurnState.QUEUED})
            with self._lock:
                if self._terminal or handle.state != TurnState.QUEUED:
                    return
                handle.state = TurnState.RUNNING
            self._publish(handle, "turn.started", {
                "turn_id": handle.turn_id, "state": TurnState.RUNNING})
    def _dispatch_item(self, handle: TurnHandle, provider_type: str,
                       item_value: Any, raw: str) -> None:
        if not isinstance(item_value, Mapping):
            self._unknown(handle, provider_type, raw)
            return
        item, phase = dict(item_value), provider_type.removeprefix("item.")
        item_type = str(item.get("type", ""))
        if item_type not in _ITEM_TYPES:
            self._unknown(handle, item_type or provider_type, raw)
        elif item_type == "agent_message":
            self._message_item(handle, phase, item)
        elif item_type == "command_execution":
            self._command_item(handle, phase, item)
        elif item_type == "file_change":
            self._file_item(handle, phase, item)
        else:
            self._plan_item(handle, phase, item)
    def _message_item(self, handle: TurnHandle, phase: str,
                      item: Mapping[str, Any]) -> None:
        item_id = _bounded_id(item.get("id", ""))
        if phase == "started":
            self._publish(handle, "message.started", {"item_id": item_id})
        elif phase == "completed":
            self._publish(handle, "message.completed",
                          {"item_id": item_id, **_text("text", item.get("text", ""))})
        else:
            self._publish(handle, "message.delta",
                          {"item_id": item_id, **_text("text", item.get("text", ""))})
    def _command_item(self, handle: TurnHandle, phase: str,
                      item: Mapping[str, Any]) -> None:
        base = {"item_id": _bounded_id(item.get("id", "")),
                "command": _bound(item.get("command", ""), 24 * 1024)[0],
                "status": _bound(item.get("status", ""), 256)[0]}
        if phase == "started":
            self._publish(handle, "command.started", base)
            return
        output = item.get("aggregated_output", "")
        output_payload = _text("aggregated_output", output)
        self._publish(handle, "command.output", {
            "item_id": base["item_id"],
            **output_payload})
        if output_payload.get("truncated") is True:
            self._emit_command_artifact(
                handle, base["item_id"], base["command"], output)
        exit_code = item.get("exit_code")
        safe_code = exit_code if isinstance(exit_code, int) and not isinstance(
            exit_code, bool) else None
        self._publish(handle, "command.completed", {**base, "exit_code": safe_code})

    def _emit_command_artifact(
            self, handle: TurnHandle, item_id: str,
            command: str, output: Any) -> None:
        try:
            context = CommandArtifactContext(
                self._session.workspace_id, self._session.session_id,
                handle.run_id, handle.turn_id, item_id)
            outcome = self._command_artifacts.capture(context, output)
        except (OSError, RuntimeError, ValueError) as exc:
            outcome = CommandArtifactOutcome(warning=str(exc))
        if outcome.artifact is not None:
            name = f"Command output · {_bound(command, 160)[0]}"
            self._publish(handle, "artifact.created",
                          outcome.artifact.event_payload(item_id, name))
        elif outcome.warning:
            self._publish(handle, "runtime.warning", {
                "category": "artifact_store", "item_id": item_id,
                "reason": _bound(outcome.warning, 2048)[0]})
    def _file_item(self, handle: TurnHandle, phase: str,
                   item: Mapping[str, Any]) -> None:
        changes, truncated = _bounded_changes(item.get("changes"))
        self._publish(handle, "file.changed", {
            "item_id": _bounded_id(item.get("id", "")),
            "status": _bound(item.get("status", phase), 256)[0],
            "changes": changes, "truncated": truncated})
    def _plan_item(self, handle: TurnHandle, phase: str,
                   item: Mapping[str, Any]) -> None:
        items, truncated = _bounded_list(item.get("items", item.get("todos", [])))
        self._publish(handle, "plan.updated", {
            "item_id": _bounded_id(item.get("id", "")),
            "status": _bound(item.get("status", phase), 256)[0],
            "items": items, "truncated": truncated})
    def _emit_usage(self, handle: TurnHandle, usage: Any) -> None:
        if isinstance(usage, Mapping):
            for name in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens"):
                if _number(value := usage.get(name)):
                    self._usage[name] = value
        self._publish(handle, "usage.updated", {"usage": dict(self._usage)})
    def _complete_provider_turn(self, handle: TurnHandle, usage: Any) -> None:
        self._emit_usage(handle, usage)
        elapsed_ms = max(0, int((time.monotonic() - self._started_monotonic) * 1000))
        self._finish(handle, "turn.completed", TurnState.SUCCEEDED, {
            "elapsed_ms": elapsed_ms, "usage": dict(self._usage)})
    def _unknown(self, handle: TurnHandle, provider_type: str, raw: str) -> None:
        self._publish(handle, "runtime.unknown_item", {
            "type": _bound(provider_type or "<missing>", 256)[0], **_text("raw", raw)})
    def _ensure_session_event(self, handle: TurnHandle) -> None:
        with self._lock:
            if self._session_announced:
                return
            self._session_announced = True
        payload = {
            "workspace_id": self._session.workspace_id,
            "session_id": self._session.session_id,
            "provider_session_id": _bound(
                self._session.provider_session_id, 2048)[0],
            "state": self._session.state}
        if self._turn_mode != "normal":
            payload["mode"] = self._turn_mode
        self._publish(handle, "session.created", payload)
        self._session_ready.set()
    def _monitor(self, handle: TurnHandle, process: Any, stdout: threading.Thread,
                 stderr: threading.Thread) -> None:
        stdout.join()
        stderr.join()
        try:
            returncode = process.wait()
        except Exception as exc:
            returncode = -1
            self._ensure_session_event(handle)
            self._start_provider_turn(handle)
            self._publish(handle, "runtime.error",
                          {"phase": "wait", **_text("reason", exc)})
        self._ensure_session_event(handle)
        if self._is_terminal(handle):
            return
        self._start_provider_turn(handle)
        if self._interrupt_requested:
            self._finish(handle, "turn.interrupted", TurnState.INTERRUPTED,
                         {"returncode": returncode})
            return
        state = TurnState.FAILED if returncode else TurnState.UNKNOWN
        self._finish(handle, "turn.failed", state, {
            "returncode": returncode,
            "reason": "provider process ended without turn.completed"})
    def _finish(self, handle: TurnHandle, kind: str, state: str,
                payload: dict[str, Any]) -> None:
        with self._lock:
            if self._terminal or handle.done:
                return
            self._terminal, handle.state = True, state
        self._publish(handle, kind,
                      {"state": state, "turn_id": handle.turn_id, **payload})
        handle._done.set()
    def _is_terminal(self, handle: TurnHandle) -> bool:
        with self._lock:
            return self._terminal or handle.done
    def interrupt_turn(self, request: Any) -> None:
        """回收当前进程组后提交不可回退的 interrupted 终态。"""
        self._ensure_open()
        default = request if isinstance(request, str) else ""
        turn_id = str(_request_value(request, "turn_id", default))
        with self._lock:
            handle = self._active
            if handle is None or handle.turn_id != turn_id:
                raise ValueError("interrupt request does not match the active turn")
            if self._interrupt_requested:
                return
            if self._terminal or handle.done:
                raise RuntimeError("cannot interrupt a terminal turn")
            process = self._process
        self._ensure_session_event(handle)
        self._start_provider_turn(handle)
        with self._lock:
            if self._interrupt_requested:
                return
            if self._terminal or handle.done:
                raise RuntimeError("cannot interrupt a terminal turn")
            self._interrupt_requested, handle.state = True, TurnState.INTERRUPTING
        self._publish(handle, "turn.interrupting", {"state": handle.state})
        _stop_process(process)
        self._finish(handle, "turn.interrupted", TurnState.INTERRUPTED, {})
    def events(self) -> Iterator[RuntimeEvent]:
        """阻塞消费事件；close 且队列耗尽后自然 StopIteration。"""
        while True:
            event = self._queue.get()
            if event is None:
                return
            yield event
    def close(self) -> None:
        """幂等回收进程组、读线程与事件队列。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process, handle = self._process, self._active
        _stop_process(process)
        for worker in list(self._threads):
            if worker is not threading.current_thread():
                worker.join(timeout=1.0)
        if handle is not None:
            handle._done.set()
        self._command_artifacts.close()
        self._queue.close()
    def _publish(self, handle: TurnHandle, kind: str,
                 payload: dict[str, Any]) -> None:
        with self._publish_lock:
            with self._lock:
                late_turn = (self._terminal and kind.startswith("turn.") and kind not in {
                    "turn.completed", "turn.failed", "turn.interrupted"})
                if self._closed or late_turn:
                    return
                seq = self._seq_by_run.get(handle.run_id, 0) + 1
                self._seq_by_run[handle.run_id] = seq
            event = RuntimeEvent(
                event_id=_id("evt"), seq=seq, occurred_at=_now(),
                workspace_id=self._session.workspace_id,
                session_id=self._session.session_id, run_id=handle.run_id,
                turn_id="" if kind.startswith("session.") else handle.turn_id,
                actor_id=handle.actor_id, kind=kind,
                payload=payload, extensions={"runtime": CODEX_EXEC_RUNTIME_NAME})
            accepted, dropped = self._queue.put(event)
            if dropped and kind != "runtime.error":
                self._report_overflow(handle)
            if not accepted and kind != "runtime.warning" and not self._closed:
                raise RuntimeError("codex runtime event queue is closed")
    def _report_overflow(self, handle: TurnHandle) -> None:
        with self._lock:
            if self._overflow_reported or self._closed:
                return
            self._overflow_reported = True
        self._publish(handle, "runtime.error", {
            "category": "event_queue_overflow",
            "reason": "oldest runtime.warning was discarded"})
    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("codex exec runtime is closed")
