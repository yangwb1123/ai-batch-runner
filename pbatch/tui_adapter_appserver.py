"""Bidirectional Codex App Server JSON-RPC adapter for the TUI runtime."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import OrderedDict
from typing import Any, Iterator, Mapping, Optional

from .tui_codex_support import (
    _bound, _bounded_id, _id, _now, _request_value, _workspace_id,
)
from .tui_command_artifacts import (
    CommandArtifactContext,
    CommandArtifactOutcome,
    CommandArtifactStore,
)
from .tui_actor_tree import (
    actor_authority_payload, root_actor_authority_payload)
from .tui_appserver_support import (
    APPROVAL_METHODS, DEFAULT_APPROVAL_TTL, DEFAULT_MAX_PENDING_APPROVALS,
    DEFAULT_QUEUE_SIZE,
    RUNTIME_NAME, ApprovalEntry, DefaultTransport, EventQueue, ItemProjector,
    RuntimeEventStream, Transport, approval_decisions, approval_expiry,
    approval_payload, bounded_mapping, canonical_approval_decision,
    decline_on_close, decision_result, effective_policy_values, explicit_policy_values,
    item_text, json_summary,
    make_runtime_info, run_startup, start_adapter_transport, startup_timeouts,
    record_server_info, request_key, review_start_params, target_summary, thread_id,
    session_local_id, session_page, session_query_params, thread_observation, turn_id,
    turn_outcome, valid_request_id,
)
from .review import extract_findings
from .tui_protocol import RuntimeEvent, TurnState
from .tui_provider_archive import apply as apply_provider_archive
from .tui_runtime import (
    RuntimeInfo, RuntimeSession, RuntimeSessionObservation,
    SessionReconciliationError, TurnHandle, UnsupportedOperation,
)
from .version import VERSION

_ACTOR_FIELD_NAMES = ("actorId", "actor_id", "parentActorId", "parent_actor_id")
_ACTOR_ID_MAX_BYTES = 256
class CodexAppServerAdapter(ItemProjector):
    """Map App Server requests and notifications to provider-neutral events."""
    def __init__(self, cwd: str | os.PathLike[str], *, daemon_socket: Optional[str] = None,
                 transport: Optional[Transport] = None, maxsize: int = DEFAULT_QUEUE_SIZE,
                 request_timeout: Optional[float] = None,
                 startup_timeout: Optional[float] = None,
                 approval_ttl: float = DEFAULT_APPROVAL_TTL,
                 max_pending_approvals: int = DEFAULT_MAX_PENDING_APPROVALS):
        self._timeout, self._startup_timeout, self._approval_ttl = startup_timeouts(
            request_timeout, startup_timeout, approval_ttl)
        if (isinstance(max_pending_approvals, bool)
                or not isinstance(max_pending_approvals, int)
                or max_pending_approvals <= 0):
            raise ValueError("max_pending_approvals must be a positive integer")
        self._cwd = os.path.realpath(os.path.abspath(os.fsdecode(cwd)))
        self._daemon_socket = daemon_socket
        self._transport, self._queue = transport, EventQueue(maxsize)
        self._lock, self._write_lock = threading.RLock(), threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._closed = self._stream_ended = self._initialized = False
        self._next_id, self._seq = 1, 0
        self._responses: dict[tuple[type, Any], queue.Queue[Any]] = {}
        self._approvals: dict[tuple[type, Any], ApprovalEntry] = {}
        self._approval_timers: dict[tuple[type, Any], threading.Timer] = {}
        self._max_pending_approvals = max_pending_approvals
        self._resolved: OrderedDict[tuple[type, Any], None] = OrderedDict()
        self._resolved_limit = max(64, max_pending_approvals * 4)
        self._announced_threads: set[str] = set()
        self._provider_actor_parents: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._provider_actor_terminal: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._provider_actor_limit = 1024
        self._command_output: dict[str, str] = {}
        self._usage: dict[str, Any] = {}
        self._active: Optional[TurnHandle] = None
        self._active_operation = ""
        self._provider_turn_id = ""
        self._session = RuntimeSession(
            _id("ses"), "", _workspace_id(self._cwd), _now(), "ACTIVE")
        self.server_info: dict[str, Any] = {}
        self._policy_state: dict[str, str] = {
            "approval": "unknown", "sandbox": "unknown", "network": "unknown",
        }
        self._command_artifacts = CommandArtifactStore(self._cwd)

    @property
    def session(self) -> RuntimeSession:
        return self._session

    @property
    def policy_state(self) -> dict[str, str]:
        """Return observed effective policy values without local inference."""
        with self._lock:
            return dict(self._policy_state)

    @property
    def pending_approvals(self) -> tuple[ApprovalEntry, ...]:
        with self._lock:
            return tuple(self._approvals.values())

    def runtime_info(self) -> RuntimeInfo:
        return self._runtime_info()

    def _runtime_info(self) -> RuntimeInfo:
        return make_runtime_info(
            self._initialized and not self._stream_ended and not self._closed, self.server_info)

    def initialize(self, request: Any = None) -> RuntimeInfo:
        self._ensure_open()
        workspace = str(_request_value(request, "workspace_id", "") or "")
        if workspace:
            self._session.workspace_id = workspace
        try:
            run_startup(self._initialize_transport, self.close, self._startup_timeout)
            self._ensure_open()
        except Exception as exc:
            self.close()
            raise RuntimeError(f"codex app-server unavailable: {exc}") from exc
        self._initialized = True
        return self.runtime_info()

    def _initialize_transport(self) -> None:
        self._start_transport()
        result = self._call("initialize", {
            "clientInfo": {"name": "pi-batch", "title": "pi-batch", "version": VERSION},
            "capabilities": {"experimentalApi": True},
        }, timeout=self._startup_timeout)
        self.server_info = record_server_info(result)
        self._observe_policy(result)
        if self._stream_ended: raise RuntimeError("connection lost during initialize")
        self._ensure_open()
        self._send({"method": "initialized", "params": {}})

    def _start_transport(self) -> None:
        start_adapter_transport(self, DefaultTransport)

    def _call(self, method: str, params: Mapping[str, Any], *,
              timeout: Optional[float] = None) -> Mapping[str, Any]:
        timeout = self._timeout if timeout is None else timeout
        with self._lock:
            if self._stream_ended:
                raise RuntimeError("connection lost")
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue[Any] = queue.Queue(maxsize=1)
            self._responses[request_key(request_id)] = waiter
        try:
            self._send({"id": request_id, "method": method, "params": dict(params)})
            response = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            with self._lock:
                self._responses.pop(request_key(request_id), None)
            raise RuntimeError(f"{method} timed out after {timeout:g}s") from exc
        except Exception:
            with self._lock:
                self._responses.pop(request_key(request_id), None)
            raise
        if isinstance(response, Exception):
            raise response
        if "error" in response:
            error = response.get("error") or {}
            raise RuntimeError(f"{method} failed: {error.get('code')} {error.get('message', '')}")
        result = response.get("result", {})
        return result if isinstance(result, Mapping) else {}

    def _send(self, value: Mapping[str, Any]) -> None:
        if self._transport is None:
            raise RuntimeError("transport is not connected")
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with self._write_lock:
            self._transport.send_line(line)

    def start_session(self, request: Any) -> RuntimeSession:
        params: dict[str, Any] = {}
        workspace = _request_value(request, "workspace", None)
        params["workspace" if workspace else "cwd"] = workspace or self._cwd
        model = str(_request_value(request, "model", "") or "").strip()
        if model:
            params["model"] = _bound(model, 512)[0]
        return self._session_call("thread/start", params, request, "created")

    def resume_session(self, request: Any) -> RuntimeSession:
        provider_id = self._provider_id(request)
        self._ensure_session_replaceable()
        self._reconcile_provider_session(provider_id)
        return self._session_call(
            "thread/resume", {"threadId": provider_id}, request, "resumed")

    def read_session(self, request: Any) -> RuntimeSessionObservation:
        """Read one provider thread summary without adopting or subscribing to it."""
        self._require_initialized()
        provider_id = self._provider_id(request)
        result = self._call("thread/read", {
            "threadId": provider_id, "includeTurns": False})
        return thread_observation(result, provider_id)

    def fork_session(self, request: Any) -> RuntimeSession:
        provider_id = self._provider_id(request)
        return self._session_call(
            "thread/fork", {"threadId": provider_id}, request, "forked", force_new=True)

    def _provider_id(self, request: Any) -> str:
        provider_id = _request_value(request, "provider_session_id", "") or ""
        requested = _request_value(request, "session_id", "") or ""
        if not isinstance(provider_id, str) or not isinstance(requested, str):
            raise ValueError("provider session ids must be strings")
        if not provider_id and requested == self._session.session_id:
            provider_id = self._session.provider_session_id
        if not provider_id:
            provider_id = requested
        if not provider_id:
            raise ValueError("provider_session_id is required")
        try:
            provider_size = len(provider_id.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("provider_session_id must be valid UTF-8") from exc
        if provider_size > 2048:
            raise ValueError("provider_session_id is too long")
        return provider_id

    def _reconcile_provider_session(self, provider_id: str) -> None:
        try:
            observation = self.read_session({"provider_session_id": provider_id})
        except ValueError as exc:
            raise SessionReconciliationError(
                "unverified", f"provider thread status is invalid: {_bound(exc, 1024)[0]}") from exc
        except RuntimeError as exc:
            raise SessionReconciliationError(
                "unavailable", f"provider thread status is unavailable: {_bound(exc, 1024)[0]}") from exc
        if observation.status in {"notLoaded", "idle"}:
            return
        if observation.status == "active":
            flags = ", ".join(observation.active_flags) or "unspecified activity"
            raise SessionReconciliationError(
                "active", f"provider thread is active ({flags})")
        raise SessionReconciliationError(
            "systemError", "provider thread is in systemError state")

    def _ensure_session_replaceable(self) -> None:
        with self._lock:
            if self._active_operation:
                raise RuntimeError("cannot replace a session during an active operation")
            if self._active is not None and not self._active.done:
                raise RuntimeError("cannot replace an active runtime session")

    def _session_call(self, method: str, params: Mapping[str, Any], request: Any,
                      mode: str, force_new: bool = False) -> RuntimeSession:
        self._require_initialized()
        with self._lock:
            self._ensure_session_replaceable()
            previous = self._session
            requested_id = str(_request_value(request, "session_id", "") or "")
            local_id = session_local_id(previous, requested_id, mode, force_new)
            workspace = str(_request_value(request, "workspace_id", "") or
                            previous.workspace_id)
            self._session = RuntimeSession(local_id, "", workspace, _now(), "ACTIVE")
        try:
            result = self._call(method, params)
            thread = result.get("thread", result)
            self._observe_policy(result)
            self._observe_policy(thread)
            provider_id = thread_id(thread) or str(params.get("threadId", ""))
            self._session.provider_session_id = provider_id
            return self._session
        except Exception:
            with self._lock:
                self._session = previous
            raise

    def list_sessions(self, query: Any = None):
        """Read one bounded history page without adopting or subscribing."""
        self._require_initialized()
        params = session_query_params(query, self._cwd)
        result = self._call("thread/list", params)
        return session_page(result, limit=params["limit"])

    def start_turn(self, request: Any) -> TurnHandle:
        self._require_initialized()
        prompt = str(_request_value(request, "prompt", request if isinstance(request, str) else ""))
        if not prompt.strip() or not self._session.provider_session_id:
            raise ValueError("prompt and active provider session are required")
        with self._lock:
            if (self._active_operation
                    or (self._active is not None and not self._active.done)):
                raise RuntimeError("an app-server turn is already running")
            self._command_artifacts.reset()
            self._command_output.clear()
            self._provider_actor_parents.clear()
            self._provider_actor_terminal.clear()
            handle = TurnHandle(
                self._session.session_id,
                str(_request_value(request, "run_id", "") or _id("run")),
                str(_request_value(request, "turn_id", "") or _id("turn")),
                str(_request_value(request, "actor_id", "agent_main") or "agent_main"), _now())
            self._active, self._provider_turn_id = handle, ""
            self._active_operation = "turn"
        try:
            params = {
                "threadId": self._session.provider_session_id,
                "input": [{"type": "text", "text": prompt}],
            }
            model = str(_request_value(request, "model", "") or "").strip()
            if model:
                params["model"] = _bound(model, 512)[0]
            result = self._call("turn/start", params)
            self._observe_policy(result)
            self._observe_policy(result.get("turn", result))
            self._provider_turn_id = turn_id(result.get("turn", result))
            return handle
        except Exception:
            with self._lock:
                self._active = None
                self._active_operation = ""
            raise

    def steer_turn(self, request: Any) -> None:
        handle = self._current_handle(request)
        text = str(_request_value(request, "text", "") or "")
        if not text.strip():
            raise ValueError("steer text must not be empty")
        expected = self._provider_turn_id
        if not expected:
            raise RuntimeError("active provider turn id is unavailable")
        result = self._call("turn/steer", {
            "threadId": self._session.provider_session_id,
            "expectedTurnId": expected,
            "input": [{"type": "text", "text": text}]})
        if turn_id(result) != expected:
            raise RuntimeError("turn/steer response did not confirm the active turn")
        if not handle.done:
            self._publish("turn.steered", {"text": _bound(text)[0]})

    def interrupt_turn(self, request: Any) -> None:
        handle = self._current_handle(request)
        self._call("turn/interrupt", {"threadId": self._session.provider_session_id,
                                      "turnId": self._provider_turn_id})
        if not handle.done:
            handle.state = TurnState.INTERRUPTING
            self._publish("turn.interrupting", {"state": handle.state})

    def _current_handle(self, request: Any) -> TurnHandle:
        requested = str(_request_value(request, "turn_id", "") or "")
        with self._lock:
            handle = self._active
            matches = handle is not None and requested in {handle.turn_id, self._provider_turn_id}
            if not matches or handle.done:
                raise ValueError("request does not match the active turn")
            return handle

    def resolve_approval(self, decision: Any) -> bool:
        request_id = _request_value(decision, "request_id", None)
        key = request_key(request_id)
        with self._lock:
            entry = self._approvals.get(key)
            if entry is None:
                return False
            expired = time.monotonic() - entry.received_monotonic >= self._approval_ttl
            if expired:
                entry = None
            elif not self._approval_scope_matches(entry, decision):
                return False
        if entry is None:
            self._expire_approval(key, "approval TTL expired")
            return False
        with self._lock:
            entry = self._approvals.get(key)
            if entry is None or not self._approval_scope_matches(entry, decision):
                return False
            claimed = _request_value(decision, "approval_type",
                                     _request_value(decision, "request_type", ""))
            if claimed and claimed not in {entry.approval_type, entry.method}:
                return False
            requested = canonical_approval_decision(
                _request_value(decision, "decision", ""))
            if requested not in entry.available_decisions:
                return False
            result = decision_result(entry, decision)
            if result is None:
                return False
            try:
                self._send({"id": entry.request_id, "result": result})
            except Exception as exc:
                self._connection_lost(str(exc))
                return False
            self._pop_approval(key)
            self._remember_resolved(key)
        value = str(_request_value(decision, "decision", "") or "")
        self._publish("approval.resolved", approval_payload(entry, decision=value))
        return True

    def _approval_scope_matches(self, entry: ApprovalEntry, decision: Any) -> bool:
        handle = self._active
        if handle is None or handle.done:
            return False
        active = (self._session.workspace_id, self._session.session_id,
                  handle.run_id, handle.turn_id, handle.actor_id)
        expected = (entry.workspace_id, entry.session_id, entry.run_id,
                    entry.turn_id, entry.actor_id)
        if active != expected:
            return False
        for name, value in zip(
                ("workspace_id", "session_id", "run_id", "turn_id", "actor_id"),
                expected):
            supplied = _request_value(decision, name, "")
            if supplied not in (None, "") and str(supplied) != value:
                return False
        return True

    def start_review(self, request: Any) -> TurnHandle:
        """Start one provider-owned read-only review on the active thread."""
        self._require_initialized()
        if not self._runtime_info().capabilities.supports("review"):
            raise UnsupportedOperation("review/start", "review")
        self._validate_review_scope(request)
        thread_id_value = self._session.provider_session_id
        params = review_start_params(request, thread_id_value)
        with self._lock:
            if (self._active_operation
                    or (self._active is not None and not self._active.done)):
                raise RuntimeError("an app-server operation is already running")
            self._command_artifacts.reset()
            self._command_output.clear()
            self._provider_actor_parents.clear()
            self._provider_actor_terminal.clear()
            handle = TurnHandle(
                self._session.session_id,
                str(_request_value(request, "run_id", "") or _id("review-run")),
                str(_request_value(request, "turn_id", "") or _id("review-turn")),
                str(_request_value(request, "actor_id", "agent_main") or "agent_main"),
                _now())
            self._active, self._provider_turn_id = handle, ""
            self._active_operation = "review"
        try:
            result = self._call("review/start", params)
            self._observe_policy(result)
            response_thread = result.get("reviewThreadId")
            if not isinstance(response_thread, str) or response_thread != thread_id_value:
                raise RuntimeError("review/start response did not confirm the active thread")
            provider_turn = turn_id(result.get("turn", result))
            if not provider_turn:
                raise RuntimeError("review/start response omitted the provider turn id")
            self._provider_turn_id = provider_turn
            return handle
        except Exception:
            with self._lock:
                if self._active is handle:
                    self._active = None
                    self._active_operation = ""
            raise

    def _validate_review_scope(self, request: Any) -> None:
        for name, expected in (("workspace_id", self._session.workspace_id),
                               ("session_id", self._session.session_id)):
            supplied = _request_value(request, name, "")
            if supplied not in (None, "") and str(supplied) != expected:
                raise ValueError(f"review {name} does not match the active session")

    review_start = start_review

    def archive_session(self, request: Any) -> None:
        apply_provider_archive(self, request, archived=True)

    def unarchive_session(self, request: Any) -> None:
        apply_provider_archive(self, request, archived=False)

    def _read_loop(self) -> None:
        try:
            while True:
                line = self._transport.recv_line() if self._transport is not None else None
                if line is None or line == "":
                    raise EOFError(self._transport_reason())
                value = json.loads(line)
                if not isinstance(value, Mapping):
                    raise ValueError("JSON-RPC line is not an object")
                self._dispatch(dict(value))
        except Exception as exc:
            if not self._closed:
                self._connection_lost(str(exc))

    def _dispatch(self, value: dict[str, Any]) -> None:
        if "id" in value and ("result" in value or "error" in value):
            with self._lock:
                waiter = self._responses.pop(request_key(value["id"]), None)
            if waiter is not None:
                waiter.put(value)
            return
        method = value.get("method")
        if not isinstance(method, str):
            self._unknown("<missing>", value)
        elif "id" in value:
            self._server_request(value["id"], method, value.get("params", {}))
        else:
            self._notification(method, value.get("params", {}))

    def _item(self, phase: str, raw_item: Any) -> None:
        super()._item(phase, raw_item)
        if phase != "completed" or self._active_operation != "review":
            return
        if not isinstance(raw_item, Mapping):
            return
        item_type = str(raw_item.get("type", ""))
        if item_type not in {"agentMessage", "agent_message"}:
            return
        text = item_text(raw_item, "text").get("text", "")
        findings, _invalid = extract_findings([text])
        for finding in findings:
            self._publish("review.finding", finding)

    def _project_explicit_actor(self, phase: str,
                                item: Mapping[str, Any]) -> None:
        if not _has_explicit_actor_fields(item):
            return
        if not self._runtime_info().capabilities.supports("subagents"):
            self._actor_warning("subagents capability was not declared")
            return
        handle = self._active
        actor_id = _wire_actor_id(_item_value(
            item, "actorId", "actor_id"))
        parent = _wire_actor_id(_item_value(
            item, "parentActorId", "parent_actor_id"))
        if handle is None or handle.done or not actor_id or not parent:
            self._actor_warning("provider actor item has invalid active scope")
            return
        if actor_id == handle.actor_id or actor_id == parent:
            self._actor_warning("provider actor item has an invalid lineage")
            return
        key = (handle.run_id, actor_id)
        parent_key = (handle.run_id, parent)
        if parent != handle.actor_id and parent_key not in self._provider_actor_parents:
            self._actor_warning("provider actor parent was not previously observed")
            return
        if phase == "started":
            self._provider_actor_started(key, parent, item)
        elif phase == "completed":
            self._provider_actor_completed(key, item)

    def _provider_actor_started(self, key: tuple[str, str],
                                parent: str, item: Mapping[str, Any]) -> None:
        if key in self._provider_actor_parents:
            self._actor_warning("duplicate provider actor start")
            return
        if not self._reserve_provider_actor(key):
            return
        self._provider_actor_parents[key] = parent
        payload = {
            "actor_id": key[1], "parent_actor_id": parent,
            "name": _actor_display(item.get("name", item.get("label", "")),
                                    "provider actor"),
            "role": _actor_display(item.get("role", ""), "provider actor"),
            "provider": _actor_display(item.get("provider", ""), ""),
            "status": "RUNNING",
        }
        authority = actor_authority_payload(
            item.get("actorAuthority", item.get("actor_authority")), parent)
        if authority:
            payload["authority"] = authority
        self._publish("actor.spawned", payload)

    def _provider_actor_completed(self, key: tuple[str, str],
                                  item: Mapping[str, Any]) -> None:
        parent = self._provider_actor_parents.get(key)
        if parent is None:
            self._actor_warning("provider actor completed before start")
            return
        if key in self._provider_actor_terminal:
            self._actor_warning("duplicate provider actor completion")
            return
        payload: dict[str, Any] = {
            "actor_id": key[1], "parent_actor_id": parent,
        }
        success = item.get("success")
        if isinstance(success, bool):
            payload["success"] = success
        authority = actor_authority_payload(
            item.get("actorAuthority", item.get("actor_authority")), parent)
        if authority:
            payload["authority"] = authority
        self._publish("actor.completed", payload)
        self._provider_actor_terminal[key] = None
        self._provider_actor_terminal.move_to_end(key)
        while len(self._provider_actor_terminal) > self._provider_actor_limit:
            self._provider_actor_terminal.popitem(last=False)

    def _reserve_provider_actor(self, key: tuple[str, str]) -> bool:
        if len(self._provider_actor_parents) < self._provider_actor_limit:
            return True
        victim = next((item for item in self._provider_actor_parents
                       if item in self._provider_actor_terminal), None)
        if victim is None:
            self._actor_warning("provider actor projection limit reached")
            return False
        self._provider_actor_parents.pop(victim, None)
        self._provider_actor_terminal.pop(victim, None)
        return True

    def _actor_warning(self, reason: str) -> None:
        self._publish("runtime.warning", {
            "category": "provider_actor", "reason": _bound(reason, 2048)[0]})

    def _notification(self, method: str, raw_params: Any) -> None:
        params = dict(raw_params) if isinstance(raw_params, Mapping) else {}
        if method == "thread/started":
            self._observe_policy(params)
            self._thread_started(params)
        elif method == "turn/started":
            self._observe_policy(params)
            self._turn_started(params)
        elif method == "turn/completed":
            self._turn_completed(params)
        elif method in {"item/started", "item/completed"}:
            self._item(method.rsplit("/", 1)[-1], params.get("item", {}))
        elif method == "item/agentMessage/delta":
            self._publish("message.delta", item_text(params, "delta"))
        elif method == "item/commandExecution/outputDelta":
            self._command_delta(params)
        elif method == "item/fileChange/patchUpdated":
            self._file_change(params, "updated")
        elif method == "thread/tokenUsage/updated":
            self._usage = bounded_mapping(params.get("tokenUsage", params.get("usage", {})))
            self._publish("usage.updated", {"usage": self._usage})
        elif method == "warning":
            self._publish("runtime.warning", {"message": _bound(params.get("message", ""))[0]})
        elif method == "error":
            error = params.get("error", params)
            message = error.get("message", "") if isinstance(error, Mapping) else error
            self._publish("runtime.error", {
                "message": _bound(message, 4096)[0],
                "error": _bound(json_summary(error), 8192)[0],
            })
        elif method == "serverRequest/resolved":
            self._server_resolved(params)
        else:
            self._unknown(method, params)

    def _thread_started(self, params: Mapping[str, Any]) -> None:
        thread = params.get("thread", params)
        provider_id = thread_id(thread)
        if provider_id:
            self._session.provider_session_id = provider_id
        if provider_id in self._announced_threads:
            return
        self._announced_threads.add(provider_id)
        self._publish("session.created", {"provider_session_id": provider_id})

    def _turn_started(self, params: Mapping[str, Any]) -> None:
        handle = self._active
        if handle is None or handle.done or handle.state != TurnState.QUEUED:
            return
        self._provider_turn_id = turn_id(params.get("turn", params)) or self._provider_turn_id
        self._publish("turn.queued", {"state": TurnState.QUEUED})
        handle.state = TurnState.RUNNING
        payload = {"state": TurnState.RUNNING}
        authority = root_actor_authority_payload(
            params, self._runtime_info().capabilities.supports("subagents"))
        if authority: payload["authority"] = authority
        self._publish("turn.started", payload)
        policy = explicit_policy_values(self.policy_state)
        if policy: self._publish("policy.updated", policy)

    def _turn_completed(self, params: Mapping[str, Any]) -> None:
        handle = self._active
        if handle is None or handle.done:
            return
        turn = params.get("turn", params)
        provider_id = turn_id(turn) or self._provider_turn_id
        if self._provider_turn_id and provider_id != self._provider_turn_id:
            return
        usage = params.get("usage", self._usage)
        self._command_artifacts.reset()
        self._command_output.clear()
        self._provider_actor_parents.clear()
        self._provider_actor_terminal.clear()
        self._invalidate_turn_approvals(handle)
        kind, handle.state, payload = turn_outcome(turn)
        payload.update({"provider_turn_id": provider_id,
                        "usage": bounded_mapping(usage)})
        self._publish(kind, payload)
        with self._lock:
            if self._active is handle:
                self._active_operation = ""
        handle._done.set()

    def _invalidate_turn_approvals(self, handle: TurnHandle) -> None:
        with self._lock:
            entries = [
                self._pop_approval(key)
                for key, entry in list(self._approvals.items())
                if entry.session_id == handle.session_id
                and entry.run_id == handle.run_id and entry.turn_id == handle.turn_id
            ]
        for entry in entries:
            if entry is not None:
                self._publish("approval.expired", approval_payload(
                    entry, reason="turn completed, approval invalidated"))

    def _command_artifact_started(self, item_id: str) -> None:
        context = self._command_artifact_context(item_id)
        if context is not None:
            self._command_artifacts.abort(context)

    def _command_artifact_delta(self, item_id: str, delta: str) -> None:
        context = self._command_artifact_context(item_id)
        if context is None:
            return
        try:
            self._command_artifacts.append(context, delta)
        except (OSError, RuntimeError, ValueError):
            return  # completion emits one bounded diagnostic

    def _command_artifact_completed(
            self, item_id: str, command: str,
            output: Any, supplied: bool) -> None:
        context = self._command_artifact_context(item_id)
        if context is None:
            if _bound(output)[1]:
                self._artifact_warning(item_id, "command item id is unavailable")
            return
        try:
            outcome = (self._command_artifacts.capture(context, output)
                       if supplied else self._command_artifacts.finish(context))
        except (OSError, RuntimeError, ValueError) as exc:
            outcome = CommandArtifactOutcome(warning=str(exc))
        if outcome.artifact is not None:
            name = f"Command output · {_bound(command, 160)[0]}"
            self._publish("artifact.created",
                          outcome.artifact.event_payload(_bounded_id(item_id), name))
        elif outcome.warning:
            self._artifact_warning(item_id, outcome.warning)

    def _command_artifact_context(
            self, item_id: str) -> Optional[CommandArtifactContext]:
        handle = self._active
        bounded = _bounded_id(item_id)
        if handle is None or handle.done or not bounded:
            return None
        return CommandArtifactContext(
            self._session.workspace_id, self._session.session_id,
            handle.run_id, handle.turn_id, bounded)

    def _artifact_warning(self, item_id: str, reason: str) -> None:
        self._publish("runtime.warning", {
            "category": "artifact_store", "item_id": _bounded_id(item_id),
            "reason": _bound(reason, 2048)[0]})

    def _server_request(self, request_id: Any, method: str, raw_params: Any) -> None:
        approval_type = APPROVAL_METHODS.get(method)
        if approval_type is None or not valid_request_id(request_id):
            self._unknown(method, {"request_id": request_id, "params": raw_params})
            return
        params = dict(raw_params) if isinstance(raw_params, Mapping) else {}
        entry = self._new_approval_entry(request_id, method, approval_type, params)
        if entry is None:
            self._reject_unscoped_approval(request_id, method, approval_type, params)
            return
        key = request_key(request_id)
        over_limit = False
        with self._lock:
            if key in self._approvals:
                self._publish("runtime.warning", {
                    "message": "duplicate approval request id", "request_id": request_id})
                return
            over_limit = len(self._approvals) >= self._max_pending_approvals
            if not over_limit:
                self._approvals[key] = entry
                timer = threading.Timer(
                    self._approval_ttl, self._expire_approval, args=(key,))
                timer.daemon = True
                self._approval_timers[key] = timer
                timer.start()
        if over_limit:
            self._decline_entry(entry, "pending approval limit reached")
            return
        self._publish("approval.requested", approval_payload(entry))

    def _new_approval_entry(
            self, request_id: str | int, method: str, approval_type: str,
            params: Mapping[str, Any]) -> Optional[ApprovalEntry]:
        handle = self._active
        if handle is None or handle.done:
            return None
        provider_thread = str(params.get("threadId", params.get("thread_id", "")) or "")
        provider_turn = str(params.get("turnId", params.get("turn_id", "")) or "")
        if provider_thread and provider_thread != self._session.provider_session_id:
            return None
        if provider_turn and self._provider_turn_id and provider_turn != self._provider_turn_id:
            return None
        return ApprovalEntry(
            request_id, method, approval_type, json_summary(params),
            target_summary(params), _now(), approval_expiry(self._approval_ttl),
            time.monotonic(), approval_decisions(params),
            self._session.workspace_id, self._session.session_id,
            handle.run_id, handle.turn_id, handle.actor_id)

    def _reject_unscoped_approval(
            self, request_id: str | int, method: str, approval_type: str,
            params: Mapping[str, Any]) -> None:
        self._send_decline(request_id, method, approval_type, params)

    def _send_decline(
            self, request_id: str | int, method: str, approval_type: str,
            params: Mapping[str, Any]) -> None:
        entry = ApprovalEntry(
            request_id, method, approval_type, json_summary(params),
            target_summary(params), _now(), approval_expiry(self._approval_ttl),
            time.monotonic(), approval_decisions(params),
            self._session.workspace_id, self._session.session_id, "", "", "")
        self._decline_entry(entry, "approval rejected outside an active turn")

    def _expire_approval(
            self, key: tuple[type, Any], reason: str = "approval TTL expired") -> None:
        with self._lock:
            entry = self._pop_approval(key)
        if entry is not None:
            self._decline_entry(entry, reason, expired=True)

    def _decline_entry(
            self, entry: ApprovalEntry, reason: str, *, expired: bool = False) -> None:
        result = decision_result(entry, {"decision": "deny"})
        try:
            self._send({"id": entry.request_id, "result": result or {"decision": "decline"}})
        except Exception as exc:
            if expired and not self._closed:
                self._publish("approval.expired", approval_payload(entry, reason=reason))
            self._connection_lost(str(exc))
            return
        with self._lock:
            self._remember_resolved(request_key(entry.request_id))
        if expired and not self._closed:
            self._publish("approval.expired", approval_payload(entry, reason=reason))
        elif not expired:
            self._publish("runtime.warning", {
                "message": reason, "request_id": entry.request_id})

    def _pop_approval(self, key: tuple[type, Any]) -> Optional[ApprovalEntry]:
        entry = self._approvals.pop(key, None)
        timer = self._approval_timers.pop(key, None)
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        return entry

    def _server_resolved(self, params: Mapping[str, Any]) -> None:
        request_id = params.get("requestId", params.get("request_id"))
        if not valid_request_id(request_id):
            self._unknown("serverRequest/resolved", params)
            return
        key = request_key(request_id)
        if key in self._resolved:
            return
        entry = self._pop_approval(key)
        self._remember_resolved(key)
        if entry is not None:
            self._publish("approval.resolved", approval_payload(entry))

    def _remember_resolved(self, key: tuple[type, Any]) -> None:
        self._resolved[key] = None
        self._resolved.move_to_end(key)
        while len(self._resolved) > self._resolved_limit:
            self._resolved.popitem(last=False)

    def _observe_policy(self, value: Any) -> None:
        observed = effective_policy_values(value)
        if observed:
            with self._lock:
                self._policy_state.update(observed)

    def _unknown(self, method: str, value: Any) -> None:
        self._publish("runtime.unknown_item", {
            "method": _bound(method, 256)[0], "raw": json_summary(value)})

    def _publish(self, kind: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            if self._closed:
                return
            self._seq += 1
            handle = self._active
            event = RuntimeEvent(
                event_id=_id("evt"), seq=self._seq, occurred_at=_now(),
                workspace_id=self._session.workspace_id,
                session_id=self._session.session_id,
                run_id=handle.run_id if handle else "",
                turn_id=handle.turn_id if handle else "",
                actor_id=handle.actor_id if handle else "agent_main", kind=kind,
                payload=bounded_mapping(payload), extensions={"runtime": RUNTIME_NAME})
        self._queue.put(event)

    def _connection_lost(self, reason: str) -> None:
        with self._lock:
            if self._stream_ended:
                return
            self._stream_ended = True
            approvals = [self._pop_approval(key) for key in list(self._approvals)]
            waiters = list(self._responses.values())
            self._responses.clear()
            handle = self._active
            self._active_operation = ""
            if handle is not None and not handle.done:
                handle.state = TurnState.DISCONNECTED
                handle._done.set()
        self._command_artifacts.reset()
        self._command_output.clear()
        error = RuntimeError(f"connection lost: {reason}")
        for waiter in waiters:
            waiter.put(error)
        for entry in approvals:
            if entry is not None:
                self._publish("approval.expired", approval_payload(
                    entry, reason="connection lost, approval invalidated"))
        self._publish("runtime.error", {
            "reason": _bound(reason)[0],
            "message": "connection lost, approval invalidated"})
        self._queue.close()

    def _transport_reason(self) -> str:
        reason = getattr(self._transport, "unavailable_reason", None)
        return str(reason()) if callable(reason) else "transport EOF"

    def events(self) -> Iterator[RuntimeEvent]:
        return RuntimeEventStream(self._queue)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            approvals = [self._pop_approval(key) for key in list(self._approvals)]
            waiters = list(self._responses.values())
            self._responses.clear()
            transport, reader, handle = self._transport, self._reader, self._active
            self._active_operation = ""
        for waiter in waiters:
            waiter.put(RuntimeError("app-server adapter closed"))
        decline = threading.Thread(
            target=decline_on_close, args=(self._send, approvals), daemon=True,
            name="codex-app-server-decline")
        decline.start()
        decline.join(timeout=0.1)
        if transport is not None:
            transport.close()
        decline.join(timeout=0.5)
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        if handle is not None and not handle.done:
            handle.state = TurnState.UNKNOWN
            handle._done.set()
        self._command_artifacts.close()
        self._command_output.clear()
        self._queue.close()

    def _require_initialized(self) -> None:
        self._ensure_open()
        if not self._initialized:
            raise RuntimeError("initialize must be called first")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("app-server runtime is closed")


def _has_explicit_actor_fields(item: Mapping[str, Any]) -> bool:
    return any(name in item for name in _ACTOR_FIELD_NAMES)


def _item_value(item: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in item:
            return item[name]
    return None


def _wire_actor_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        if len(value.encode("utf-8")) > _ACTOR_ID_MAX_BYTES:
            return ""
    except UnicodeEncodeError:
        return ""
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return ""
    return value


def _actor_display(value: Any, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    text = " ".join(value.split())
    if not text:
        return fallback
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= 512:
        return text
    return encoded[:509].decode("utf-8", "ignore") + "..."
