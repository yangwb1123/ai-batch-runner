"""Outbound Hub Gateway that owns and publishes local AgentSession objects."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Callable, Optional

from .agent_session import AgentSession, create_agent_session
from .hub_client import MAX_PROMPT_BYTES, HubClient, HubError
from .hub_gateway_store import GatewayStore, StateDirectoryLock
from .hub_gateway_protocol import (EVENT_NAMESPACE, TERMINAL,
                                   TERMINAL_STATES, VISIBLE_EVENTS, field,
                                   has_terminal_event, is_fencing_error,
                                   nested_int, nested_text,
                                   project_event_payload, running_event_id,
                                   truth)
from .hub_gateway_config import HubGatewayConfig, normalize_config, session_options
from .memory_records import redact_content
from .tui_protocol import RuntimeEvent

log = logging.getLogger("pi-batch.hub-gateway")


@dataclass
class _ManagedSession:
    local_id: str
    hub_id: str
    name: str
    session: AgentSession
    active_turn: str = ""
    runtime_turns: dict[str, str] | None = None


class HubGateway:
    """A single-process owner of local sessions routed through the hub."""

    def __init__(self, options: HubGatewayConfig, *, client: Any = None,
                 session_factory: Optional[Callable[..., AgentSession]] = None):
        self.options = normalize_config(options)
        self.client = client or HubClient.from_env(self.options.hub_url)
        self._factory = session_factory or create_agent_session
        self._store = GatewayStore(self.options.state_dir)
        self._state_lock = StateDirectoryLock(self._store.directory)
        try:
            self._state_lock.acquire()
            self._bind_instance_id()
        except Exception:
            self._state_lock.close()
            self._store.close()
            raise
        self._stop = threading.Event()
        self._sessions: dict[str, _ManagedSession] = {}
        self._session_lock = threading.RLock()
        self._turn_threads: dict[str, threading.Thread] = {}
        self._pending_starts: dict[str, tuple[str, _ManagedSession, str]] = {}
        self._generation = 0
        self._boot_id = uuid.uuid4().hex
        self._previous_boot_id = (self._store.metadata("candidate_boot_id") or
                                  self._store.metadata("boot_id"))
        self._store.set_metadata("candidate_boot_id", self._boot_id)
        self._last_heartbeat = 0.0
        self._last_poll = 0.0
        self._started = False
        self._closed = False
        self._fatal: Optional[BaseException] = None
        try:
            self._register()
        except Exception:
            self.close()
            raise

    def _bind_instance_id(self) -> None:
        stored = self._store.metadata("instance_id")
        selected = self.options.instance_id.strip() or stored or (
            "inst-" + uuid.uuid4().hex)
        if stored and selected != stored:
            raise ValueError("configured instance id does not match this state directory")
        self.options = replace(self.options, instance_id=selected)
        self._store.set_metadata("instance_id", selected)

    @property
    def instance_id(self) -> str:
        return self.options.instance_id

    @property
    def generation(self) -> int:
        return self._generation

    def start(self) -> None:
        if self._started:
            return
        created: list[_ManagedSession] = []
        try:
            for index in range(self.options.sessions):
                name = f"{self.options.name}-{index + 1}"
                created.append(self._create_session(name))
        except Exception:
            for item in created:
                item.session.close()
            self.close()
            raise
        self._started = True
        self._last_heartbeat = time.monotonic()
        self._last_poll = 0.0

    def _register(self) -> None:
        record = self.client.register_instance(
            instance_id=self.instance_id, boot_id=self._boot_id,
            name=self.options.name, project_id=self.options.project_id)
        generation = nested_int(record, "generation")
        if generation <= 0:
            raise RuntimeError("hub registration response omitted generation")
        self._generation = generation
        self._store.activate_generation(generation, boot_id=self._boot_id,
                                       previous_boot_id=self._previous_boot_id)

    def _create_session(self, name: str) -> _ManagedSession:
        session = self._factory(session_options(self.options, name))
        state = session.state
        local_id = str(state.get("session_id", ""))
        if not local_id:
            session.close()
            raise RuntimeError("AgentSession did not expose a local session id")
        try:
            remote = self.client.register_session(
                instance_id=self.instance_id, generation=self.generation,
                local_session_id=local_id, name=name,
                project_id=self.options.project_id, status="idle", controllable=True)
            hub_id = nested_text(remote, "session_id")
            if not hub_id:
                raise RuntimeError("hub session registration omitted session_id")
            managed = _ManagedSession(local_id, hub_id, name, session,
                                      runtime_turns={})
            self._store.add_session(local_session_id=local_id,
                                    hub_session_id=hub_id, name=name,
                                    project_id=self.options.project_id)
            session.subscribe(lambda event: self._on_event(managed, event))
            with self._session_lock:
                self._sessions[local_id] = managed
            return managed
        except Exception:
            session.close()
            raise

    def run_once(self) -> None:
        self._raise_if_fatal()
        for operation in (self._maybe_heartbeat, self._maybe_poll_commands,
                          self._retry_pending_start, self._flush_outbox):
            try:
                operation()
            except HubError as exc:
                self._handle_hub_error(exc)
                if self._fatal is not None:
                    break
        self._raise_if_fatal()

    def run_forever(self) -> None:
        self.start()
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(min(self.options.poll_interval, 1.0))
        self._raise_if_fatal()

    def _maybe_heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat < self.options.heartbeat_interval:
            return
        self.client.heartbeat(instance_id=self.instance_id,
                              generation=self.generation)
        self._last_heartbeat = now

    def _maybe_poll_commands(self) -> None:
        now = time.monotonic()
        if now - self._last_poll < self.options.poll_interval:
            return
        self._last_poll = now
        commands = self.client.get_commands(instance_id=self.instance_id,
                                            generation=self.generation)
        for command in commands:
            if self._dispatch_command(command):
                break

    def _dispatch_command(self, command: dict[str, Any]) -> bool:
        turn_id = field(command, "turn_id")
        local_id = field(command, "local_session_id")
        if not turn_id or not local_id:
            return False
        managed = self._managed(local_id)
        if managed is None:
            self._publish_synthetic(turn_id, field(command, "session_id"),
                                    "turn.failed", {"reason": "session_unavailable"})
            return True
        if truth(command.get("cancel_requested")):
            self._cancel_command(command, managed)
            return True
        return self._start_command(command, managed)

    def _start_command(self, command: dict[str, Any], managed: _ManagedSession) -> bool:
        turn_id = field(command, "turn_id")
        if managed.active_turn:
            return False
        prompt = command.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            self._publish_synthetic(turn_id, managed.hub_id, "turn.failed",
                                    {"reason": "empty_prompt"})
            return True
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            self._publish_synthetic(turn_id, managed.hub_id, "turn.failed",
                                    {"reason": "prompt_too_large"})
            return True
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        receipt = self._store.turn(turn_id)
        if receipt is None:
            self._store.accept_turn(turn_id=turn_id,
                                    local_session_id=managed.local_id,
                                    prompt_digest=digest)
            receipt = self._store.turn(turn_id)
        if (not receipt or receipt["local_session_id"] != managed.local_id
                or receipt["state"] not in {"accepted", "starting"}):
            return False
        if receipt["prompt_digest"] != digest:
            log.error("Hub command changed after its durable receipt")
            return False
        self._enqueue_running(turn_id, managed.hub_id)
        self._pending_starts[turn_id] = (prompt, managed, managed.hub_id)
        return True

    def _enqueue_running(self, turn_id: str, hub_session_id: str) -> None:
        event_id = running_event_id(self.instance_id, hub_session_id, turn_id)
        body = {"event_id": event_id, "session_id": hub_session_id,
                "turn_id": turn_id, "kind": "turn.running",
                "payload": {"accepted": True}}
        self._store.enqueue_event(event_id=event_id, event=body,
                                  generation=self.generation)

    def _retry_pending_start(self) -> None:
        if not self._pending_starts:
            return
        turn_id, (prompt, managed, hub_session_id) = next(
            iter(self._pending_starts.items()))
        row = self._store.outbox_event(running_event_id(
            self.instance_id, hub_session_id, turn_id))
        if row is None:
            return
        response = self._post_running_ack(turn_id, row)
        if response is None:
            return
        state = nested_text(response, "turn_state")
        if truth(response.get("cancel_requested")) or state == "cancel_requested":
            self._pending_starts.pop(turn_id, None)
            self._publish_synthetic(turn_id, hub_session_id, "turn.interrupted",
                                    {"reason": "cancelled_before_start"})
            return
        if state in TERMINAL_STATES:
            self._store.set_turn(turn_id, state)
            self._pending_starts.pop(turn_id, None)
            return
        if state == "running":
            self._launch_turn(turn_id, prompt, managed, hub_session_id)

    def _post_running_ack(self, turn_id: str,
                          row: dict[str, Any]) -> Optional[dict[str, Any]]:
        try:
            response = self.client.send_event(row["body"],
                                              instance_id=self.instance_id,
                                              generation=self.generation)
        except HubError as exc:
            if exc.code == "turn_terminal":
                self._store.obsolete_event(row["event_id"])
                self._store.set_turn(turn_id, "cancelled")
                self._pending_starts.pop(turn_id, None)
                return None
            self._store.note_event_failure(row["event_id"])
            if is_fencing_error(exc):
                self._fence_and_stop(exc)
            raise
        cursor = nested_text(response, "cursor")
        if not cursor:
            raise HubError("running acknowledgement omitted cursor",
                           code="invalid_response")
        self._store.mark_event_sent(row["event_id"], cursor)
        return response

    def _launch_turn(self, turn_id: str, prompt: str, managed: _ManagedSession,
                     hub_session_id: str) -> None:
        self._pending_starts.pop(turn_id, None)
        self._store.set_turn(turn_id, "starting")
        managed.active_turn = turn_id
        thread = threading.Thread(target=self._run_turn,
                                  args=(turn_id, prompt, managed),
                                  name=f"hub-turn-{turn_id[:12]}", daemon=True)
        self._turn_threads[turn_id] = thread
        try:
            thread.start()
        except RuntimeError as exc:
            self._turn_threads.pop(turn_id, None)
            managed.active_turn = ""
            self._publish_synthetic(turn_id, hub_session_id, "turn.failed",
                                    {"reason": type(exc).__name__})

    def _run_turn(self, turn_id: str, prompt: str, managed: _ManagedSession) -> None:
        self._store.set_turn(turn_id, "executing")
        try:
            from .hub_compute_broker import turn_tools
            with turn_tools(self, managed, turn_id, prompt) as tool_prompt:
                result = managed.session.prompt(tool_prompt)
            if not has_terminal_event(result):
                self._publish_synthetic(turn_id, managed.hub_id, "turn.failed",
                                        {"reason": "runtime_missing_terminal_event"})
        except Exception as exc:
            self._publish_synthetic(turn_id, managed.hub_id, "turn.failed",
                                    {"reason": type(exc).__name__})
        finally:
            managed.active_turn = ""
            self._turn_threads.pop(turn_id, None)

    def _on_event(self, managed: _ManagedSession, event: RuntimeEvent) -> None:
        if not isinstance(event, RuntimeEvent) or event.kind not in VISIBLE_EVENTS:
            return
        runtime_id = event.turn_id
        if event.kind == "turn.started" and managed.active_turn:
            if managed.runtime_turns is not None and runtime_id:
                managed.runtime_turns[runtime_id] = managed.active_turn
            self._store.set_turn(managed.active_turn, "executing", runtime_id)
            return
        turn_id = (managed.runtime_turns or {}).get(runtime_id, "")
        turn_id = turn_id or managed.active_turn
        if not turn_id:
            return
        for index, payload in enumerate(project_event_payload(event)):
            event_id = str(uuid.uuid5(
                EVENT_NAMESPACE,
                f"{self.instance_id}:{managed.local_id}:{event.event_id}:{index}"))
            body = {"event_id": event_id, "session_id": managed.hub_id,
                    "turn_id": turn_id, "kind": event.kind,
                    "payload": payload}
            self._store.enqueue_event(event_id=event_id, event=body,
                                      generation=self.generation)
        if event.kind in TERMINAL:
            self._store.set_turn(turn_id, TERMINAL[event.kind], runtime_id)
            if managed.runtime_turns is not None:
                managed.runtime_turns.pop(runtime_id, None)

    def _cancel_command(self, command: dict[str, Any],
                        managed: _ManagedSession) -> None:
        turn_id = field(command, "turn_id")
        if managed.active_turn == turn_id:
            try:
                managed.session.abort()
            except Exception as exc:
                log.info("Hub cancel unsupported (%s)", type(exc).__name__)
            return
        prior = self._store.turn(turn_id)
        if prior is None or prior["state"] in {"accepted", "starting"}:
            self._pending_starts.pop(turn_id, None)
            self._publish_synthetic(turn_id, managed.hub_id, "turn.interrupted",
                                    {"reason": "cancelled_before_start"})

    def _publish_synthetic(self, turn_id: str, session_id: str, kind: str,
                           payload: dict[str, Any]) -> None:
        if not session_id or not turn_id:
            return
        reason = str(payload.get("reason", ""))[:128]
        event_id = str(uuid.uuid5(EVENT_NAMESPACE,
                                  f"{self.instance_id}:{session_id}:{turn_id}:"
                                  f"{kind}:{reason}"))
        body = {"event_id": event_id, "session_id": session_id,
                "turn_id": turn_id, "kind": kind,
                "payload": redact_content({"reason": reason})}
        self._store.enqueue_event(event_id=event_id, event=body,
                                  generation=self.generation)
        self._store.set_turn(turn_id, TERMINAL.get(kind, "failed"))

    def _flush_outbox(self) -> None:
        deadline = time.monotonic() + 0.5
        sent = 0
        for row in self._store.pending_events():
            if sent >= 32 or time.monotonic() >= deadline:
                return
            event = row["body"]
            if (event.get("kind") == "turn.running"
                    and field(event, "turn_id") in self._pending_starts):
                continue
            try:
                response = self.client.send_event(
                    event, instance_id=self.instance_id,
                    generation=int(row["generation"]))
            except HubError as exc:
                if exc.code == "turn_terminal" and event.get("kind") == "turn.running":
                    turn_id = field(event, "turn_id")
                    self._store.obsolete_event(row["event_id"])
                    self._store.set_turn(turn_id, "cancelled")
                    return
                self._store.note_event_failure(row["event_id"])
                if is_fencing_error(exc):
                    self._fence_and_stop(exc)
                    raise
                return
            cursor = nested_text(response, "cursor")
            if not cursor:
                self._store.note_event_failure(row["event_id"])
                raise HubError("event acknowledgement omitted cursor",
                               code="invalid_response")
            self._store.mark_event_sent(row["event_id"], cursor)
            sent += 1

    def _handle_hub_error(self, error: HubError) -> None:
        if is_fencing_error(error):
            self._fence_and_stop(error)
            log.error("Hub Gateway fenced; stopping command execution")
        elif error.status in {401, 403}:
            log.error("Hub Gateway authorization rejected (%s)", error.code)
        else:
            log.warning("Hub Gateway request unavailable (%s)", error.code)

    def _fence_and_stop(self, error: HubError) -> None:
        if self._fatal is not None:
            return
        self._fatal = error
        self._stop.set()
        with self._session_lock:
            sessions = tuple(item.session for item in self._sessions.values())
        for session in sessions:
            try:
                session.abort()
            except Exception:
                continue

    def _managed(self, local_id: str) -> Optional[_ManagedSession]:
        with self._session_lock:
            return self._sessions.get(local_id)

    def _raise_if_fatal(self) -> None:
        if self._fatal is not None:
            raise RuntimeError("Hub Gateway lost its fencing generation") from self._fatal

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        with self._session_lock:
            managed = tuple(self._sessions.values())
            self._sessions.clear()
        for item in managed:
            try:
                item.session.close()
            except Exception:
                pass
        for thread in tuple(self._turn_threads.values()):
            thread.join(timeout=1.0)
        self._state_lock.close()
        self._store.close()

    def __enter__(self) -> "HubGateway":
        self.start()
        return self

    def __exit__(self, _kind, _value, _traceback) -> None:
        self.close()


__all__ = ["HubGateway", "HubGatewayConfig"]
