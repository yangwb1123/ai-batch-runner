"""One bounded creation worker; provider initialization never blocks Hub traffic."""

from __future__ import annotations

import threading

from .hub_client import HubError
from .hub_gateway_config import session_options
from .hub_gateway_protocol import nested_int
from .hub_session_client import session_name


def _invalid() -> HubError:
    return HubError("Hub session creation response is invalid", code="invalid_response")


def _text(value, maximum=256):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and len(value) <= maximum
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _command(value, gateway):
    keys = {"request_id", "instance_id", "generation", "name", "project_id", "local_session_id"}
    if not isinstance(value, dict) or set(value) != keys:
        raise _invalid()
    if any(not _text(value[key]) for key in ("request_id", "instance_id", "local_session_id")):
        raise _invalid()
    try:
        if session_name(value["name"]) != value["name"]:
            raise _invalid()
    except (ValueError, UnicodeError):
        raise _invalid() from None
    if not isinstance(value["project_id"], str) or len(value["project_id"]) > 256:
        raise _invalid()
    if value["instance_id"] != gateway.instance_id or value["project_id"] != gateway.options.project_id:
        raise _invalid()
    if type(value["generation"]) is not int or value["generation"] != gateway.generation:
        raise HubError("Hub session creation generation changed", code="stale_generation", status=409)
    return dict(value)


class SessionCreation:
    """Only this worker constructs requested sessions or reports their durable result."""

    def __init__(self, gateway):
        self.gateway = gateway
        self._commands = {}
        self._sessions = {}
        self._thread = None
        self._wake = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="hub-session-create", daemon=True)
        self._thread.start()

    def publish_capacity(self):
        gateway = self.gateway
        gateway._ensure_running()
        response = gateway.client.register_instance(
            instance_id=gateway.instance_id, boot_id=gateway._boot_id,
            name=gateway.options.name, project_id=gateway.options.project_id,
            session_capacity=gateway.options.max_sessions, session_close_supported=True)
        if nested_int(response, "generation") != gateway.generation:
            error = HubError("Hub Gateway generation changed", code="stale_generation", status=409)
            gateway._fence_and_stop(error)
            raise error

    def pending(self, local_id):
        return (any(row["local_session_id"] == local_id for row in self._commands.values())
                or local_id in self._sessions)

    def stop(self):
        self._wake.set()
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        self._commands.clear()
        return sessions

    def join(self):
        if (self._thread is not None and self._thread.ident is not None
                and self._thread is not threading.current_thread()):
            self._thread.join(timeout=1.0)

    def _run(self):
        gateway = self.gateway
        while not gateway._stop.is_set():
            try:
                self._cycle()
            except HubError as error:
                gateway._handle_hub_error(error)
            except Exception:
                gateway._fence_and_stop(HubError(
                    "Gateway session creation state is unavailable", code="session_creation_unavailable"))
            self._wake.wait(gateway.options.poll_interval)

    def _cycle(self):
        gateway = self.gateway
        self._report_pending()
        if gateway._stop.is_set():
            return
        commands = gateway.client.get_session_requests(
            instance_id=gateway.instance_id, generation=gateway.generation)
        if not isinstance(commands, list) or len(commands) > 8:
            raise _invalid()
        for value in commands:
            self._receive(_command(value, gateway))
        self._create_next()
        self._report_pending()

    def _receive(self, command):
        gateway = self.gateway
        request_id = command["request_id"]
        with gateway._session_lock:
            if gateway._stop.is_set():
                return
            receipt = gateway._store.creation(request_id)
            if receipt is not None:
                if any(receipt[key] != command[key] for key in (
                        "generation", "local_session_id", "name", "project_id")):
                    raise _invalid()
                return
            local_id = command["local_session_id"]
            if local_id in gateway._sessions or self.pending(local_id):
                raise _invalid()
            gateway._store.accept_creation(command)
            used = len(gateway._sessions) + len(self._sessions) + len(self._commands)
            if used >= gateway.options.max_sessions:
                gateway._store.finish_creation(request_id, "failed", error_code="session_capacity")
                return
            self._commands[request_id] = command

    def _create_next(self):
        gateway = self.gateway
        with gateway._session_lock:
            if gateway._stop.is_set() or not self._commands:
                return
            command = next(iter(self._commands.values()))
        session = None
        try:
            session = gateway._factory(session_options(
                gateway.options, command["name"], session_id=command["local_session_id"]))
            if session.state.get("session_id") != command["local_session_id"]:
                raise _invalid()
        except Exception:
            if session is not None:
                _close(session)
            self._failed(command)
            return
        with gateway._session_lock:
            if gateway._stop.is_set():
                _close(session)
                return
            self._sessions[command["local_session_id"]] = session
            gateway._store.finish_creation(command["request_id"], "created")
            self._commands.pop(command["request_id"], None)

    def _failed(self, command):
        gateway = self.gateway
        with gateway._session_lock:
            if gateway._stop.is_set():
                return
            gateway._store.finish_creation(command["request_id"], "failed",
                                           error_code="session_creation_failed")
            self._commands.pop(command["request_id"], None)

    def _report_pending(self):
        gateway = self.gateway
        with gateway._session_lock:
            if gateway._stop.is_set():
                return
            pending = gateway._store.pending_creations(gateway.generation)
        for row in pending:
            if gateway._stop.is_set():
                return
            response = gateway.client.report_session_request(
                instance_id=gateway.instance_id, generation=gateway.generation,
                request_id=row["request_id"], status=row["state"], error_code=row["error_code"])
            self._acknowledge(row, response)

    def _acknowledge(self, row, response):
        gateway = self.gateway
        _check_report(row, response, gateway.instance_id)
        with gateway._session_lock:
            if gateway._stop.is_set():
                return
            hub_id = response["session_id"]
            if row["state"] == "created":
                local_id = row["local_session_id"]
                session = self._sessions[local_id]
                gateway._adopt_session(session, local_id, hub_id, row["name"])
                self._sessions.pop(local_id)
            gateway._store.finish_creation(row["request_id"], row["state"],
                                           error_code=row["error_code"],
                                           hub_session_id=hub_id, acknowledged=True)


def _check_report(row, response, instance_id):
    if not isinstance(response, dict):
        raise _invalid()
    expected = {"request_id": row["request_id"], "instance_id": instance_id,
                "name": row["name"], "status": row["state"], "error_code": row["error_code"]}
    if any(response.get(key) != value for key, value in expected.items()):
        raise _invalid()
    session_id = response.get("session_id")
    if (row["state"] == "created" and not _text(session_id)) or (
            row["state"] == "failed" and session_id != ""):
        raise _invalid()


def _close(session):
    try:
        session.close()
    except Exception:
        pass
