"""Native subprocess lock contracts; the same tests run on Windows CI."""

from __future__ import annotations

import errno
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from pbatch import file_lock

_ROOT = Path(__file__).resolve().parents[1]
_PROBE = """
import sys
from pbatch import file_lock as lock
with open(sys.argv[1], 'a+b') as handle:
    try:
        lock.flock(handle, int(sys.argv[2]) | lock.LOCK_NB)
    except BlockingIOError:
        print('blocked')
    else:
        print('locked')
"""
_HOLDER = """
import sys
from pbatch import file_lock as lock
with open(sys.argv[1], 'a+b') as handle:
    print('attempting', flush=True)
    lock.flock(handle, int(sys.argv[2]))
    print('locked', flush=True)
    sys.stdin.readline()
print('closed', flush=True)
"""


def _probe(path, mode):
    result = subprocess.run([sys.executable, "-c", _PROBE, str(path), str(mode)],
                            cwd=_ROOT, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


class _Holder:
    def __init__(self, path, mode):
        self.process = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(path), str(mode)],
            cwd=_ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        self.lines = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        for line in self.process.stdout:
            self.lines.put(line.strip())

    def receive(self, timeout=5):
        return self.lines.get(timeout=timeout)

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.write("release\n")
            self.process.stdin.flush()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise
        finally:
            self.reader.join(timeout=5)
            for stream in (self.process.stdin, self.process.stdout,
                           self.process.stderr):
                stream.close()


@pytest.mark.parametrize("held, requested, expected", [
    (file_lock.LOCK_EX, file_lock.LOCK_EX, "blocked"),
    (file_lock.LOCK_EX, file_lock.LOCK_SH, "blocked"),
    (file_lock.LOCK_SH, file_lock.LOCK_EX, "blocked"),
    (file_lock.LOCK_SH, file_lock.LOCK_SH, "locked"),
])
def test_cross_process_shared_and_exclusive(tmp_path, held, requested, expected):
    path = tmp_path / "state.lock"
    with path.open("a+b") as handle:
        file_lock.flock(handle, held)
        assert _probe(path, requested) == expected
        file_lock.flock(handle, file_lock.LOCK_UN)
        assert _probe(path, file_lock.LOCK_EX) == "locked"


def test_blocking_lock_waits_until_owner_closes(tmp_path):
    path = tmp_path / "state.lock"
    handle = path.open("a+b")
    file_lock.flock(handle, file_lock.LOCK_EX)
    child = _Holder(path, file_lock.LOCK_EX)
    try:
        assert child.receive() == "attempting"
        with pytest.raises(queue.Empty):
            child.receive(timeout=0.15)
        handle.close()
        assert child.receive() == "locked"
    finally:
        handle.close()
        child.close()
    assert _probe(path, file_lock.LOCK_EX) == "locked"


@pytest.mark.parametrize("mode", [file_lock.LOCK_SH, file_lock.LOCK_EX])
def test_repeated_lock_needs_only_one_unlock(tmp_path, mode):
    path = tmp_path / "state.lock"
    with path.open("a+b") as handle:
        file_lock.flock(handle, file_lock.LOCK_UN)
        file_lock.flock(handle, mode | file_lock.LOCK_NB)
        file_lock.flock(handle, mode | file_lock.LOCK_NB)
        file_lock.flock(handle, file_lock.LOCK_UN)
        assert _probe(path, file_lock.LOCK_EX) == "locked"


def test_lock_converts_between_shared_and_exclusive(tmp_path):
    path = tmp_path / "state.lock"
    with path.open("a+b") as handle:
        file_lock.flock(handle, file_lock.LOCK_EX)
        file_lock.flock(handle, file_lock.LOCK_SH | file_lock.LOCK_NB)
        assert _probe(path, file_lock.LOCK_SH) == "locked"
        assert _probe(path, file_lock.LOCK_EX) == "blocked"
        file_lock.flock(handle, file_lock.LOCK_EX | file_lock.LOCK_NB)
        assert _probe(path, file_lock.LOCK_SH) == "blocked"


def test_failed_nonblocking_conversion_drops_previous_lock(tmp_path):
    path = tmp_path / "state.lock"
    with path.open("a+b") as handle:
        file_lock.flock(handle, file_lock.LOCK_SH)
        child = _Holder(path, file_lock.LOCK_SH)
        try:
            assert child.receive() == "attempting"
            assert child.receive() == "locked"
            with pytest.raises(BlockingIOError) as caught:
                file_lock.flock(handle, file_lock.LOCK_EX | file_lock.LOCK_NB)
            assert caught.value.errno in (errno.EACCES, errno.EAGAIN)
        finally:
            child.close()
        assert _probe(path, file_lock.LOCK_EX) == "locked"


def test_closed_descriptor_can_be_reused_for_a_new_lock(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    descriptor = os.open(first, os.O_CREAT | os.O_RDWR, 0o600)
    file_lock.flock(descriptor, file_lock.LOCK_EX)
    os.close(descriptor)
    reused = os.open(second, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        assert reused == descriptor
        file_lock.flock(reused, file_lock.LOCK_EX)
        assert _probe(first, file_lock.LOCK_EX) == "locked"
        assert _probe(second, file_lock.LOCK_EX) == "blocked"
    finally:
        os.close(reused)
    assert _probe(second, file_lock.LOCK_EX) == "locked"


def test_lock_does_not_prevent_ordinary_data_reads(tmp_path):
    path = tmp_path / "index.jsonl"
    path.write_bytes(b"index contents\n")
    code = "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())"
    with path.open("r+b") as handle:
        file_lock.flock(handle, file_lock.LOCK_EX)
        result = subprocess.run([sys.executable, "-c", code, str(path)],
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "index contents"


@pytest.mark.skipif(os.name == "nt", reason="POSIX delegation contract")
def test_posix_keeps_existing_fcntl_monkeypatches(monkeypatch):
    import fcntl
    calls = []
    monkeypatch.setattr(fcntl, "flock", lambda *args: calls.append(args))
    file_lock.flock(17, file_lock.LOCK_EX)
    assert calls == [(17, fcntl.LOCK_EX)]
