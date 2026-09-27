"""Instance-lock races use independent processes and explicit barriers."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time

import pytest

from pbatch import lock, lock_storage, process_identity

_ROOT = Path(__file__).resolve().parents[1]
_CHILD = """
import json, os, sys, time
from pathlib import Path
from pbatch import lock, lock_storage
path, mode, minutes = Path(sys.argv[1]), sys.argv[2], float(sys.argv[3])
outputs, session = tuple(json.loads(sys.argv[4])), sys.argv[5]
lock._worktree_dirty = lambda cwd: False
lock_storage.GUARD_TIMEOUT = 0.25
def send(kind, **data):
    print(json.dumps(dict(kind=kind, **data)), flush=True)
if mode == 'guard':
    with lock_storage.lock_guard(path):
        send('guarded')
        sys.stdin.readline()
else:
    if mode == 'paused_stale':
        original = Path.unlink
        def paused_unlink(self, *args, **kwargs):
            if self == path:
                send('before_unlink')
                sys.stdin.readline()
            return original(self, *args, **kwargs)
        Path.unlink = paused_unlink
    send('attempting')
    started = time.monotonic()
    try:
        held = lock.acquire_lock(str(path.parent), wait_minutes=minutes,
                                 outputs=outputs, session_name=session)
    except SystemExit as exc:
        send('refused', code=exc.code, elapsed=time.monotonic()-started)
    else:
        if mode == 'paused_stale':
            Path.unlink = original
        send('acquired', pid=os.getpid(), elapsed=time.monotonic()-started,
             held=str(held), verified=lock.verify_held(held))
        while sys.stdin.readline().strip() == 'verify':
            send('verified', owned=lock.verify_held(held))
        lock.release_lock(held)
        send('released')
