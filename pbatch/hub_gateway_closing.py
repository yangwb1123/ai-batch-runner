"""Durable, bounded runtime closure separate from prompt and creation traffic."""

from __future__ import annotations

import math
import threading

from .hub_client import HubError


def _invalid():
    return HubError("Hub session close response is invalid", code="invalid_response")


def _identifier(value):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and len(value) <= 256
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _command(value, gateway):
    keys = {"request_id", "instance_id", "generation", "session_id", "local_session_id"}
    if not isinstance(value, dict) or set(value) != keys:
        raise _invalid()
    if any(not _identifier(value[key]) for key in keys - {"generation"}):
        raise _invalid()
    if value["instance_id"] != gateway.instance_id:
        raise _invalid()
    if type(value["generation"]) is not int or value["generation"] != gateway.generation:
        raise HubError("Hub session close generation changed", code="stale_generation", status=409)
    return dict(value)


def _check_report(row, response, instance_id):
    expected = {"request_id": row["request_id"], "instance_id": instance_id,
                "session_id": row["session_id"], "status": row["state"],
                "error_code": row["error_code"]}
    if not isinstance(response, dict) or set(response) != set(expected) | {"created_at", "updated_at"}:
        raise _invalid()
    if any(response.get(key) != value for key, value in expected.items()):
        raise _invalid()
    try:
        for key in ("created_at", "updated_at"):
            value = response[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise _invalid()
    except OverflowError:
        raise _invalid() from None


class SessionClosing:
    """Each runtime close is attempted at most once within this Gateway lifetime."""

    def __init__(self, gateway):
        self.gateway = gateway
        self._commands = {}
        self._thread = None
        self._wake = threading.Event()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="hub-session-close", daemon=True)
        self._thread.start()

    def pending(self, local_id):
        return any(row["local_session_id"] == local_id for row in self._commands.values())

    def stop(self):
        self._wake.set()
        self._commands.clear()

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
                    "Gateway session close state is unavailable", code="session_close_unavailable"))
            self._wake.wait(gateway.options.poll_interval)

    def _cycle(self):
        gateway = self.gateway
        self._report_pending()
        if gateway._stop.is_set():
            return
        commands = gateway.client.get_session_close_requests(
            instance_id=gateway.instance_id, generation=gateway.generation)
        if not isinstance(commands, list) or len(commands) > 8:
            raise _invalid()
        for value in commands:
            self._receive(_command(value, gateway))
        self._close_next()
        self._report_pending()

    def _receive(self, command):
        gateway = self.gateway
        request_id, local_id = command["request_id"], command["local_session_id"]
        with gateway._session_lock:
            if gateway._stop.is_set():
                return
            receipt = gateway._store.closure(request_id)
            if receipt is not None:
                if any(receipt[key] != command[key] for key in (
                        "generation", "local_session_id", "session_id")):
                    raise _invalid()
                return
            if len(self._commands) >= 8 or self.pending(local_id):
                raise _invalid()
            managed = gateway._sessions.get(local_id)
            if managed is not None and (managed.hub_id != command["session_id"] or managed.closing):
                raise _invalid()
            gateway._store.accept_closure(command)
            if managed is None and not gateway._creation.pending(local_id):
                gateway._store.finish_closure(request_id, "failed", error_code="session_unavailable")
                return
            self._commands[request_id] = command
            if managed is not None:
                managed.closing = True
                self._discard_pending_starts(managed)

    def _discard_pending_starts(self, managed):
        gateway = self.gateway
        for turn_id, (_, candidate, _) in tuple(gateway._pending_starts.items()):
            if candidate is managed:
                gateway._pending_starts.pop(turn_id, None)

    def _claim_next(self):
        gateway = self.gateway
        with gateway._session_lock:
            if gateway._stop.is_set():
                return None
            for command in self._commands.values():
                managed = gateway._sessions.get(command["local_session_id"])
                if managed is None or managed.active_turn:
                    continue
                if managed.hub_id != command["session_id"] or managed.close_started:
                    raise _invalid()
                managed.closing = managed.close_started = True
                self._discard_pending_starts(managed)
                return command, managed
        return None

    def _close_next(self):
        claimed = self._claim_next()
        if claimed is None:
            return
        command, managed = claimed
        status, error = "closed", ""
        try:
            managed.session.close()
        except Exception:
            status, error = "failed", "session_close_failed"
        gateway = self.gateway
        with gateway._session_lock:
            if gateway._stop.is_set():
                return
            gateway._store.finish_closure(command["request_id"], status, error_code=error)
            if status == "closed":
                gateway._sessions.pop(managed.local_id, None)
            self._commands.pop(command["request_id"], None)

    def _report_pending(self):
        gateway = self.gateway
        with gateway._session_lock:
            if gateway._stop.is_set():
                return
            pending = gateway._store.pending_closures(gateway.generation)
        for row in pending:
            if gateway._stop.is_set():
                return
            response = gateway.client.report_session_close_request(
                instance_id=gateway.instance_id, generation=gateway.generation,
                request_id=row["request_id"], status=row["state"], error_code=row["error_code"])
            _check_report(row, response, gateway.instance_id)
            with gateway._session_lock:
                if gateway._stop.is_set():
                    return
                gateway._store.finish_closure(row["request_id"], row["state"],
                                             error_code=row["error_code"], acknowledged=True)
