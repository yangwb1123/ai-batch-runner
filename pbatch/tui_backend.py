"""Single-turn conversation backend for an optional terminal UI."""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, runner, tui_backend_structured
from .lock import LOCK_EXIT_CODE, acquire_lock, release_lock
from .memory import new_session_id, record_task
from .models import Task, TaskResult
from .tui_workspace import capture_workspace, workspace_delta, workspace_diff


Emit = Callable[[str, str], None]
_TURN_LOCK = threading.Lock()


class _CallbackSink:
    """Minimal thread-safe text sink used by runner drain threads."""

    def __init__(self, kind: str, emit: Emit, lock: threading.Lock):
        self._kind = kind
        self._emit = emit
        self._lock = lock

    def write(self, value: str) -> int:
        text = str(value)
        if not text:
            return 0
        with self._lock:
            try:
                self._emit(self._kind, text)
            except Exception:
                pass  # presentation failures must never block pipe draining
        return len(text)

    def flush(self) -> None:
        return None


class _CallbackLogHandler(logging.Handler):
    """Route pi-batch operational logs through the UI callback."""

    def __init__(self, sink: _CallbackSink):
        super().__init__()
        self._sink = sink
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        self._sink.write(self.format(record) + "\n")


class TUIBackend:
    """Run one conversational agent turn at a time on a stable session."""

    def __init__(self, emit: Optional[Emit] = None, *, cwd: str = "",
                 agent: str = "", model: str = "", provider: str = "",
                 memory_mode: str = "", timeout: int = 0,
                 session_id: str = "", session_name: str = "tui"):
        self.emit = emit or (lambda _kind, _text: None)
        if not callable(self.emit):
            raise TypeError("emit must be callable")
        self.cwd = str(Path(cwd or os.getcwd()).resolve())
        self.agent = str(agent or config.AGENT_BIN)
        self.model = str(model or config.AGENT_DEFAULT_MODEL)
        self.provider = str(provider)
        self.memory_mode = str(memory_mode or config.MEMORY_MODE).lower()
        self.timeout = int(timeout or config.AGENT_DEFAULT_TIMEOUT)
        self._validate_settings()
        self.session_name = str(session_name or "tui")
        self.session_id = str(session_id or self._make_session_id())
        self._state_lock = threading.Lock()
        self._emit_lock = threading.RLock()
        self._cancel_requested = threading.Event()
        self._turn_done = threading.Event()
        self._turn_done.set()
        self._turn_procs: tuple = ()
        self._turn_interrupt: Optional[Callable[[], None]] = None
        self._running = False
        self._session_started = False
        self._turn = 0
        self._status = "idle"
        self._last_result: Optional[TaskResult] = None
        self._preflight_agent = ""
        self._preflight_errors: list[str] = []
        self._workspace = capture_workspace(self.cwd)
        self._turn_workspace_before = self._workspace
        self._last_workspace_delta: dict = {
            "changed": False, "paths": [], "head_changed": False,
        }
        self._configure_sinks()

    def set_emit(self, emit: Emit) -> None:
        """Attach a presentation callback before a turn starts."""
        if not callable(emit):
            raise TypeError("emit must be callable")
        with self._state_lock:
            if self._running:
                raise RuntimeError("cannot replace emit callback during a turn")
            self.emit = emit
            self._configure_sinks()

    def _configure_sinks(self) -> None:
        self._stdout = _CallbackSink("stdout", self.emit, self._emit_lock)
        self._stderr = _CallbackSink("stderr", self.emit, self._emit_lock)
        self._log_handler = _CallbackLogHandler(
            _CallbackSink("log", self.emit, self._emit_lock))

    def _validate_settings(self) -> None:
        if not self.agent.strip():
            raise ValueError("agent must not be empty")
        if self.memory_mode not in ("auto", "on", "off"):
            raise ValueError("memory_mode must be auto, on, or off")
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    def _make_session_id(self) -> str:
        return new_session_id(self.session_name, "", self.cwd)

    def snapshot(self) -> dict:
        """Return a bounded, JSON-friendly view of the current turn state."""
        agent_errors = self._agent_errors()
        with self._state_lock:
            result = self._last_result
            return {
                "status": self._status, "running": self._running,
                "cancel_requested": self._cancel_requested.is_set(),
                "session_id": self.session_id,
                "session_name": self.session_name, "turn": self._turn,
                "agent": self.agent, "model": self.model,
                "provider": self.provider, "memory_mode": self.memory_mode,
                "agent_ready": not agent_errors,
                "agent_error": "; ".join(agent_errors),
                "timeout": self.timeout, "cwd": self.cwd,
                "last_result": self._result_snapshot(result),
                "workspace": self._workspace.summary(),
                "last_turn_workspace": dict(self._last_workspace_delta),
            }

    def workspace_status(self, refresh: bool = True) -> dict:
        """Return bounded file-level Git status for the review UI."""
        if refresh:
            self._workspace = capture_workspace(self.cwd)
        return self._workspace.details()

    def workspace_diff(self, path: str = "") -> dict:
        """Return a bounded, read-only unified diff for the workspace."""
        result = workspace_diff(self.cwd, path)
        self._workspace = capture_workspace(self.cwd)
        return result

    def _agent_errors(self) -> list[str]:
        """Cache executable/adapter checks until the selected agent changes."""
        if self.agent != self._preflight_agent:
            self._preflight_agent = self.agent
            self._preflight_errors = config.agent_preflight_errors(self.agent)
        return list(self._preflight_errors)

    @staticmethod
    def _result_snapshot(result: Optional[TaskResult]) -> Optional[dict]:
        if result is None:
            return None
        return {
            "success": result.success, "returncode": result.returncode,
            "reason": result.reason, "elapsed": result.elapsed,
            "session_id": result.session_id,
        }

    def new_session(self, name: str = "", session_id: str = "") -> dict:
        """Reset conversation state; a running turn cannot be abandoned."""
        with self._state_lock:
            if self._running:
                raise RuntimeError("cannot replace a running session")
            if name:
                self.session_name = str(name)
            self.session_id = str(session_id or self._make_session_id())
            self._session_started = False
            self._turn = 0
            self._status = "idle"
            self._last_result = None
            self._cancel_requested.clear()
            self._last_workspace_delta = {
                "changed": False, "paths": [], "head_changed": False,
            }
        self._notify("status", "new_session")
        return self.snapshot()

    def run_turn(self, prompt: str) -> TaskResult:
        """Synchronously execute a turn; UI callers should use a worker."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must not be empty")
        if not _TURN_LOCK.acquire(blocking=False):
            raise RuntimeError("another TUI turn is already running")
        try:
            turn, flags_kind = self._begin_turn()
            task = self._task(prompt)
            result = self._execute(task, turn, flags_kind)
            return self._finish_turn(result)
        finally:
            self._end_turn()
            _TURN_LOCK.release()

    def run_turn_structured(self, prompt: str, *,
                            adapter_factory: Optional[Callable[[str], Any]] = None
                            ) -> TaskResult:
        """Execute one turn through a typed event adapter."""
        return tui_backend_structured.run_turn_structured(
            self, prompt, adapter_factory=adapter_factory, turn_lock=_TURN_LOCK,
            begin_turn=self._begin_turn, make_task=self._task,
            runtime_config=self._runtime_config, finish_turn=self._finish_turn,
            record_result=self._record_result, end_turn=self._end_turn,
            install_interrupt=self._set_turn_interrupt, notify=self._notify,
            is_cancelled=self._cancel_requested.is_set)

    def _begin_turn(self) -> tuple[int, str]:
        workspace_before = capture_workspace(self.cwd)
        turn_procs = runner.proc_snapshot()
        with self._state_lock:
            if self._running:
                raise RuntimeError("a turn is already running")
            self._running = True
            self._status = "running"
            self._cancel_requested.clear()
            self._turn_done.clear()
            turn = self._turn + 1
            flags_kind = "continue" if self._session_started else "start"
            self._session_started = True
            self._turn_workspace_before = workspace_before
            self._workspace = workspace_before
            self._turn_procs = turn_procs
        self._notify("status", "running")
        return turn, flags_kind

    def _task(self, prompt: str) -> Task:
        return Task(prompt=prompt, model=self.model, provider=self.provider,
                    cwd=self.cwd, timeout=self.timeout)

    def _execute(self, task: Task, turn: int, flags_kind: str) -> TaskResult:
        try:
            with self._runtime_config():
                return self._execute_locked(task, turn, flags_kind)
        except Exception as exc:
            return TaskResult(task, False, stderr=str(exc), reason=str(exc))

    def _execute_locked(self, task: Task, turn: int,
                        flags_kind: str) -> TaskResult:
        lock_path = None
        try:
            lock_path = acquire_lock(self.cwd,
                                       session_name=self.session_name or self.session_id)
        except SystemExit as exc:
            reason = f"workspace is locked (exit {exc.code or LOCK_EXIT_CODE})"
            return TaskResult(task, False, returncode=LOCK_EXIT_CODE,
                              stderr=reason, reason=reason)
        try:
            result = self._invoke_runner(task, turn, flags_kind)
            self._record_result(result)
            return result
        finally:
            release_lock(lock_path)

    def _invoke_runner(self, task: Task, turn: int,
                       flags_kind: str) -> TaskResult:
        flags = config._session_flags(
            flags_kind, self.session_id, self.session_name)
        if self._cancel_requested.is_set():
            return self._cancelled_result(task)
        return runner.run_task(
            task, task_index=turn, total=1, session_flags=flags,
            session_name=self.session_name,
            stdout_target=self._stdout, stderr_target=self._stderr)

    @staticmethod
    def _record_result(result: TaskResult) -> None:
        try:
            record_task(result)
        except (OSError, TypeError, ValueError) as exc:
            config.log.warning("TUI memory record skipped: %s", exc)

    @contextmanager
    def _runtime_config(self):
        previous = (config.AGENT_BIN, config.MEMORY_MODE,
                    config.STREAM_OUTPUT)
        previous_logging = (list(config.log.handlers), config.log.propagate,
                            config.log.level)
        config.AGENT_BIN = self.agent
        config.MEMORY_MODE = self.memory_mode
        config.STREAM_OUTPUT = "full"
        config.log.handlers = [self._log_handler]
        config.log.propagate = False
        config.log.setLevel(logging.INFO)
        try:
            yield
        finally:
            (config.AGENT_BIN, config.MEMORY_MODE,
             config.STREAM_OUTPUT) = previous
            handlers, propagate, level = previous_logging
            config.log.handlers, config.log.propagate = handlers, propagate
            config.log.setLevel(level)

    @staticmethod
    def _cancelled_result(task: Task) -> TaskResult:
        return TaskResult(task, False, returncode=-1, reason="cancelled")

    def _finish_turn(self, result: TaskResult) -> TaskResult:
        cancelled = self._cancel_requested.is_set()
        if cancelled:
            result.success = False
            result.reason = "cancelled"
        status = "cancelled" if cancelled else (
            "succeeded" if result.success else "failed")
        workspace_after = capture_workspace(self.cwd)
        delta = workspace_delta(self._turn_workspace_before, workspace_after)
        with self._state_lock:
            self._turn += 1
            self._running = False
            self._status = status
            self._last_result = result
            self._workspace = workspace_after
            self._last_workspace_delta = delta
        self._notify("status", status)
        return result

    def cancel(self) -> bool:
        """Request cancellation of processes registered during this turn."""
        with self._state_lock:
            if not self._running or self._cancel_requested.is_set():
                return False
            self._cancel_requested.set()
            self._status = "cancelling"
            before = self._turn_procs
            active = runner.proc_snapshot()
            interrupt = self._turn_interrupt
        self._notify("status", "cancelling")
        if interrupt is not None:
            interrupt()
        for proc in active:
            if proc not in before:
                runner.kill_proc_group(proc)
        return True

    def _set_turn_interrupt(self, callback: Optional[Callable[[], None]]) -> None:
        with self._state_lock:
            self._turn_interrupt = callback

    def _end_turn(self) -> None:
        with self._state_lock:
            self._turn_procs = ()
            self._turn_interrupt = None
        self._turn_done.set()

    def _notify(self, kind: str, text: str) -> None:
        with self._emit_lock:
            try:
                self.emit(kind, text)
            except Exception:
                pass


TurnBackend = TUIBackend