"""


class _Child:
    def __init__(self, path: Path, mode: str, minutes: float,
                 outputs=(), session=""):
        environment = dict(os.environ, PBATCH_LOCK_FILE=str(path),
                           PBATCH_RUNTIME_HOME=str(path.parent / "runtime"))
        self.process = subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(path), mode, str(minutes),
             json.dumps(outputs), session],
            cwd=_ROOT, env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.messages = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            self.messages.put(json.loads(line))

    def receive(self, timeout=5):
        return self.messages.get(timeout=timeout)

    def send(self, value="continue"):
        self.process.stdin.write(value + "\n")
        self.process.stdin.flush()

    def close(self):
        try:
            if self.process.poll() is None:
                try:
                    self.send("finish\nfinish")
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise
        finally:
            self.reader.join(timeout=2)
            for stream in (self.process.stdin, self.process.stdout,
                           self.process.stderr):
                stream.close()


@contextmanager
def _child(path, mode="owner", minutes=0, outputs=(), session=""):
    child = _Child(path, mode, minutes, outputs, session)
    try:
        yield child
    finally:
        child.close()


@pytest.fixture
def coordinated(tmp_path, monkeypatch):
    path = tmp_path / "instance.lock"
    monkeypatch.setenv("PBATCH_LOCK_FILE", str(path))
    monkeypatch.setattr(lock, "_worktree_dirty", lambda cwd: False)
    monkeypatch.setattr(lock_storage, "GUARD_TIMEOUT", 0.2)
    yield path
    lock.release_lock(path)


def _holder(**values):
    return dict({"pid": 99999999, "host": socket.gethostname(), "start": 0,
                 "token": "stale"}, **values)


def test_stale_reclamation_cannot_delete_concurrent_successor(coordinated):
    coordinated.write_text(json.dumps(_holder()), encoding="utf-8")
    with _child(coordinated, "paused_stale") as first:
        assert first.receive()["kind"] == "attempting"
        assert first.receive()["kind"] == "before_unlink"
        with _child(coordinated) as rival:
            assert rival.receive()["kind"] == "attempting"
            rejected = rival.receive()
            assert rejected["kind"] == "refused"
            assert rejected["code"] == lock.LOCK_EXIT_CODE
        first.send()
        winner = first.receive()
        assert winner["kind"] == "acquired"
        assert lock_storage.read_holder(coordinated)["pid"] == winner["pid"]
        with _child(coordinated) as later:
            assert later.receive()["kind"] == "attempting"
            assert later.receive()["kind"] == "refused"


def test_guard_contention_respects_wait_deadline(coordinated):
    with _child(coordinated, "guard") as holder:
        assert holder.receive()["kind"] == "guarded"
        started = time.monotonic()
        with pytest.raises(SystemExit) as caught:
            lock.acquire_lock(str(coordinated.parent), wait_minutes=0.004)
        elapsed = time.monotonic() - started
        assert caught.value.code == lock.LOCK_EXIT_CODE
        assert 0.15 <= elapsed < 1.5
        assert not coordinated.exists()


def test_waiter_does_not_block_owner_release(coordinated):
    with _child(coordinated) as owner:
        assert owner.receive()["kind"] == "attempting"
        assert owner.receive()["kind"] == "acquired"
        with _child(coordinated, "waiter", minutes=0.1) as waiter:
            assert waiter.receive()["kind"] == "attempting"
            with pytest.raises(queue.Empty):
                waiter.receive(timeout=0.15)
            owner.send()
            assert owner.receive()["kind"] == "released"
            acquired = waiter.receive()
            assert acquired["kind"] == "acquired"
            assert lock_storage.read_holder(coordinated)["pid"] == acquired["pid"]


def test_failed_release_retains_ownership_for_retry(coordinated):
    owned = lock.acquire_lock(str(coordinated.parent))
    with _child(coordinated, "guard") as holder:
        assert holder.receive()["kind"] == "guarded"
        lock.release_lock(owned)
        assert coordinated.exists()
    assert lock.verify_held(owned)
    lock.release_lock(owned)
    assert not coordinated.exists()


def test_old_owner_release_preserves_successor_token(coordinated):
    owned = lock.acquire_lock(str(coordinated.parent))
    successor = lock_storage.read_holder(owned)
    successor["token"] = "successor-token"
    with lock_storage.lock_guard(owned):
        owned.unlink()
        assert lock_storage.publish_holder(owned, successor)
    lock.release_lock(owned)
    assert lock_storage.read_holder(owned)["token"] == "successor-token"


def _assert_conflict(path, outputs, session):
    with _child(path, outputs=outputs, session=session) as contender:
        assert contender.receive()["kind"] == "attempting"
        result = contender.receive()
        assert result["kind"] == "refused"
        assert result["code"] == lock.LOCK_EXIT_CODE


@pytest.mark.parametrize("second_outputs, requested, session", [
    (("b.md",), ("b.md",), ""),
    (("tree",), ("tree/child.md",), ""),
    (("tree/child.md",), ("tree",), ""),
    (("b.md",), ("c.md",), "second-session"),
])
def test_parallel_participant_remains_protected_after_base_owner_exits(
        coordinated, second_outputs, requested, session):
    with _child(coordinated, outputs=("a.md",)) as first:
        assert first.receive()["kind"] == "attempting"
        assert first.receive()["kind"] == "acquired"
        with _child(coordinated, outputs=second_outputs,
                    session="second-session") as second:
            assert second.receive()["kind"] == "attempting"
            participant = second.receive()
            assert participant["kind"] == "acquired"
            assert participant["verified"] is True
            assert participant["held"] != str(coordinated)
            _assert_conflict(coordinated, requested, session)
            first.send()
            assert first.receive()["kind"] == "released"
            second.send("verify")
            assert second.receive() == {"kind": "verified", "owned": True}
            _assert_conflict(coordinated, requested, session)


@pytest.mark.parametrize("value", [
    [1], 17, {"pid": "broken"}, _holder(pid=True), _holder(pid=-1),
    _holder(pid=1 << 80), _holder(start="broken"), _holder(start=float("nan")),
    _holder(start=float("inf")), _holder(host=[]), _holder(outputs="out.md"),
    _holder(outputs=[{}]), _holder(token=[]), _holder(workspace=[]),
])
def test_malformed_holder_is_refused_and_preserved(coordinated, value):
    raw = json.dumps(value).encode("utf-8")
    coordinated.write_bytes(raw)
    with pytest.raises(SystemExit) as caught:
        lock.acquire_lock(str(coordinated.parent))
    assert caught.value.code == lock.LOCK_EXIT_CODE
    assert coordinated.read_bytes() == raw


@pytest.mark.parametrize("field", ["workspace", "outputs"])
def test_invalid_path_in_live_holder_exits_with_lock_code(coordinated, field):
    value = _holder(pid=os.getpid(), workspace=str(coordinated.parent),
                    outputs=["a.md"], worktree_dirty=False)
    value[field] = ["a\x00.md"] if field == "outputs" else "bad\x00workspace"
    raw = json.dumps(value).encode("utf-8")
    coordinated.write_bytes(raw)
    with pytest.raises(SystemExit) as caught:
        lock.acquire_lock(str(coordinated.parent), outputs=("b.md",))
    assert caught.value.code == lock.LOCK_EXIT_CODE
    assert coordinated.read_bytes() == raw


@pytest.mark.skipif(os.name == "nt", reason="symlink creation requires Windows privilege")
def test_unresolvable_holder_path_exits_with_lock_code(coordinated):
    (coordinated.parent / "loop").symlink_to("loop")
    value = _holder(pid=os.getpid(), workspace=str(coordinated.parent),
                    outputs=["loop/file"], worktree_dirty=False)
    raw = json.dumps(value).encode("utf-8")
    coordinated.write_bytes(raw)
    with pytest.raises(SystemExit) as caught:
        lock.acquire_lock(str(coordinated.parent), outputs=("b.md",))
    assert caught.value.code == lock.LOCK_EXIT_CODE
    assert coordinated.read_bytes() == raw


def test_legacy_guard_keeps_participant_lookup_after_base_release(coordinated,
                                                               monkeypatch):
    monkeypatch.delenv("PBATCH_LOCK_FILE")
    legacy = coordinated.parent / lock.LOCK_NAME
    legacy.write_text(json.dumps(_holder()), encoding="utf-8")
    cwd = str(coordinated.parent)
    first = lock.acquire_lock(cwd, outputs=("a.md",))
    second = lock.acquire_lock(cwd, outputs=("b.md",))
    try:
        assert first == legacy
        lock.release_lock(first)
        assert lock._lock_path(cwd) == legacy
        with pytest.raises(SystemExit) as caught:
            lock.acquire_lock(cwd, outputs=("b.md",))
        assert caught.value.code == lock.LOCK_EXIT_CODE
        assert lock.verify_held(second)
    finally:
        lock.release_lock(first)
        lock.release_lock(second)


@pytest.mark.parametrize("raw", [b"[" * 2000 + b"0" + b"]" * 2000,
                                 b"x" * 65537, b"\xff", b""])
def test_unreadable_holder_is_bounded_and_preserved(coordinated, raw):
    coordinated.write_bytes(raw)
    with pytest.raises(SystemExit) as caught:
        lock.acquire_lock(str(coordinated.parent))
    assert caught.value.code == lock.LOCK_EXIT_CODE
    assert coordinated.read_bytes() == raw


@pytest.mark.parametrize("host", ["different-host.invalid", "", None])
def test_unknown_host_cannot_be_reclaimed_using_local_pid(coordinated, host):
    metadata = _holder(host=host)
    if host is None:
        metadata.pop("host")
    raw = json.dumps(metadata).encode("utf-8")
    coordinated.write_bytes(raw)
    with pytest.raises(SystemExit) as caught:
        lock.acquire_lock(str(coordinated.parent))
    assert caught.value.code == lock.LOCK_EXIT_CODE
    assert coordinated.read_bytes() == raw


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission-error probe")
def test_permission_denied_does_not_authorize_stale_cleanup(coordinated,
                                                          monkeypatch):
    raw = json.dumps(_holder(pid=12345)).encode("utf-8")
    coordinated.write_bytes(raw)

    def deny(pid, signal):
        raise PermissionError(errno.EPERM, "process belongs to another user")

    monkeypatch.setattr(process_identity.os, "kill", deny)
    with pytest.raises(SystemExit) as caught:
        lock.acquire_lock(str(coordinated.parent))
    assert caught.value.code == lock.LOCK_EXIT_CODE
    assert coordinated.read_bytes() == raw
