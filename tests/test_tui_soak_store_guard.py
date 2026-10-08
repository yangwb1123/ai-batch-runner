"""The Linux fixture guard preserves atomic evidence and the original wait budget."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

from test_tui_soak_provider_log import LOG,TOOLS,SoakFailure

sys.path.insert(0,str(TOOLS))
try:
    import tui_soak_store_guard as guard
finally:sys.path.remove(str(TOOLS))


def held(parent):
    descriptor=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
    try:
        with pytest.raises(BlockingIOError):fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
    finally:os.close(descriptor)


def test_all_fixture_connections_hold_guard_until_sqlite_close(tmp_path,monkeypatch):
    connected=[];closed=[];connect=sqlite3.connect
    class GuardedConnection(sqlite3.Connection):
        def close(self):held(tmp_path);closed.append(True);super().close()
    def inspected(*args,**kwargs):
        held(tmp_path);connected.append(True)
        return connect(*args,factory=GuardedConnection,**kwargs)
    monkeypatch.setattr(LOG.sqlite3,"connect",inspected)
    path=tmp_path/"provider.jsonl";reader=LOG.ProviderLog(path,SoakFailure)
    LOG.append_provider_event(path,{"kind":"owned"})
    assert reader.read()==[{"kind":"owned"}]
    assert connected==closed==[True,True,True]


def test_spent_guard_wait_is_not_added_to_original_sqlite_busy_timeout(tmp_path,monkeypatch):
    clock=[100.0];sleeps=[];calls=[];flock=guard.fcntl.flock;connect=sqlite3.connect;timeouts=[]
    def contend(descriptor,mode):
        calls.append(mode)
        if len(calls)==1:clock[0]=101.0;raise BlockingIOError("owned busy")
        return flock(descriptor,mode)
    def sleep(delay):sleeps.append(delay);clock[0]+=delay
    def inspected(*args,**kwargs):timeouts.append(kwargs["timeout"]);return connect(*args,**kwargs)
    monkeypatch.setattr(guard,"time",SimpleNamespace(monotonic=lambda:clock[0],sleep=sleep))
    monkeypatch.setattr(guard.fcntl,"flock",contend);monkeypatch.setattr(guard.sqlite3,"connect",inspected)
    with guard.connection(tmp_path/"owned.sqlite",LOG.LOCK_TIMEOUT):pass
    assert sleeps==[.01] and len(timeouts)==1 and 1.98<timeouts[0]<2.0
    assert LOG.LOCK_TIMEOUT==3.0


def test_busy_guard_does_not_open_sqlite_export_or_advance_cursor(tmp_path,monkeypatch):
    path=tmp_path/"provider.jsonl";reader=LOG.ProviderLog(path,SoakFailure)
    def busy(*_args):raise BlockingIOError("owned lock")
    ticks=iter([100.0,101.0,104.0])
    monkeypatch.setattr(guard,"time",SimpleNamespace(monotonic=lambda:next(ticks),sleep=lambda _delay:None))
    monkeypatch.setattr(guard.fcntl,"flock",busy)
    monkeypatch.setattr(guard.sqlite3,"connect",lambda *_a,**_k:pytest.fail("busy guard must not connect"))
    with pytest.raises(SoakFailure,match="locked"):reader.read()
    assert reader.offset==0 and not reader.exhausted and not path.exists()


def test_guard_nonblocking_wait_has_original_bound_with_real_held_lock(tmp_path):
    path=tmp_path/"provider.jsonl";reader=LOG.ProviderLog(path,SoakFailure)
    descriptor=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX)
        started=time.monotonic()
        with pytest.raises(SoakFailure,match="locked"):reader.read()
        assert time.monotonic()-started<LOG.LOCK_TIMEOUT+1.5
        assert reader.offset==0 and not path.exists()
    finally:os.close(descriptor)
    LOG.append_provider_event(path,{"kind":"after-release"})
    assert reader.read()==[{"kind":"after-release"}]


def test_store_guard_does_not_treat_corruption_as_transient_lock_or_empty(tmp_path):
    path=tmp_path/"provider.jsonl";reader=LOG.ProviderLog(path,SoakFailure)
    with sqlite3.connect(LOG.database_path(path)) as conn:conn.execute("INSERT INTO events VALUES(1,?)",(b"bad-json\n",))
    with pytest.raises(SoakFailure,match="invalid committed"):reader.read()
    assert reader.offset==0 and not reader.exhausted and not path.exists()


def test_failed_export_still_preserves_cursor_and_all_committed_evidence(tmp_path):
    path=tmp_path/"provider.jsonl";reader=LOG.ProviderLog(path,SoakFailure)
    LOG.append_provider_event(path,{"kind":"owned"})
    path.mkdir()
    with pytest.raises(OSError):reader.read()
    assert reader.offset==0
    with sqlite3.connect(LOG.database_path(path)) as conn:assert conn.execute("SELECT count(*) FROM events").fetchone()==(1,)


@pytest.mark.parametrize("kind",["symlink","owner"])
def test_bad_guard_directory_refuses_before_sqlite(kind,tmp_path,monkeypatch):
    real=tmp_path/"real";real.mkdir();parent=real
    if kind=="symlink":parent=tmp_path/"alias";parent.symlink_to(real,target_is_directory=True)
    else:
        uid=os.getuid();monkeypatch.setattr(guard.os,"getuid",lambda:uid+1)
    monkeypatch.setattr(guard.sqlite3,"connect",lambda *_a,**_k:pytest.fail("bad directory must not connect"))
    with pytest.raises((OSError,sqlite3.Error)):
        with guard.connection(parent/"owned.sqlite",LOG.LOCK_TIMEOUT):pytest.fail("bad directory must not admit")
    assert not list(real.iterdir())
