"""Close linearizes with real turn and compute admissions, including stale views."""

import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pbatch.hub_store import HubStore, HubStoreError
from test_hub_compute import system, submit  # noqa: F401
from test_hub_session_requests import rejected


def enable_close(system):
    system.hub.register_instance("team", "source", {
        "boot_id": "boot", "session_capacity": 1, "session_close_supported": True})


def close(system):
    return system.hub.close_session("team", system.session["session_id"], "alice", "close",
                                    client_id="console")


def report(system, request):
    return system.hub.report_session_close_request("team", "source", 1, {
        "request_id": request["request_id"], "status": "closed", "error_code": ""})


@pytest.mark.parametrize("state", ["queued", "dispatching", "running", "cancel_requested"])
def test_nonterminal_compute_blocks_close_even_without_active_turn(system, state):
    enable_close(system)
    task = submit(system)
    system.compute.store.update("team", task["task_id"], {}, state=state)
    assert system.hub.session("team", system.session["session_id"])["active_turn_id"] == ""
    rejected("session_busy", close, system)
    assert not system.hub.session("team", system.session["session_id"])["frozen"]
    assert system.hub.session_close_requests("team", "source", 1) == []


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled", "lost"])
def test_terminal_compute_history_and_pending_archival_survive_close(system, state):
    enable_close(system)
    task = submit(system)
    store = system.compute.store
    terminal = store.update("team", task["task_id"], {"archive_state": "pending"}, state=state)
    events = system.hub.events("team", system.session["session_id"])
    report(system, close(system))
    assert store.get("team", task["task_id"], private=True) == terminal
    assert system.hub.events("team", system.session["session_id"]) == events
    store.update("team", task["task_id"], {"archive_state": "archived"})
    assert store.get("team", task["task_id"])["archive_state"] == "archived"


def test_stale_session_view_cannot_admit_new_compute_after_close(system):
    enable_close(system)
    assert system.session["controllable"]
    close(system)
    rejected("session_unavailable", system.compute.store.submit, system.user, system.session,
             {"argv": ["true"]}, "late", ["device-b"], False)
    assert system.compute.store.list("team", ("source",)) == []


def test_persisted_compute_blocks_close_after_restart_without_compute_runtime(system):
    enable_close(system)
    submit(system)
    system.hub.close()
    system.hub = HubStore(system.hub.path)
    try:
        rejected("session_busy", close, system)
    finally:
        system.hub.close()


def test_compute_for_another_session_does_not_block_idle_close(system):
    enable_close(system)
    system.hub.register_instance("team", "source", {
        "boot_id": "boot", "session_capacity": 2, "session_close_supported": True})
    other = system.hub.upsert_session("team", "source", 1, {
        "local_session_id": "other", "controllable": True})
    system.compute.store.submit(system.user, other, {"argv": ["true"]}, "other", ["device-b"], False)
    assert close(system)["status"] == "queued"


def run_operation(system, kind):
    try:
        if kind == "close":
            return close(system)["status"]
        if kind == "turn":
            return system.hub.submit_turn("team", system.session["session_id"], "alice",
                                          "Work", "turn")["status"]
        return system.compute.store.submit(system.user, system.session, {"argv": ["true"]},
                                            "task", ["device-b"], False)["state"]
    except HubStoreError as error:
        return error.code


@pytest.mark.parametrize("work", ["turn", "compute"])
@pytest.mark.parametrize("first", ["close", "work"])
def test_close_and_work_admission_share_one_atomic_sqlite_boundary(system, monkeypatch, work, first):
    enable_close(system)
    acquired, attempted, release = threading.Event(), threading.Event(), threading.Event()
    write = system.hub._write

    @contextlib.contextmanager
    def held_write():
        if acquired.is_set():
            attempted.set()
        with write() as conn:
            if not acquired.is_set():
                acquired.set()
                assert release.wait(5)
            yield conn

    monkeypatch.setattr(system.hub, "_write", held_write)
    kinds = ("close", work) if first == "close" else (work, "close")
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(run_operation, system, kinds[0])
        try:
            assert acquired.wait(5)
            loser = pool.submit(run_operation, system, kinds[1])
            assert attempted.wait(5)
        finally:
            release.set()
        assert winner.result(timeout=10) == "queued"
        assert loser.result(timeout=10) == ("session_unavailable" if first == "close" else "session_busy")
    session = system.hub.session("team", system.session["session_id"])
    assert session["frozen"] is (first == "close")
    assert bool(system.hub.session_close_requests("team", "source", 1)) is (first == "close")
