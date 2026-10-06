"""Actual CLI spawn/finish observations and parent links; no provider-call guesses."""
from __future__ import annotations

import atexit
import json
import queue
import threading
import time

from . import config, metering
from .task_center_io import display
from .task_lifecycle import parent_fields, stamp
from .task_duration import work_fingerprint
from .task_state_io import append_bounded, encode

_STARTS = {}
_LOCK = threading.Lock()
MAX_STARTS = 4096
_PENDING = queue.Queue(maxsize=512)
_WORKER = None
_WORKER_LOCK = threading.Lock()


def started(task, task_id: str, attempt: int, workdir: str, session_id: str,
            session_name: str, pid: int) -> None:
    if not config.AGENT_LEDGER_ENABLED:
        return
    now = time.time()
    with _LOCK:
        if len(_STARTS) < MAX_STARTS:
            _STARTS[(metering.RUN_ID, task_id, attempt)] = now
    value = _base(task, task_id, attempt, session_id, session_name)
    value.update(status="running", event="task_start", started_at=now,
                 agent_pid=pid if type(pid) is int and pid > 0 else None, spawn_observed=True)
    _emit(workdir, value)


def completed(result, task, workdir: str, phase: str) -> None:
    if not config.AGENT_LEDGER_ENABLED:
        return
    key = (result.run_id, result.task_id, result.attempt)
    with _LOCK:
        beginning = _STARTS.pop(key, None) if phase == "execution" else None
    value = _base(task, result.task_id, result.attempt,
                  result.session_id, result.session_name)
    value.update(run_id=result.run_id, phase=phase, started_at=beginning,
                 status="succeeded" if result.success else "failed", reason=display(result.reason, 500),
                 usage=result.usage if phase == "execution" else None,
                 elapsed=result.elapsed if phase == "execution" else None,
                 spawn_observed=beginning is not None if phase == "execution" else None)
    _emit(workdir, value)


def _base(task, task_id: str, attempt: int, session_id: str, session_name: str) -> dict:
    return {"schema_version": 1, "ts": stamp(), "run_id": metering.RUN_ID,
            "task_id": task_id, "attempt": attempt, "phase": "execution",
            "task_brief": display(task.prompt, 240), "stage": str(task.memory.get("stage", "")),
            "role": str(task.memory.get("role", "")),
            "session_id": session_id, "session_name": session_name,
            "agent": task.effective_agent(), "provider": task.provider,
            "model": task.model, "thinking": task.thinking,
            "outputs": [task.output] if task.output else [],
            "work_fingerprint": work_fingerprint(task), **parent_fields(task)}


def _emit(workdir: str, value: dict) -> None:
    # Freeze before queueing; never perform filesystem I/O inside the agent's
    # hard deadline or let later result mutation change a recorded observation.
    try:
        item = (config.data_dir(workdir) / "tasks" / "attempts.jsonl", encode(value))
        _ensure_worker()
        _PENDING.put_nowait(item)
    except Exception as exc:
        config.log.warning("Task attempt observation unavailable: %s", display(str(exc)))


def _ensure_worker() -> None:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_drain, daemon=True, name="task-observation")
            _WORKER.start()


def _drain() -> None:
    while True:
        path, payload = _PENDING.get()
        try:
            append_bounded(path, json.loads(payload))
        except Exception as exc:
            config.log.warning("Task attempt observation write lost: %s", display(str(exc)))
        finally:
            _PENDING.task_done()


def flush(timeout: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    with _PENDING.all_tasks_done:
        while _PENDING.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _PENDING.all_tasks_done.wait(remaining)
    return True


atexit.register(flush)
