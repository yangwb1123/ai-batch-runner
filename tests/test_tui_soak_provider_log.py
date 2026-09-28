"""Provider evidence stays atomic when cancellation kills its writer."""

import json
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
try:
    import tui_soak_provider_log as LOG
    from tui_soak_support import JsonTail, SoakFailure
finally:
    sys.path.remove(str(TOOLS))


def _wait_ready(process, ready):
    deadline = time.monotonic() + 5
    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert ready.exists(), "writer did not reach the injected kill boundary"


def _kill(process):
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "linux", reason="deterministic Linux SIGKILL boundary")
def test_legacy_shared_append_reproduces_partial_record_after_kill(tmp_path):
    path, ready = tmp_path / "provider.jsonl", tmp_path / "ready"
    source = ("import os, pathlib, sys, time\n"
              "path, ready = map(pathlib.Path, sys.argv[1:])\n"
              "with path.open('ab', buffering=0) as stream:\n"
              "    stream.write(b'{\"kind\": \"control\", \"command\": \"abort\"')\n"
              "    ready.touch()\n"
              "    time.sleep(30)\n"
              "    stream.write(b'}\\n')\n")
    process = subprocess.Popen([sys.executable, "-c", source, str(path), str(ready)])
    try:
        _wait_ready(process, ready)
        _kill(process)
        with path.open("ab") as stream:
            stream.write(b'{"kind": "started"}\n')
        with pytest.raises(SoakFailure, match="invalid evidence JSON"):
            JsonTail(path).read()
    finally:
        _kill(process)


def _paused_transaction(path, ready, committed):
    source = (
        "import pathlib, sqlite3, sys, time\n"
        f"sys.path.insert(0, {str(TOOLS)!r})\n"
        "import tui_soak_provider_log as log\n"
        "path, ready = map(pathlib.Path, sys.argv[1:])\n"
        "class Paused(sqlite3.Connection):\n"
        "    def __init__(self, *args, **kwargs):\n"
        "        super().__init__(*args, **kwargs)\n"
        "        self.execute('PRAGMA cache_size=1')\n"
        "    def commit(self):\n"
        + ("        super().commit()\n" if committed else "")
        + "        ready.touch()\n"
        "        time.sleep(30)\n"
        "original = sqlite3.connect\n"
        "log.sqlite3.connect = lambda *args, **kwargs: original(*args, factory=Paused, **kwargs)\n"
        "log.append_provider_event(path, {'kind':'control', 'command':'abort', 'padding':'x'*128000})\n"
    )
    return subprocess.Popen([sys.executable, "-c", source, str(path), str(ready)])


@pytest.mark.skipif(sys.platform != "linux", reason="deterministic Linux SIGKILL boundary")
@pytest.mark.parametrize("committed", [False, True])
def test_killed_transaction_publishes_all_or_none_and_next_provider_can_append(tmp_path, committed):
    path, ready = tmp_path / "provider.jsonl", tmp_path / "ready"
    reader = LOG.ProviderLog(path, SoakFailure)
    LOG.append_provider_event(path, {"kind": "prompt"})
    process = _paused_transaction(path, ready, committed)
    try:
        _wait_ready(process, ready)
        if not committed:
            assert LOG.database_path(path).with_suffix(".sqlite-journal").exists()
        _kill(process)
        assert process.returncode == -signal.SIGKILL
        LOG.append_provider_event(path, {"kind": "started"})
        values = reader.read()
        assert [value["kind"] for value in values] == (
            ["prompt", "control", "started"] if committed else ["prompt", "started"])
        assert JsonTail(path).read() == values
        assert reader.read() == [] and reader.exhausted
        with sqlite3.connect(LOG.database_path(path)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        _kill(process)


def test_concurrent_fixture_writers_export_every_committed_record_once(tmp_path):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    source = ("import pathlib, sys\n"
              f"sys.path.insert(0, {str(TOOLS)!r})\n"
              "from tui_soak_provider_log import append_provider_event\n"
              "for number in range(40):\n"
              "    append_provider_event(pathlib.Path(sys.argv[1]), {'writer':sys.argv[2], 'number':number})\n")
    processes = [subprocess.Popen([sys.executable, "-c", source, str(path), str(number)])
                 for number in range(4)]
    values, deadline = [], time.monotonic() + 10
    try:
        while any(process.poll() is None for process in processes) and time.monotonic() < deadline:
            values.extend(reader.read())
            time.sleep(.01)
        assert all(process.wait(timeout=1) == 0 for process in processes)
        while batch := reader.read():
            values.extend(batch)
        assert len(values) == reader.offset == 160
        assert {(value["writer"], value["number"]) for value in values} == {
            (str(writer), number) for writer in range(4) for number in range(40)}
        assert [json.loads(line) for line in path.read_bytes().splitlines()] == values
    finally:
        for process in processes:
            _kill(process)


@pytest.mark.parametrize("raw", [b"not-json\n", b'{}', b'[]\n', b'\xff\n', b'x' * (LOG.MAX_BYTES + 1)])
def test_committed_invalid_record_is_rejected_without_export(tmp_path, raw):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    with sqlite3.connect(LOG.database_path(path)) as connection:
        connection.execute("INSERT INTO events (payload) VALUES (?)", (raw,))
    with pytest.raises(SoakFailure):
        reader.read()
    assert reader.offset == 0 and not path.exists()


def test_provider_reads_have_a_record_and_byte_budget(tmp_path):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    with sqlite3.connect(LOG.database_path(path)) as connection:
        connection.executemany("INSERT INTO events (payload) VALUES (?)", [(b'{}\n',)] * 130)
    assert len(reader.read()) == 128
    assert len(reader.read()) == 2
    value = {"padding": "x" * (LOG.MAX_BYTES // 2)}
    LOG.append_provider_event(path, value)
    LOG.append_provider_event(path, value)
    assert reader.read() == [value]
    assert reader.read() == [value]
    assert reader.read() == []


def test_provider_store_lock_wait_is_bounded(tmp_path):
    path = tmp_path / "provider.jsonl"
    reader = LOG.ProviderLog(path, SoakFailure)
    connection = sqlite3.connect(LOG.database_path(path))
    try:
        connection.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with pytest.raises(SoakFailure, match="locked"):
            reader.read()
        assert time.monotonic() - started < LOG.LOCK_TIMEOUT + 1.5
    finally:
        connection.close()
