"""Contract tests for the bidirectional Codex App Server adapter."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import time
import unicodedata

import pytest

from pbatch.tui_adapter_appserver import (
    CodexAppServerAdapter,
    Transport,
)
from pbatch.tui_appserver_support import (
    app_server_argv, effective_policy_values, item_text, review_start_params,
    session_page, session_query_params,
)
from pbatch.tui_protocol import TurnState
from pbatch.tui_runtime import (
    ApprovalDecision,
    ArchiveSessionRequest,
    InitializeRequest,
    InterruptRequest,
    ListSessionsRequest,
    StartSessionRequest,
    StartTurnRequest,
    SteerRequest,
    SessionReconciliationError,
    UnsupportedOperation,
)
from pbatch.version import VERSION


def _read_line(fd: int, buffer: bytearray):
    while b"\n" not in buffer:
        try:
            chunk = os.read(fd, 8192)
        except OSError:
            return None
        if not chunk:
            return None
        buffer.extend(chunk)
    raw, _, rest = buffer.partition(b"\n")
    buffer[:] = rest
    return raw.decode("utf-8") + "\n"


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


class PipeTransport:
    """Client half of two cross-connected line-oriented pipes."""
    def __init__(self, read_fd: int, write_fd: int):
        self._read_fd, self._write_fd = read_fd, write_fd
        self._buffer = bytearray()
        self._lock, self._closed = threading.Lock(), False

    def send_line(self, line: str) -> None:
        with self._lock:
            if self._closed:
                raise EOFError("closed")
            os.write(self._write_fd, (line.rstrip("\r\n") + "\n").encode())

    def recv_line(self):
        return _read_line(self._read_fd, self._buffer)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            write_fd = self._write_fd
            self._write_fd = -1
        _close_fd(write_fd)

    def close_reader(self) -> None:
        with self._lock:
            read_fd, self._read_fd = self._read_fd, -1
        _close_fd(read_fd)


class FakeDaemon:
    """Scripted JSON-RPC daemon running over a real full-duplex socketpair."""
    def __init__(self):
        daemon_read, client_write = os.pipe()
        client_read, daemon_write = os.pipe()
        self.transport = PipeTransport(client_read, client_write)
        self._read_fd, self._write_fd = daemon_read, daemon_write
        self._buffer = bytearray()
        self._scripts: queue.Queue[tuple[str, object, tuple[dict, ...]]] = queue.Queue()
        self._write_lock = threading.Lock()
        self.received: queue.Queue[dict] = queue.Queue()
        self.notifications: queue.Queue[dict] = queue.Queue()
        self.responses: queue.Queue[dict] = queue.Queue()
        self.errors: queue.Queue[BaseException] = queue.Queue()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="fake-codex-daemon", daemon=True)
        self._thread.start()

    def expect(self, method: str, result=None, *, push: tuple[dict, ...] = ()) -> None:
        self._scripts.put((method, {} if result is None else result, push))

    def push_notification(self, method: str, params: dict) -> None:
        self._send({"method": method, "params": params})

    def push_request(self, request_id, method: str, params: dict) -> None:
        self._send({"id": request_id, "method": method, "params": params})

    def _send(self, value: dict) -> None:
        with self._write_lock:
            if self._closed or self._write_fd < 0:
                return
            os.write(self._write_fd, (json.dumps(value, ensure_ascii=False) + "\n").encode())

    def _close_output(self) -> None:
        with self._write_lock:
            write_fd, self._write_fd = self._write_fd, -1
        _close_fd(write_fd)

    def _run(self) -> None:
        try:
            while (line := _read_line(self._read_fd, self._buffer)) is not None:
                value = json.loads(line)
                if "method" not in value:
                    self.responses.put(value)
                    continue
                if "id" not in value:
                    self.notifications.put(value)
                    continue
                self.received.put(value)
                method, result, pushes = self._scripts.get(timeout=2)
                assert value["method"] == method
                for pushed in pushes:
                    self._send(pushed)
                self._send({"id": value["id"], "result": result})
        except (OSError, ValueError):
            pass
        except BaseException as exc:  # test harness must surface script failures
            self.errors.put(exc)
        finally:
            self._close_output()

    def disconnect(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_output()

    def close(self) -> None:
        self.disconnect()
        self.transport.close()
        self._thread.join(timeout=1)
        read_fd, self._read_fd = self._read_fd, -1
        _close_fd(read_fd)
        self.transport.close_reader()
        if not self.errors.empty():
            raise self.errors.get_nowait()


def _initialize(daemon: FakeDaemon, **capabilities):
    daemon.expect("initialize", {
        "protocolVersion": 1,
        "schemaVersion": "0.149.0",
        "serverInfo": {"name": "codex", "version": "0.149.0"},
        "capabilities": capabilities,
    })
    adapter = CodexAppServerAdapter("/tmp/project", transport=daemon.transport)
    info = adapter.initialize(InitializeRequest("ws_test"))
    return adapter, info


def _request(daemon: FakeDaemon, method: str) -> dict:
    value = daemon.received.get(timeout=1)
    assert value["method"] == method
    return value


def _notification(daemon: FakeDaemon, method: str) -> dict:
    value = daemon.notifications.get(timeout=1)
    assert value["method"] == method
    return value


def _event(stream, kind: str):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        value = next(stream)
        if value.kind == kind:
            return value
    raise AssertionError(f"event not observed: {kind}")


def _start_turn(daemon: FakeDaemon, adapter: CodexAppServerAdapter):
    daemon.expect("thread/start", {"thread": {"id": "thread-1"}})
    adapter.start_session(StartSessionRequest(workspace_id="ws_test"))
    daemon.expect("turn/start", {"turn": {"id": "provider-turn-1"}})
    return adapter.start_turn(StartTurnRequest(
        "hello", run_id="run-1", turn_id="turn-1"))


def _start_running_turn(daemon: FakeDaemon, adapter: CodexAppServerAdapter):
    handle = _start_turn(daemon, adapter)
    _request(daemon, "thread/start")
    _request(daemon, "turn/start")
    stream = adapter.events()
    daemon.push_notification("turn/started", {
        "threadId": "thread-1", "turn": {
            "id": "provider-turn-1", "status": "inProgress"}})
    _event(stream, "turn.started")
    return handle, stream


def test_initialize_handshake_and_capabilities_are_truthful():
    daemon = FakeDaemon()
    adapter = CodexAppServerAdapter("/tmp/project", transport=daemon.transport)
    assert all(not enabled for enabled in adapter.runtime_info().capabilities.capabilities.values())
    adapter.close()
    daemon.close()


def test_long_appserver_item_ids_keep_digest_identity():
    prefix = "x" * 2200
    first = item_text({"id": prefix + "-first", "text": "one"}, "text")
    second = item_text({"id": prefix + "-second", "text": "two"}, "text")

    assert first["item_id"] != second["item_id"]
    assert len(first["item_id"].encode("utf-8")) <= 2048
    assert len(second["item_id"].encode("utf-8")) <= 2048

    encoded_long_id = first["item_id"]
    mimicking_short = item_text(
        {"id": encoded_long_id, "text": "mimic"}, "text")
    assert mimicking_short["item_id"] != encoded_long_id
    assert item_text({"id": "", "text": "implicit"}, "text")["item_id"] == ""

    daemon = FakeDaemon()
    adapter, info = _initialize(
        daemon, review=True, subagents=True, images=True, instructionSources=True)
    request = _request(daemon, "initialize")
    assert request["params"]["clientInfo"] == {
        "name": "pi-batch", "title": "pi-batch", "version": VERSION}
    assert "protocolVersion" not in request["params"]
    assert _notification(daemon, "initialized")["params"] == {}
    enabled = info.capabilities
    for name in ("structured_events", "message_stream", "interrupt", "usage",
                 "session_resume", "session_read", "session_list", "session_fork",
                 "session_archive", "turn_steer",
                 "typed_approvals", "review", "subagents", "images",
                 "instruction_sources"):
        assert enabled.supports(name) is True
    assert enabled.message_queue is False
    assert adapter.server_info["server_version"] == "0.149.0"
    assert adapter.server_info["schema_version"] == "0.149.0"
    adapter.close()
    daemon.close()


def test_session_and_turn_request_shapes_and_handle_scoped_control():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    handle = _start_turn(daemon, adapter)
    start_session = _request(daemon, "thread/start")
    assert start_session["params"] == {"cwd": "/tmp/project"}
    start_turn = _request(daemon, "turn/start")
    assert start_turn["params"] == {
        "threadId": "thread-1", "input": [{"type": "text", "text": "hello"}]}

    before = daemon.received.qsize()
    with pytest.raises(ValueError, match="active turn"):
        adapter.steer_turn(SteerRequest("not-current", "do not send"))
    assert daemon.received.qsize() == before

    daemon.expect("turn/steer", {"turnId": "provider-turn-1"})
    adapter.steer_turn(SteerRequest(handle.turn_id, "more"))
    steer = _request(daemon, "turn/steer")
    assert steer["params"]["expectedTurnId"] == "provider-turn-1"
    assert steer["params"]["input"][0]["text"] == "more"

    daemon.expect("turn/interrupt", {})
    adapter.interrupt_turn(InterruptRequest(handle.turn_id))
    interrupt = _request(daemon, "turn/interrupt")
    assert interrupt["params"] == {
        "threadId": "thread-1", "turnId": "provider-turn-1"}
    adapter.close()
    daemon.close()


def test_model_override_is_forwarded_on_thread_and_turn_start():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    daemon.expect("thread/start", {"thread": {"id": "thread-model"}})
    adapter.start_session({"model": "gpt-model-thread"})
    assert _request(daemon, "thread/start")["params"] == {
        "cwd": "/tmp/project", "model": "gpt-model-thread"}

    daemon.expect("turn/start", {"turn": {"id": "provider-turn-model"}})
    adapter.start_turn({"prompt": "hello", "model": "gpt-model-turn"})
    assert _request(daemon, "turn/start")["params"] == {
        "threadId": "thread-model",
        "input": [{"type": "text", "text": "hello"}],
        "model": "gpt-model-turn",
    }
    adapter.close()
    daemon.close()


def test_resume_fork_and_list_sessions_use_provider_thread_ids():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    daemon.expect("thread/read", {"thread": {
        "id": "thread-parent", "status": {"type": "idle"}}})
    daemon.expect("thread/resume", {"thread": {"id": "thread-parent"}})
    resumed = adapter.resume_session({"provider_session_id": "thread-parent"})
    assert resumed.provider_session_id == "thread-parent"
    assert _request(daemon, "thread/read")["params"] == {
        "threadId": "thread-parent", "includeTurns": False}
    assert _request(daemon, "thread/resume")["params"] == {"threadId": "thread-parent"}
    daemon.expect("thread/fork", {"thread": {"id": "thread-child"}})
    forked = adapter.fork_session({"provider_session_id": "thread-parent"})
    assert forked.provider_session_id == "thread-child"
    assert forked.session_id != resumed.session_id
    _request(daemon, "thread/fork")
    daemon.expect("thread/list", {"data": [{
        "id": "thread-child", "name": "Branch",
        "status": {"type": "idle"}, "updatedAt": 42}],
        "nextCursor": "next-page"})
    page = adapter.list_sessions({"limit": 10})
    assert len(page.sessions) == 1
    assert page.sessions[0].provider_session_id == "thread-child"
    assert page.sessions[0].name == "Branch"
    assert page.sessions[0].status == "idle"
    assert page.sessions[0].updated_at == 42
    assert page.next_cursor == "next-page"
    assert _request(daemon, "thread/list")["params"] == {
        "limit": 10,
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "sourceKinds": ["cli", "vscode", "exec", "appServer"],
        "archived": False,
        "cwd": os.path.realpath("/tmp/project"),
        "useStateDbOnly": True,
    }
    adapter.close()
    daemon.close()


def test_provider_archive_uses_exact_thread_methods_and_blocks_active_thread():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    daemon.expect("thread/start", {"thread": {"id": "thread-current"}})
    adapter.start_session({"session_id": "local-current"})
    _request(daemon, "thread/start")

    with pytest.raises(RuntimeError, match="active provider session"):
        adapter.archive_session(ArchiveSessionRequest("thread-current"))
    with pytest.raises(queue.Empty):
        daemon.received.get(timeout=0.05)

    daemon.expect("thread/archive", {})
    adapter.archive_session(ArchiveSessionRequest(
        "thread-history", session_id="local-history", workspace_id="ws_test"))
    archive = _request(daemon, "thread/archive")
    assert archive["params"] == {"threadId": "thread-history"}

    daemon.expect("thread/unarchive", {"thread": {"id": "thread-history"}})
    adapter.unarchive_session(ArchiveSessionRequest(
        "thread-history", session_id="local-history", workspace_id="ws_test"))
    restore = _request(daemon, "thread/unarchive")
    assert restore["params"] == {"threadId": "thread-history"}
    adapter.close()
    daemon.close()


def test_provider_archive_rejects_scope_and_malformed_unarchive_confirmation():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    with pytest.raises(ValueError, match="workspace"):
        adapter.archive_session({
            "provider_session_id": "thread-history", "workspace_id": "other"})
    with pytest.raises(ValueError, match="provider_session_id"):
        adapter.archive_session({})
    daemon.expect("thread/unarchive", {"thread": {"id": "other"}})
    with pytest.raises(RuntimeError, match="did not confirm"):
        adapter.unarchive_session({"provider_session_id": "thread-history"})
    _request(daemon, "thread/unarchive")
    daemon.expect("thread/archive", {})
    adapter.archive_session({"provider_session_id": "thread-history"})
    _request(daemon, "thread/archive")
    adapter.close()
    daemon.close()


def test_read_session_is_non_subscribing_and_preserves_structured_status():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    original = adapter.session
    daemon.expect("thread/read", {"thread": {
        "id": "thread-observed", "name": "Background task",
        "status": {"type": "active", "activeFlags": ["waitingOnApproval"]}}})

    observed = adapter.read_session({"provider_session_id": "thread-observed"})

    assert observed.provider_session_id == "thread-observed"
    assert observed.status == "active"
    assert observed.active_flags == ("waitingOnApproval",)
    assert observed.name == "Background task"
    assert adapter.session is original
    assert _request(daemon, "thread/read")["params"] == {
        "threadId": "thread-observed", "includeTurns": False}
    with pytest.raises(queue.Empty):
        adapter.events().get(timeout=0)
    adapter.close()
    daemon.close()


def test_session_query_is_workspace_scoped_and_rejects_unbounded_fields():
    params = session_query_params(ListSessionsRequest(cursor="page-two"), "/tmp/../tmp/project")
    assert params == {
        "limit": 50,
        "sortKey": "updated_at",
        "sortDirection": "desc",
        "sourceKinds": ["cli", "vscode", "exec", "appServer"],
        "archived": False,
        "cwd": os.path.realpath("/tmp/project"),
        "useStateDbOnly": True,
        "cursor": "page-two",
    }

    invalid = [
        {"cwd": "/outside"},
        {1: "mixed-key"},
        {"limit": 0},
        {"limit": True},
        {"limit": 101},
        {"sort_key": "future"},
        {"sort_direction": "sideways"},
        {"source_kinds": ["future"]},
        {"source_kinds": "appServer"},
        {"archived": 1},
        {"is_pinned": "yes"},
        {"cursor": "bad\nvalue"},
        {"search_term": 42},
    ]
    for query in invalid:
        with pytest.raises(ValueError):
            session_query_params(query, "/tmp/project")
    with pytest.raises(TypeError, match="ListSessionsRequest"):
        session_query_params(object(), "/tmp/project")


def test_review_start_params_match_the_app_server_target_contract():
    assert review_start_params({"scope": "default"}, "thread-1") == {
        "threadId": "thread-1", "target": {"type": "uncommittedChanges"}}
    assert review_start_params({
        "scope": "uncommitted", "scope_argv": ("--uncommitted",),
    }, "thread-1") == {
        "threadId": "thread-1", "target": {"type": "uncommittedChanges"}}
    assert review_start_params({
        "scope": "base:main", "scope_argv": ("--base", "main"),
    }, "thread-1") == {
        "threadId": "thread-1",
        "target": {"type": "baseBranch", "branch": "main"},
    }
    assert review_start_params({"scope": "commit:abc123"}, "thread-1") == {
        "threadId": "thread-1",
        "target": {"type": "commit", "sha": "abc123"},
    }
    assert review_start_params({
        "scope": "custom", "scope_argv": ("inspect retries",),
    }, "thread-1") == {
        "threadId": "thread-1",
        "target": {"type": "custom", "instructions": "inspect retries"},
    }
    assert review_start_params({
        "target": {"type": "commit", "sha": "abc", "title": None},
        "delivery": "inline",
    }, "thread-1") == {
        "threadId": "thread-1", "delivery": "inline",
        "target": {"type": "commit", "sha": "abc", "title": None},
    }
    invalid = [
        {"scope": "base:"}, {"scope": "commit:"},
        {"scope": "custom", "scope_argv": ()},
        {"scope": "future"}, {"scope": "uncommitted", "scope_argv": ("x",)},
        {"target": {"type": "custom", "instructions": "x", "extra": 1}},
        {"target": {"type": "uncommittedChanges", "branch": "main"}},
        {"delivery": "detached", "scope": "default"},
    ]
    for request in invalid:
        with pytest.raises(ValueError):
            review_start_params(request, "thread-1")


def test_session_page_preserves_advisory_fields_and_skips_malformed_items():
    page = session_page({
        "data": [
            {
                "id": "thread-good",
                "name": "N" * 5_000,
                "preview": "Build the picker",
                "ephemeral": False,
                "isPinned": True,
                "modelProvider": "openai",
                "createdAt": 10,
                "updatedAt": 20,
                "status": {"type": "active", "activeFlags": ["waitingOnApproval"]},
            },
            {"id": "thread-bad", "status": {"type": "future"}},
            {"id": "thread-good", "status": {"type": "idle"}},
        ],
        "nextCursor": "more",
    }, limit=10)

    assert len(page.sessions) == 1
    summary = page.sessions[0]
    assert summary.provider_session_id == "thread-good"
    assert summary.name.endswith("...[truncated]")
    assert summary.preview == "Build the picker"
    assert summary.active_flags == ("waitingOnApproval",)
    assert summary.model_provider == "openai"
    assert summary.created_at == 10 and summary.updated_at == 20
    assert summary.ephemeral is False and summary.is_pinned is True
    assert page.next_cursor == "more"
    assert len(page.warnings) == 2

    with pytest.raises(ValueError, match="data list"):
        session_page({}, limit=10)
    with pytest.raises(ValueError, match="nextCursor"):
        session_page({"data": []}, limit=10)
    with pytest.raises(ValueError, match="nextCursor"):
        session_page({"data": [], "nextCursor": "bad\nvalue"}, limit=10)

    invalid_statuses = session_page({
        "data": [
            {"id": "thread-idle-flags", "status": {
                "type": "idle", "activeFlags": []}},
            {"id": "thread-list-type", "status": {
                "type": ["idle"]}},
        ],
        "nextCursor": None,
    }, limit=10)
    assert invalid_statuses.sessions == ()
    assert len(invalid_statuses.warnings) == 2


def test_session_page_sanitizes_unicode_controls_and_surrogates():
    page = session_page({
        "data": [{
            "id": "thread-display-safe",
            "name": "A\x1b[31mB\x1b[0m\u009bC\u202eD\ud800E",
            "preview": "line-one\nline-two\u2066isolated",
            "modelProvider": "openai\u200dprovider",
            "status": {"type": "idle"},
        }],
        "nextCursor": None,
    }, limit=10)

    summary = page.sessions[0]
    for value in (summary.name, summary.preview, summary.model_provider):
        value.encode("utf-8", errors="strict")
        assert not any(unicodedata.category(char) in {"Cc", "Cf", "Cs"}
                       for char in value)
    assert "[31m" not in summary.name


@pytest.mark.parametrize(("listed_status", "read_status", "blocked"), [
    ({"type": "idle"}, {"type": "active", "activeFlags": []}, True),
    ({"type": "active", "activeFlags": []}, {"type": "idle"}, False),
])
def test_thread_list_status_never_replaces_resume_time_read_gate(
        listed_status, read_status, blocked):
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    daemon.expect("thread/list", {"data": [{
        "id": "thread-racy", "status": listed_status}],
        "nextCursor": None})
    page = adapter.list_sessions({"limit": 1})
    _request(daemon, "thread/list")
    assert page.sessions[0].status == listed_status["type"]
    daemon.expect("thread/read", {"thread": {
        "id": "thread-racy", "status": read_status}})
    if blocked:
        with pytest.raises(SessionReconciliationError, match="active"):
            adapter.resume_session({"provider_session_id": "thread-racy"})
    else:
        daemon.expect("thread/resume", {"thread": {"id": "thread-racy"}})
        resumed = adapter.resume_session({"provider_session_id": "thread-racy"})
        assert resumed.provider_session_id == "thread-racy"
    _request(daemon, "thread/read")
    if not blocked:
        _request(daemon, "thread/resume")
    adapter.close()
    daemon.close()


@pytest.mark.parametrize(("status", "expected"), [
    ({"type": "active", "activeFlags": []}, "active"),
    ({"type": "systemError"}, "systemError"),
    ({"type": "idle", "activeFlags": []}, "unverified"),
    ({"type": ["idle"]}, "unverified"),
    ({"type": "future"}, "unverified"),
    ("idle", "unverified"),
])
def test_resume_reconciles_provider_status_before_adopting_thread(status, expected):
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    original = adapter.session
    daemon.expect("thread/read", {"thread": {
        "id": "thread-blocked", "status": status}})

    with pytest.raises(SessionReconciliationError) as error:
        adapter.resume_session({"provider_session_id": "thread-blocked"})

    assert error.value.status == expected
    assert adapter.session is original
    assert _request(daemon, "thread/read")["params"]["includeTurns"] is False
    assert daemon.received.empty()
    adapter.close()
    daemon.close()


def test_steer_requires_response_to_confirm_the_active_provider_turn():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    handle = _start_turn(daemon, adapter)
    _request(daemon, "thread/start")
    _request(daemon, "turn/start")
    stream = adapter.events()
    daemon.expect("turn/steer", {"turnId": "different-turn"})

    with pytest.raises(RuntimeError, match="did not confirm"):
        adapter.steer_turn(SteerRequest(handle.turn_id, "more"))

    _request(daemon, "turn/steer")
    with pytest.raises(queue.Empty):
        stream.get(timeout=0)
    adapter.close()
    daemon.close()


def test_explicit_provider_root_authority_is_projected_as_metadata():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon, subagents=True)
    _request(daemon, "initialize")
    handle = _start_turn(daemon, adapter)
    _request(daemon, "thread/start")
    _request(daemon, "turn/start")
    stream = adapter.events()
    daemon.push_notification("turn/started", {"threadId": "thread-1", "turn": {
        "id": "provider-turn-1", "status": "inProgress",
        "actorAuthority": {"state": "declared",
                            "capabilities": ["actor_control"],
                            "scope": "workspace"}}})
    event = _event(stream, "turn.started")
    assert event.actor_id == handle.actor_id
    assert event.payload["authority"] == {
        "state": "DECLARED", "source_actor_id": "",
        "capabilities": ["actor_control"], "scope": "workspace"}
    adapter.close()
    daemon.close()


def test_explicit_provider_actor_items_project_scoped_lineage():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon, subagents=True)
    _request(daemon, "initialize")
    handle, stream = _start_running_turn(daemon, adapter)
    daemon.push_notification("item/started", {"item": {
        "id": "sub-1", "type": "subAgent", "actorId": "child-1",
        "parentActorId": "agent_main", "name": "security review",
        "role": "reviewer", "provider": "codex",
        "actorAuthority": {"state": "inherited",
                            "source_actor_id": "agent_main",
                            "capabilities": ["read"], "scope": "workspace"}}})
    spawned = _event(stream, "actor.spawned")
    assert spawned.actor_id == "agent_main"
    assert spawned.payload["actor_id"] == "child-1"
    assert spawned.payload["parent_actor_id"] == handle.actor_id
    assert spawned.payload["role"] == "reviewer"
    assert spawned.payload["authority"] == {
        "state": "INHERITED", "source_actor_id": "agent_main",
        "capabilities": ["read"], "scope": "workspace"}

    daemon.push_notification("item/started", {"item": {
        "id": "sub-2", "type": "subAgent", "actorId": "child-2",
        "parentActorId": "child-1", "role": "qa"}})
    nested = _event(stream, "actor.spawned")
    assert nested.payload["parent_actor_id"] == "child-1"

    daemon.push_notification("item/completed", {"item": {
        "id": "sub-1", "type": "subAgent", "actorId": "child-1",
        "parentActorId": "agent_main", "success": True}})
    completed = _event(stream, "actor.completed")
    assert completed.payload == {
        "actor_id": "child-1", "parent_actor_id": "agent_main",
        "success": True,
    }
    adapter.close()
    daemon.close()


def test_provider_actor_fields_without_declared_subagents_stay_non_actor():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    _handle, stream = _start_running_turn(daemon, adapter)
    daemon.push_notification("item/started", {"item": {
        "type": "subAgent", "actorId": "child-1",
        "parentActorId": "agent_main"}})
    warning = _event(stream, "runtime.warning")
    assert warning.payload["category"] == "provider_actor"
    assert stream.get(timeout=1).kind == "runtime.unknown_item"
    with pytest.raises(queue.Empty):
        stream.get(timeout=0.05)
    adapter.close()
    daemon.close()


def test_notification_mapping_bounding_and_terminal_singleton(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime-data"))
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    handle = _start_turn(daemon, adapter)
    _request(daemon, "thread/start")
    _request(daemon, "turn/start")
    stream = adapter.events()
    daemon.push_notification("turn/started", {
        "threadId": "thread-1", "turn": {"id": "provider-turn-1", "status": "inProgress"}})
    daemon.push_notification("item/started", {"item": {
        "id": "m1", "type": "agentMessage", "text": ""}})
    daemon.push_notification("item/agentMessage/delta", {
        "itemId": "m1", "delta": "partial"})
    daemon.push_notification("item/completed", {"item": {
        "id": "m1", "type": "agentMessage", "text": "complete"}})
    huge = "x" * (128 * 1024)
    daemon.push_notification("item/completed", {"item": {
        "id": "c1", "type": "commandExecution", "command": "big",
        "aggregatedOutput": huge, "exitCode": 7}})
    daemon.push_notification("thread/tokenUsage/updated", {
        "tokenUsage": {"total": {"inputTokens": 11, "outputTokens": 3}}})
    completed = {"threadId": "thread-1", "turn": {
        "id": "provider-turn-1", "status": "completed"}}
    daemon.push_notification("turn/completed", completed)
    daemon.push_notification("turn/completed", completed)
    daemon.push_notification("warning", {"message": "ordering barrier"})

    events = []
    while not handle.done or not any(event.kind == "turn.completed" for event in events):
        events.append(next(stream))
    assert _event(stream, "runtime.warning").payload["message"] == "ordering barrier"
    assert [event.kind for event in events[:4]] == [
        "turn.queued", "turn.started", "message.started", "message.delta"]
    assert any(event.kind == "message.completed" and
               event.payload["text"] == "complete" for event in events)
    command = next(event for event in events if event.kind == "command.completed")
    assert command.payload["exit_code"] == 7
    assert command.payload["truncated"] is True
    assert command.payload["aggregated_output"].endswith("...[truncated]")
    artifact = next(event for event in events if event.kind == "artifact.created")
    assert artifact.payload["scope"] == "data"
    assert artifact.payload["original_bytes"] == len(huge)
    stored = Path(tmp_path / "runtime-data" / artifact.payload["path"])
    assert stored.read_text() == huge
    assert any(event.kind == "usage.updated" for event in events)
    assert sum(event.kind == "turn.completed" for event in events) == 1
    assert [event.seq for event in events] == list(range(1, len(events) + 1))
    daemon.disconnect()
    assert [event.kind for event in stream] == ["runtime.error"]
    assert handle.state == "SUCCEEDED"
    assert all(not value for value in adapter.runtime_info().capabilities.capabilities.values())
    adapter.close()
    daemon.close()


def test_command_output_deltas_finalize_private_artifact_without_aggregate(
        tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime-data"))
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    handle = _start_turn(daemon, adapter)
    _request(daemon, "thread/start")
    _request(daemon, "turn/start")
    stream = adapter.events()
    daemon.push_notification("turn/started", {
        "threadId": "thread-1", "turn": {"id": "provider-turn-1"}})
    daemon.push_notification("item/started", {"item": {
        "id": "streamed", "type": "commandExecution", "command": "build"}})
    chunks = [f"{index}:" + "x" * 20_000 for index in range(4)]
    for chunk in chunks:
        daemon.push_notification("item/commandExecution/outputDelta", {
            "itemId": "streamed", "delta": chunk})
    daemon.push_notification("item/completed", {"item": {
        "id": "streamed", "type": "commandExecution",
        "command": "build", "exitCode": 0}})
    daemon.push_notification("turn/completed", {
        "threadId": "thread-1", "turn": {
            "id": "provider-turn-1", "status": "completed"}})

    events = []
    while not handle.done or not any(event.kind == "turn.completed" for event in events):
        events.append(next(stream))
    artifact = next(event for event in events if event.kind == "artifact.created")
    stored = Path(tmp_path / "runtime-data" / artifact.payload["path"])
    assert stored.read_text() == "".join(chunks)
    assert artifact.payload["original_bytes"] == len("".join(chunks))
    assert events.index(artifact) < next(
        index for index, event in enumerate(events)
        if event.kind == "command.completed")
    adapter.close()
    daemon.close()


@pytest.mark.parametrize(("provider_status", "expected_kind", "expected_state"), [
    ("completed", "turn.completed", "SUCCEEDED"),
    ("failed", "turn.failed", "FAILED"),
    ("interrupted", "turn.interrupted", "INTERRUPTED"),
    ("futureStatus", "turn.failed", "UNKNOWN"),
    (None, "turn.failed", "UNKNOWN"),
])
def test_turn_completed_notification_preserves_provider_terminal_status(
        provider_status, expected_kind, expected_state):
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    handle = _start_turn(daemon, adapter)
    _request(daemon, "thread/start")
    _request(daemon, "turn/start")
    stream = adapter.events()
    daemon.push_notification("turn/started", {
        "threadId": "thread-1", "turn": {
            "id": "provider-turn-1", "status": "inProgress"}})
    _event(stream, "turn.started")
    error = ({"message": "provider exploded", "codexErrorInfo": {"kind": "Other"}}
             if provider_status == "failed" else None)
    if error:
        daemon.push_notification("error", {"error": error})
        diagnostic = _event(stream, "runtime.error")
        assert diagnostic.payload["message"] == "provider exploded"
        assert handle.done is False
    turn = {"id": "provider-turn-1", "error": error}
    if provider_status is not None:
        turn["status"] = provider_status
    daemon.push_notification("turn/completed", {
        "threadId": "thread-1", "turn": turn})
    terminal = _event(stream, expected_kind)
    assert terminal.payload["state"] == expected_state
    assert terminal.payload["provider_status"] == (provider_status or "")
    assert handle.state == expected_state
    assert handle.done is True
    if provider_status == "failed":
        assert terminal.payload["reason"] == "provider exploded"
    if expected_state == "UNKNOWN":
        assert terminal.payload["reason"].endswith(provider_status or "<missing>")
    adapter.close()
    daemon.close()


def test_effective_policy_values_are_explicit_and_hide_structured_data():
    assert effective_policy_values({"approvalPolicy": "on-request",
                                    "sandboxPolicy": {"mode": "workspace"},
                                    "networkAccess": False}) == {
        "approval": "on-request", "sandbox": "present", "network": "disabled"}
    assert effective_policy_values({}) == {}


def test_app_server_retains_policy_values_from_thread_start():
    daemon = FakeDaemon()
    adapter, _info = _initialize(daemon)
    daemon.expect("thread/start", {
        "thread": {"id": "thread-policy", "approvalPolicy": "on-request",
                    "sandboxPolicy": "workspace-write", "networkAccess": True}})
    adapter.start_session(StartSessionRequest(workspace_id="ws_test"))
    assert adapter.policy_state == {
        "approval": "on-request", "sandbox": "workspace-write", "network": "enabled"}
    adapter.close()
    daemon.close()


def test_turn_started_emits_typed_effective_policy_snapshot():
    daemon = FakeDaemon()
    adapter, _info = _initialize(daemon)
    _request(daemon, "initialize")
    daemon.expect("thread/start", {
        "thread": {"id": "thread-policy", "approvalPolicy": "on-request",
                    "sandboxPolicy": "workspace-write", "networkAccess": True}})
    adapter.start_session(StartSessionRequest(workspace_id="ws_test"))
    _request(daemon, "thread/start")
    daemon.expect("turn/start", {"turn": {"id": "provider-turn-1"}})
    handle = adapter.start_turn(StartTurnRequest(
        "hello", run_id="run-1", turn_id="turn-1"))
    _request(daemon, "turn/start")
    stream = adapter.events()
    daemon.push_notification("turn/started", {
        "threadId": "thread-policy", "turn": {
            "id": "provider-turn-1", "status": "inProgress"}})

    _event(stream, "turn.started")
    policy = _event(stream, "policy.updated")

    assert policy.payload == {
        "approval": "on-request", "sandbox": "workspace-write",
        "network": "enabled"}
    assert policy.turn_id == handle.turn_id
    adapter.close()
    daemon.close()


def test_approval_round_trip_unknown_and_type_mismatch_are_fail_closed():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    _handle, stream = _start_running_turn(daemon, adapter)
    daemon.push_request("approve-1", "execCommandApproval", {
        "threadId": "thread-1", "turnId": "provider-turn-1",
        "command": ["git", "status"], "cwd": "/tmp/project"})
    requested = _event(stream, "approval.requested")
    assert requested.payload["request_id"] == "approve-1"
    assert requested.payload["approval_type"] == "exec_command"
    assert adapter.resolve_approval(ApprovalDecision("approve-1", "allow")) is True
    response = daemon.responses.get(timeout=1)
    assert response == {"id": "approve-1", "result": {"decision": "approved"}}
    resolved = _event(stream, "approval.resolved")
    assert resolved.payload["request_id"] == "approve-1"

    assert adapter.resolve_approval(ApprovalDecision("unknown", "allow")) is False
    assert adapter.resolve_approval({
        "request_id": "approve-1", "decision": "allow",
        "approval_type": "file_change"}) is False
    with pytest.raises(queue.Empty):
        daemon.responses.get(timeout=0.05)
    adapter.close()
    daemon.close()
    daemon.close()


def test_disconnect_invalidates_pending_approval_then_ends_event_stream():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    _handle, stream = _start_running_turn(daemon, adapter)
    daemon.push_request(9, "item/fileChange/requestApproval", {
        "threadId": "thread-1", "turnId": "provider-turn-1", "changes": ["x.py"]})
    assert _event(stream, "approval.requested").payload["request_id"] == 9
    daemon.disconnect()
    tail = list(stream)
    assert [event.kind for event in tail] == ["approval.expired", "runtime.error"]
    assert tail[0].payload["target"] == 'changes: ["x.py"]'
    assert tail[0].payload["reason"] == "connection lost, approval invalidated"
    assert tail[1].payload["message"] == "connection lost, approval invalidated"
    assert adapter.resolve_approval(ApprovalDecision(9, "allow")) is False
    assert all(not value for value in adapter.runtime_info().capabilities.capabilities.values())
    with pytest.raises(queue.Empty):
        daemon.responses.get(timeout=0.05)
    adapter.close()


def test_appserver_review_start_uses_typed_target_and_projects_findings():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon, review=True)
    _request(daemon, "initialize")
    daemon.expect("thread/start", {"thread": {"id": "thread-1"}})
    adapter.start_session(StartSessionRequest(workspace_id="ws_test"))
    _request(daemon, "thread/start")
    with pytest.raises(ValueError, match="session_id"):
        adapter.start_review({"scope": "default", "session_id": "other-session"})
    assert daemon.received.empty()
    daemon.expect("review/start", {
        "reviewThreadId": "thread-1",
        "turn": {"id": "provider-review-1", "status": "inProgress"},
    })
    handle = adapter.start_review({"scope": "base:main", "run_id": "review-run",
                                  "turn_id": "review-turn"})
    request = _request(daemon, "review/start")
    assert request["params"] == {
        "threadId": "thread-1",
        "target": {"type": "baseBranch", "branch": "main"},
    }
    assert handle.session_id == adapter.session.session_id
    assert handle.run_id == "review-run" and handle.turn_id == "review-turn"

    stream = adapter.events()
    daemon.push_notification("turn/started", {
        "threadId": "thread-1", "turn": {
            "id": "provider-review-1", "status": "inProgress"}})
    daemon.push_notification("item/completed", {"item": {
        "id": "review-message", "type": "agentMessage",
        "text": ('```json\\n[{"severity":"high","file":"src/a.py",'
                 '"line":7,"problem":"unsafe input"}]\\n```')}})
    daemon.push_notification("turn/completed", {
        "threadId": "thread-1", "turn": {
            "id": "provider-review-1", "status": "completed"}})
    events = []
    while not handle.done or not any(event.kind == "turn.completed" for event in events):
        events.append(next(stream))
    assert any(event.kind == "review.finding" and
               event.payload["file"] == "src/a.py" for event in events)
    assert events[-1].kind == "turn.completed"
    assert handle.state == TurnState.SUCCEEDED
    adapter.close()
    daemon.close()


def test_ttl_unknown_notification_and_unsupported_review():
    daemon = FakeDaemon()
    daemon.expect("initialize", {"protocolVersion": 1, "capabilities": {"review": False}})
    adapter = CodexAppServerAdapter(
        "/tmp/project", transport=daemon.transport, approval_ttl=0.05)
    adapter.initialize(InitializeRequest())
    _request(daemon, "initialize")
    _handle, stream = _start_running_turn(daemon, adapter)
    daemon.push_request("short", "item/commandExecution/requestApproval", {
        "threadId": "thread-1", "turnId": "provider-turn-1",
        "command": "echo hi"})
    _event(stream, "approval.requested")
    expired = _event(stream, "approval.expired")
    assert expired.payload["request_id"] == "short"
    assert expired.payload["reason"] == "approval TTL expired"
    assert daemon.responses.get(timeout=1) == {
        "id": "short", "result": {"decision": "decline"}}
    assert adapter.resolve_approval(ApprovalDecision("short", "accept")) is False

    daemon.push_notification("future/notification", {"large": "z" * (128 * 1024)})
    unknown = _event(stream, "runtime.unknown_item")
    assert unknown.payload["method"] == "future/notification"
    assert len(json.dumps(unknown.payload).encode()) <= 64 * 1024
    with pytest.raises(UnsupportedOperation, match="review/start"):
        adapter.start_review({})
    adapter.close()
    daemon.close()


def test_approval_scope_network_target_and_raw_result_are_fail_closed():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    _handle, stream = _start_running_turn(daemon, adapter)
    daemon.push_request("network-1", "item/commandExecution/requestApproval", {
        "threadId": "thread-1", "turnId": "provider-turn-1",
        "command": "curl hidden.example",
        "networkApprovalContext": {"host": "hidden.example", "port": 443},
        "availableDecisions": ["accept", "decline"],
    })

    requested = _event(stream, "approval.requested")
    assert requested.payload["target"].startswith("network:")
    assert requested.payload["available_decisions"] == ["allow", "deny"]
    assert adapter.resolve_approval({
        "request_id": "network-1", "decision": "allow",
        "turn_id": "another-turn"}) is False
    assert adapter.resolve_approval({
        "request_id": "network-1", "decision": "allow_session"}) is False
    assert adapter.resolve_approval({
        "request_id": "network-1",
        "result": {"decision": "accept"}}) is False
    with pytest.raises(queue.Empty):
        daemon.responses.get(timeout=0.05)

    assert adapter.resolve_approval({
        "request_id": "network-1", "decision": "deny",
        "workspace_id": "ws_test", "session_id": adapter.session.session_id,
        "run_id": "run-1", "turn_id": "turn-1", "actor_id": "agent_main",
    }) is True
    assert daemon.responses.get(timeout=1) == {
        "id": "network-1", "result": {"decision": "decline"}}
    adapter.close()
    daemon.close()


def test_pending_approval_limit_declines_excess_request():
    daemon = FakeDaemon()
    daemon.expect("initialize", {"protocolVersion": 1, "capabilities": {}})
    adapter = CodexAppServerAdapter(
        "/tmp/project", transport=daemon.transport, max_pending_approvals=1)
    adapter.initialize(InitializeRequest("ws_test"))
    _request(daemon, "initialize")
    _handle, stream = _start_running_turn(daemon, adapter)
    scope = {"threadId": "thread-1", "turnId": "provider-turn-1"}
    daemon.push_request("first", "item/fileChange/requestApproval", {
        **scope, "changes": ["first.py"]})
    assert _event(stream, "approval.requested").payload["request_id"] == "first"

    daemon.push_request("excess", "item/fileChange/requestApproval", {
        **scope, "changes": ["excess.py"]})
    warning = _event(stream, "runtime.warning")
    assert warning.payload["message"] == "pending approval limit reached"
    assert daemon.responses.get(timeout=1) == {
        "id": "excess", "result": {"decision": "decline"}}
    assert [entry.request_id for entry in adapter.pending_approvals] == ["first"]
    adapter.close()
    daemon.close()


def test_approval_without_active_turn_is_declined_not_projected():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    stream = adapter.events()

    daemon.push_request("unscoped", "item/commandExecution/requestApproval", {
        "command": "echo unsafe"})
    warning = _event(stream, "runtime.warning")
    assert warning.payload["message"] == "approval rejected outside an active turn"
    assert daemon.responses.get(timeout=1) == {
        "id": "unscoped", "result": {"decision": "decline"}}
    assert adapter.pending_approvals == ()
    with pytest.raises(queue.Empty):
        stream.get(timeout=0)
    adapter.close()
    daemon.close()


def test_close_declines_outstanding_approval_before_transport_shutdown():
    daemon = FakeDaemon()
    adapter, _ = _initialize(daemon)
    _request(daemon, "initialize")
    _handle, stream = _start_running_turn(daemon, adapter)
    daemon.push_request("close-me", "item/fileChange/requestApproval", {
        "threadId": "thread-1", "turnId": "provider-turn-1",
        "changes": ["close.py"]})
    _event(stream, "approval.requested")

    adapter.close()

    assert daemon.responses.get(timeout=1) == {
        "id": "close-me", "result": {"decision": "decline"}}
    daemon.close()


def test_initialize_eof_is_structured_unavailability():
    daemon = FakeDaemon()
    adapter = CodexAppServerAdapter("/tmp/project", transport=daemon.transport)
    daemon.disconnect()
    with pytest.raises(RuntimeError, match="^codex app-server unavailable: connection lost"):
        adapter.initialize(InitializeRequest())
    assert all(not enabled for enabled in adapter.runtime_info().capabilities.capabilities.values())
    daemon.close()


def test_transport_protocol_is_runtime_checkable():
    daemon = FakeDaemon()
    assert isinstance(daemon.transport, Transport)
    daemon.close()


def test_default_transport_owns_stdio_server_and_proxy_requires_socket():
    assert app_server_argv() == ["codex", "app-server"]
    assert app_server_argv("/tmp/codex.sock") == [
        "codex", "app-server", "proxy", "--sock", "/tmp/codex.sock"]
