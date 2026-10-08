"""Private pathname checks and SQLite ownership share a bounded cooperative guard."""
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from pbatch import file_lock as locks
from pbatch import llm_pool_store as module
from pbatch.llm_pool_store import PoolStore,PoolStoreError


def probe(parent):
    return os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)


def test_permission_checks_hold_exclusive_directory_guard_before_sqlite_begin(tmp_path,monkeypatch):
    store=PoolStore(tmp_path/"state"/"owned.sqlite");original=module._private_file;checks=[]
    def inspected(path):
        descriptor=probe(store.path.parent)
        try:
            with pytest.raises(BlockingIOError):locks.flock(descriptor,locks.LOCK_EX|locks.LOCK_NB)
            checks.append(path)
        finally:os.close(descriptor)
        return original(path)
    monkeypatch.setattr(module,"_private_file",inspected)
    with store._transaction(prune=False) as conn:
        conn.execute("INSERT INTO controls VALUES('owned',1,0)")
        descriptor=probe(store.path.parent)
        try:
            with pytest.raises(BlockingIOError):locks.flock(descriptor,locks.LOCK_EX|locks.LOCK_NB)
        finally:os.close(descriptor)
    assert store.path in checks
    descriptor=probe(store.path.parent)
    try:locks.flock(descriptor,locks.LOCK_EX|locks.LOCK_NB)
    finally:os.close(descriptor)


def test_sqlite_receives_only_remaining_guard_budget(tmp_path,monkeypatch):
    store=PoolStore(tmp_path/"owned.sqlite");connect=module.sqlite3.connect;observed=[]
    @contextmanager
    def spent_guard(_parent):yield time.monotonic()+0.2
    def bounded(*args,**kwargs):
        observed.append(kwargs["timeout"])
        return connect(*args,**kwargs)
    monkeypatch.setattr(module,"state_guard",spent_guard)
    monkeypatch.setattr(module.sqlite3,"connect",bounded)
    with store._transaction(prune=False):pass
    assert len(observed)==1 and 0<=observed[0]<=0.2


def test_rollback_and_close_occur_before_directory_guard_release(tmp_path,monkeypatch):
    store=PoolStore(tmp_path/"owned.sqlite");connect=module.sqlite3.connect;closed=[]
    class CheckedConnection(sqlite3.Connection):
        def close(self):
            descriptor=probe(store.path.parent)
            try:
                with pytest.raises(BlockingIOError):locks.flock(descriptor,locks.LOCK_EX|locks.LOCK_NB)
                closed.append(True)
            finally:os.close(descriptor)
            super().close()
    monkeypatch.setattr(module.sqlite3,"connect",lambda *a,**k:connect(*a,factory=CheckedConnection,**k))
    with pytest.raises(ValueError,match="owned abort"):
        with store._transaction(prune=False) as conn:
            conn.execute("INSERT INTO controls VALUES('rolled-back',1,0)")
            raise ValueError("owned abort")
    with connect(store.path) as conn:assert conn.execute("SELECT count(*) FROM controls").fetchone()==(0,)
    assert closed==[True]


def test_guard_busy_refuses_within_original_budget_and_closes_descriptor(tmp_path,monkeypatch):
    from pbatch import llm_pool_state_guard as guard
    ticks=iter([10,12,21]);sleeps=[];closed=[];close=guard.os.close
    monkeypatch.setattr(guard,"time",SimpleNamespace(monotonic=lambda:next(ticks),sleep=sleeps.append))
    def busy(*_args):raise BlockingIOError("owned busy")
    monkeypatch.setattr(guard.fcntl,"flock",busy)
    def tracked(descriptor):closed.append(descriptor);return close(descriptor)
    monkeypatch.setattr(guard.os,"close",tracked)
    with pytest.raises(TimeoutError,match="busy"):
        with guard.state_guard(tmp_path):pytest.fail("busy state must not admit")
    assert sleeps==[.01] and len(closed)==1 and guard.LOCK_TIMEOUT==10.0


@pytest.mark.parametrize("kind",["symlink","file","owner"])
def test_guard_rejects_bad_directory_before_lock(kind,tmp_path,monkeypatch):
    from pbatch import llm_pool_state_guard as guard
    path=tmp_path/"scope";real=tmp_path/"real";real.mkdir()
    if kind=="symlink":path.symlink_to(real,target_is_directory=True)
    elif kind=="file":path.write_bytes(b"unmodified")
    else:
        path.mkdir();uid=os.getuid();monkeypatch.setattr(guard.os,"getuid",lambda:uid+1)
    monkeypatch.setattr(guard.fcntl,"flock",lambda *_:pytest.fail("invalid directory must not lock"))
    with pytest.raises(OSError):
        with guard.state_guard(path):pytest.fail("invalid directory must not admit")
    assert not list(real.iterdir())


def test_named_directory_replacement_during_lock_refuses(tmp_path,monkeypatch):
    from pbatch import llm_pool_state_guard as guard
    parent=tmp_path/"scope";parent.mkdir();moved=tmp_path/"preserved"
    def replace(*_args):parent.rename(moved);parent.mkdir()
    monkeypatch.setattr(guard,"_acquire",replace)
    with pytest.raises(OSError,match="changed"):
        with guard.state_guard(parent):pytest.fail("changed path must not admit")
    assert moved.is_dir() and parent.is_dir()


def test_mutex_does_not_silence_private_file_replacement_check(tmp_path,monkeypatch):
    path=tmp_path/"sidecar";path.write_bytes(b"old");original=module.os.chmod
    def replace(selected,*args,**kwargs):
        selected.rename(tmp_path/"preserved-old");selected.write_bytes(b"new")
        return original(selected,*args,**kwargs)
    monkeypatch.setattr(module.os,"chmod",replace)
    with pytest.raises(PoolStoreError,match="changed while checking"):
        module._private_file(path)
    assert path.read_bytes()==b"new" and (tmp_path/"preserved-old").read_bytes()==b"old"


@pytest.mark.skipif(sys.platform!="linux",reason="explicit Linux directory lock/SIGKILL boundary")
def test_killed_holder_releases_guard_without_lock_breaking_or_state_cleanup(tmp_path):
    from pbatch.llm_pool_state_guard import state_guard
    ready=tmp_path/"ready"
    script="""
from pathlib import Path
import sys,time
from pbatch.llm_pool_state_guard import state_guard
with state_guard(Path(sys.argv[1])):
    Path(sys.argv[2]).touch()
    time.sleep(30)
"""
    child=subprocess.Popen([sys.executable,"-c",script,str(tmp_path),str(ready)])
    try:
        deadline=time.monotonic()+5
        while not ready.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.01)
        assert ready.exists();child.kill();child.wait(timeout=5)
        assert child.returncode==-signal.SIGKILL
        with state_guard(tmp_path):assert ready.exists()
    finally:
        if child.poll() is None:child.kill()
        child.wait(timeout=5)
