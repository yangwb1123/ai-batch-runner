"""Bounded, authenticated control sessions for the mobile operations view.

The operations page is intentionally not a generic remote shell.  A control
session is created by the server with its configured workspace and agent; a
client can only select an exact server-issued session id and submit a bounded
prompt to that session.  Historical memory sessions remain read-only.
"""

from __future__ import annotations

import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from . import memory
from .agent_session import AgentSession, AgentSessionOptions, create_agent_session
from .memory_records import redact_content
from .tui_protocol import RuntimeEvent

MAX_SESSIONS = 8
MAX_PROMPT_BYTES = 16 * 1024
MAX_SESSION_NAME_BYTES = 128
MAX_EVENTS = 256
MAX_EVENT_VIEWS = 80
MAX_TURNS = 64
MAX_TURN_TEXT = 64 * 1024
MAX_JOBS = 256
_ID_RE = re.compile(r"^mb-[A-Za-z0-9_-]{8,80}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip(value: Any, maximum: int) -> str:
    text = str(value or "")
    if len(text) <= maximum:
        return text
    return text[: max(0, maximum - 1)] + "…"


def _safe_clip(value: Any, maximum: int) -> str:
    return _clip(redact_content(str(value or "")), maximum)


def _checked_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{label} exceeds {maximum} bytes")
    return value.strip()


def _event_view(event: RuntimeEvent) -> dict[str, Any]:
    """Project one trusted event to a small mobile-safe diagnostic record."""
    payload = redact_content(dict(event.payload))
    kind = str(event.kind)
    if kind in {"message.delta", "message.completed", "command.output"}:
        projected = {"text": _clip(payload.get("text", ""), 2048)}
    else:
        projected = {}
        for key in (
            "state", "status", "reason", "category", "name", "title",
            "exit_code", "success", "stage", "task", "completed_stages",
            "next_stage_index", "verdict",
        ):
            if key in payload:
                value = payload[key]
                if isinstance(value, (str, int, float, bool)) or value is None:
                    projected[key] = _clip(value, 512) if isinstance(value, str) else value
    return {
        "kind": kind,
        "occurred_at": _clip(event.occurred_at, 64),
        "run_id": _clip(event.run_id, 128),
        "turn_id": _clip(event.turn_id, 128),
        "actor_id": _clip(event.actor_id, 128),
        "payload": projected,
    }


def _event_text(event: RuntimeEvent) -> str:
    value = event.payload.get("text", "")
    return redact_content(value) if isinstance(value, str) else ""


def _session_id() -> str:
    return "mb-" + uuid.uuid4().hex


@dataclass
class _Job:
    job_id: str
    session_id: str
    prompt_excerpt: str
    created_at: str
    turn_id: str = ""
    state: str = "queued"
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Managed:
    session_id: str
    name: str
    created_at: str
    session: Optional[AgentSession] = None
    status: str = "starting"
    error: str = ""
    active_job: Optional[_Job] = None
    events: deque = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    turns: deque = field(default_factory=lambda: deque(maxlen=MAX_TURNS))
    active_text: str = ""
    active_turn_id: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class MobileSessionManager:
    """Own bounded, exact-identity sessions exposed by ``ops serve``."""

    def __init__(self, cwd: str, *, agent: str = "", model: str = "",
                 provider: str = "", runtime: str = "auto", timeout: int = 900,
                 memory_mode: str = "auto", max_sessions: int = MAX_SESSIONS,
                 session_factory: Optional[Callable[..., AgentSession]] = None):
        self.cwd = str(cwd)
        self.agent = str(agent)
        self.model = str(model)
        self.provider = str(provider)
        self.runtime = str(runtime or "auto")
        self.timeout = int(timeout)
        self.memory_mode = str(memory_mode or "auto")
        if self.timeout <= 0:
            raise ValueError("control timeout must be positive")
        if isinstance(max_sessions, bool) or not 1 <= int(max_sessions) <= MAX_SESSIONS:
            raise ValueError(f"max_sessions must be between 1 and {MAX_SESSIONS}")
        self.max_sessions = int(max_sessions)
        self._factory = session_factory or create_agent_session
        self._lock = threading.RLock()
        self._sessions: dict[str, _Managed] = {}
        self._jobs: dict[str, _Job] = {}
        self._job_order: deque[str] = deque()
        self._closed = False

    def create(self, name: str = "") -> dict[str, Any]:
        if name == "":
            name = "mobile"
        else:
            name = _checked_text(name, "session name", MAX_SESSION_NAME_BYTES)
        with self._lock:
            if self._closed:
                raise RuntimeError("mobile control plane is closed")
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError("mobile control session limit reached")
            item = _Managed(_session_id(), name, _now())
            self._sessions[item.session_id] = item
        try:
            options = AgentSessionOptions(
                cwd=self.cwd, agent=self.agent, model=self.model,
                provider=self.provider, memory_mode=self.memory_mode,
                timeout=self.timeout, session_name=name,
                session_id=item.session_id, workspace_id=self.cwd,
                runtime=self.runtime)
            session = self._factory(options)
            session.subscribe(lambda event: self._observe(item, event))
        except Exception as exc:
            with item.lock:
                item.status = "error"
                item.error = _safe_clip(exc, 1024)
            raise RuntimeError(f"cannot start control session: {item.error}") from exc
        with item.lock:
            item.session = session
            item.status = "idle"
        return self.detail(item.session_id)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        with self._lock:
            active = [self._summary(item) for item in self._sessions.values()]
        historical = []
        try:
            historical = [_history_summary(row) for row in memory.recent(
                self.cwd, limit=limit) if str(row.get("session_id", ""))]
        except Exception:
            historical = []
        seen = {row["session_id"] for row in active}
        return active + [row for row in historical if row["session_id"] not in seen][:limit]

    def detail(self, session_id: str) -> dict[str, Any]:
        item = self._find(_checked_session_id(session_id))
        if item is None:
            raise KeyError("session not found")
        with item.lock:
            return self._detail_locked(item)

    def history_detail(self, session_id: str, max_bytes: int = 64 * 1024) -> dict[str, Any]:
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256:
            raise ValueError("invalid session id")
        if self._find(session_id) is not None:
            return self.detail(session_id)
        return historical_detail(self.cwd, session_id, max_bytes)

    def start_turn(self, session_id: str, prompt: str) -> dict[str, Any]:
        checked = _checked_session_id(session_id)
        prompt = _checked_text(prompt, "prompt", MAX_PROMPT_BYTES)
        item = self._find(checked)
        if item is None:
            raise KeyError("session is not a managed control session")
        with item.lock:
            if item.status in {"error", "closed"} or item.session is None:
                raise RuntimeError("session is not available for control")
            if item.active_job is not None:
                raise RuntimeError("session already has an active turn")
            job = _Job("job-" + uuid.uuid4().hex, checked,
                       _safe_clip(prompt, 240), _now())
            item.active_job = job
            item.status = "running"
            item.active_text = ""
            item.active_turn_id = ""
            self._remember_job(job)
            session = item.session
        worker = threading.Thread(
            target=self._run_turn, args=(item, job, session, prompt),
            name=f"mobile-turn-{job.job_id[-8:]}", daemon=True)
        worker.start()
        return self._job_view(job)

    def cancel(self, session_id: str) -> dict[str, Any]:
        item = self._find(_checked_session_id(session_id))
        if item is None:
            raise KeyError("session not found")
        with item.lock:
            job = item.active_job
            session = item.session
            if job is None or session is None:
                raise RuntimeError("session has no active turn")
            job.state = "interrupting"
        try:
            session.abort()
        except Exception as exc:
            with item.lock:
                job.state = "running"
            raise RuntimeError(_safe_clip(exc, 1024)) from exc
        return self._job_view(job)

    def job(self, job_id: str) -> dict[str, Any]:
        checked = _checked_id(job_id, "job id", 128)
        with self._lock:
            job = self._jobs.get(checked)
            if job is None:
                raise KeyError("job not found")
        return self._job_view(job)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            items = list(self._sessions.values())
        for item in items:
            with item.lock:
                session = item.session
                item.status = "closed"
            if session is not None:
                try:
                    session.close()
                except Exception:
                    pass

    def _run_turn(self, item: _Managed, job: _Job, session: AgentSession,
                  prompt: str) -> None:
        with item.lock:
            job.state = "running"
        try:
            outcome = session.prompt(prompt, timeout=self.timeout)
            with item.lock:
                if job.state not in {"cancelled", "completed", "failed"}:
                    job.state = "completed" if getattr(outcome, "succeeded", False) else "failed"
                    job.result = {"text": _safe_clip(getattr(outcome, "text", ""), MAX_TURN_TEXT),
                                  "reason": _safe_clip(getattr(outcome, "reason", ""), 1024)}
                if item.active_job is job and job.state in {"completed", "failed"}:
                    self._finish_job_locked(item, job)
        except TimeoutError as exc:
            with item.lock:
                job.result = {"reason": _safe_clip(exc, 1024), "provider_continues": True}
                job.state = "running"
                item.error = "local wait expired; provider turn remains authoritative"
        except Exception as exc:
            with item.lock:
                job.state = "failed"
                job.result = {"reason": _safe_clip(exc, 1024)}
                if item.active_job is job:
                    self._finish_job_locked(item, job)
                item.status = "error"
                item.error = _safe_clip(exc, 1024)

    def _observe(self, item: _Managed, event: RuntimeEvent) -> None:
        view = _event_view(event)
        with item.lock:
            item.events.append(view)
            if event.kind in {"turn.queued", "turn.started"}:
                item.active_turn_id = event.turn_id
                if item.active_job is not None:
                    item.active_job.turn_id = event.turn_id
                    item.active_job.state = "running"
            if event.kind in {"message.delta", "message.completed"}:
                text = _event_text(event)
                if event.kind == "message.completed":
                    item.active_text = _clip(text, MAX_TURN_TEXT)
                elif text:
                    item.active_text = _clip(item.active_text + text, MAX_TURN_TEXT)
            if event.kind in {"turn.completed", "turn.failed", "turn.interrupted"}:
                job = item.active_job
                if job is not None:
                    job.state = {
                        "turn.completed": "completed",
                        "turn.failed": "failed",
                        "turn.interrupted": "cancelled",
                    }[event.kind]
                    job.result = {
                        "text": item.active_text,
                        "reason": _safe_clip(event.payload.get("reason", ""), 1024),
                    }
                    self._finish_job_locked(item, job)

    def _finish_job_locked(self, item: _Managed, job: _Job) -> None:
        if item.active_job is not job:
            return
        job.result.setdefault("text", item.active_text)
        job.result.setdefault("reason", "")
        item.turns.append({
            "job_id": job.job_id, "turn_id": job.turn_id,
            "created_at": job.created_at, "completed_at": _now(),
            "status": job.state, "prompt": job.prompt_excerpt,
            "text": _safe_clip(job.result.get("text", ""), MAX_TURN_TEXT),
            "reason": _safe_clip(job.result.get("reason", ""), 1024),
        })
        item.active_job = None
        item.active_text = ""
        item.active_turn_id = ""
        if item.status != "closed":
            item.status = "idle"

    def _remember_job(self, job: _Job) -> None:
        with self._lock:
            self._jobs[job.job_id] = job
            self._job_order.append(job.job_id)
            while len(self._job_order) > MAX_JOBS:
                old = self._job_order.popleft()
                self._jobs.pop(old, None)

    def _find(self, session_id: str) -> Optional[_Managed]:
        with self._lock:
            return self._sessions.get(session_id)

    @staticmethod
    def _summary(item: _Managed) -> dict[str, Any]:
        with item.lock:
            job = item.active_job
            return {
                "session_id": item.session_id, "name": item.name,
                "created_at": item.created_at, "status": item.status,
                "managed": True, "controllable": item.status == "idle" and job is None,
                "active_job_id": job.job_id if job else "",
                "active_turn_id": job.turn_id if job else "",
                "error": item.error,
            }

    def _detail_locked(self, item: _Managed) -> dict[str, Any]:
        result = self._summary(item)
        result.update({
            "events": list(item.events)[-MAX_EVENT_VIEWS:],
            "turns": list(item.turns)[-MAX_TURNS:],
            "active_text": _clip(item.active_text, 8192),
        })
        return result

    @staticmethod
    def _job_view(job: _Job) -> dict[str, Any]:
        return {
            "job_id": job.job_id, "session_id": job.session_id,
            "turn_id": job.turn_id, "state": job.state,
            "created_at": job.created_at, "prompt": job.prompt_excerpt,
            "result": dict(job.result),
        }


def _checked_id(value: Any, label: str, maximum: int) -> str:
    return _checked_text(value, label, maximum)


def _checked_session_id(value: Any) -> str:
    value = _checked_id(value, "session id", 96)
    if not _ID_RE.fullmatch(value):
        raise ValueError("invalid mobile session id")
    return value


def _history_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": str(row.get("session_id", "")),
        "name": str(row.get("session_name", "")),
        "created_at": str(row.get("ts", "")),
        "status": str(row.get("status", "")),
        "managed": False,
        "controllable": False,
        "prompt": _clip(row.get("prompt_excerpt", ""), 240),
        "message_count": row.get("message_count", 0),
    }


def historical_sessions(cwd: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return only bounded, redacted local history; it is never controllable."""
    bounded = max(1, min(int(limit), 100))
    try:
        return [_history_summary(row) for row in memory.recent(cwd, bounded)
                if str(row.get("session_id", ""))]
    except Exception:
        return []


def historical_detail(cwd: str, session_id: str,
                      max_bytes: int = 64 * 1024) -> dict[str, Any]:
    """Read an exact local memory id through memory's path/scope checks."""
    if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256:
        raise ValueError("invalid session id")
    return {
        "session_id": session_id,
        "managed": False,
        "controllable": False,
        "history": memory.read_session(
            session_id, cwd=cwd, max_bytes=min(max(1, int(max_bytes)), 64 * 1024),
            redact=True),
    }


__all__ = [
    "MAX_PROMPT_BYTES", "MobileSessionManager", "historical_detail",
    "historical_sessions",
]
