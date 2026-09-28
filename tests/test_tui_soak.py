"""A sustained real-terminal harness must retain one TUI process and honest evidence."""

from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
try:
    import tui_soak_support as SUPPORT
    from tui_soak_provider_log import append_provider_event
    SPEC = importlib.util.spec_from_file_location("tui_soak", TOOLS / "tui-soak.py")
    SOAK = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(SOAK)
finally:
    sys.path.remove(str(TOOLS))


def test_environment_excludes_credentials_and_real_user_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    monkeypatch.setenv("PBATCH_DATA_DIR", "/some-user-state")
    monkeypatch.setenv("CODEX_HOME", "/some-user-home")
    monkeypatch.setenv("PYTHONPATH", "/some-user-module")
    environment, provider = SUPPORT.prepare_environment(tmp_path)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "PBATCH_DATA_DIR", "CODEX_HOME", "PYTHONPATH"):
        assert key not in environment
    for key in ("HOME", "XDG_CONFIG_HOME", "PBATCH_HOME", "PBATCH_RUNTIME_HOME"):
        assert Path(environment[key]).is_relative_to(tmp_path)
    assert provider.name == "pi" and provider.stat().st_mode & 0o100
    assert environment["PATH"] == str(provider.parent)


def test_json_tail_preserves_partial_record_and_rejects_truncation(tmp_path):
    path = tmp_path / "events.jsonl"
    reader = SUPPORT.JsonTail(path)
    path.write_bytes(b'{"kind":"first"}\n{"kind":')
    assert reader.read() == [{"kind": "first"}]
    with path.open("ab") as handle:
        handle.write(b'"second"}\n')
    assert reader.read() == [{"kind": "second"}]
    path.write_bytes(b"")
    with pytest.raises(SUPPORT.SoakFailure, match="truncated"):
        reader.read()


@pytest.mark.parametrize("value", [b"not-json\n", b"[]\n", b"x" * (1024 * 1024 + 1)])
def test_json_tail_rejects_untrustworthy_evidence(tmp_path, value):
    path = tmp_path / "events.jsonl"
    path.write_bytes(value)
    with pytest.raises(SUPPORT.SoakFailure):
        SUPPORT.JsonTail(path).read()


def _event(event_id="evt_one", seq=1):
    return {"event_id": event_id, "seq": seq, "session_id": "ses_one",
            "run_id": "run_one", "kind": "turn.started", "payload": {}}


def test_journal_rejects_duplicate_ids_and_regressing_sequences(tmp_path):
    evidence = SUPPORT.JournalEvidence(tmp_path / "runtime")
    try:
        evidence._accept(_event())
        with pytest.raises(SUPPORT.SoakFailure, match="duplicate"):
            evidence._accept(_event())
        with pytest.raises(SUPPORT.SoakFailure, match="regressed"):
            evidence._accept(_event("evt_two"))
        evidence._accept(_event("evt_two", 2))
        assert evidence.summary()["events"] == 2
        assert evidence.summary()["duplicate_ids"] == 1
    finally:
        evidence.close()


def test_json_tail_has_per_poll_budget(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{}\n' * 1000)
    reader = SUPPORT.JsonTail(path)
    assert 0 < len(reader.read()) <= 128
    assert 0 < reader.offset < path.stat().st_size


def test_final_journal_validation_rejects_unconsumed_partial_tail(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    path = runtime / "events.jsonl"
    path.write_text(json.dumps(_event()) + '\n{"partial":')
    evidence = SUPPORT.JournalEvidence(runtime)
    try:
        evidence.poll()
        with pytest.raises(SUPPORT.SoakFailure, match="partial"):
            evidence.finish(time.monotonic() + 2)
    finally:
        evidence.close()


@pytest.mark.parametrize("bad_final_record", [False, True])
def test_final_provider_export_consumes_all_batches_and_rejects_bad_commits(tmp_path, bad_final_record):
    run = SOAK.SoakRun([sys.executable], tmp_path)
    connection = sqlite3.connect(tmp_path / "provider.sqlite")
    try:
        connection.executemany("INSERT INTO events (payload) VALUES (?)",
                               [(b'{"kind":"control"}\n',)] * 400)
        if bad_final_record:
            connection.execute("INSERT INTO events (payload) VALUES (?)", (b'not-json\n',))
        connection.commit()
        if bad_final_record:
            with pytest.raises(SUPPORT.SoakFailure, match="invalid committed"):
                run._finish_provider_log(time.monotonic() + 5)
        else:
            run._finish_provider_log(time.monotonic() + 5)
            assert run.provider_log.offset == 400 and run.provider_log.exhausted
            assert len((tmp_path / "provider.jsonl").read_bytes().splitlines()) == 400
    finally:
        connection.close()
        run.journal.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux PTY and /proc evidence")
def test_cleanup_reaps_provider_even_when_its_parent_already_disappeared(tmp_path):
    run = SOAK.SoakRun([sys.executable], tmp_path)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                               start_new_session=True)
    append_provider_event(tmp_path / "provider.jsonl", {
        "kind": "started", "pid": process.pid, "ppid": 999999999})
    try:
        with pytest.raises(SUPPORT.SoakFailure, match="disappeared"):
            run._read_provider_log()
        run._cleanup()
        assert process.wait(timeout=5) < 0
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            run.journal.database.execute("SELECT 1")
        report = run.report("failed", "parent disappeared")
        assert report["forced_cleanup_pids"] == [process.pid]
        assert report["executable"]["path"] == str(Path(sys.executable).resolve())
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_executable_report_binds_the_tested_bytes(tmp_path):
    executable = tmp_path / "binary"
    executable.write_bytes(b"binary-evidence")
    report = SUPPORT.executable_evidence([str(executable)])
    assert report["path"] == str(executable)
    assert report["sha256"] == hashlib.sha256(b"binary-evidence").hexdigest()


def test_composer_requires_fresh_busy_to_ready_and_handles_fragmented_ansi():
    view = SUPPORT.ComposerObservation()
    view.feed("\x1b[35;5HAsk pi-batch…".encode())
    previous = view.revision
    assert not view.ready_after(previous)
    view.feed("\x1b[1;5HQueue follow-up\x1b[35;5HAsk pi-batch…".encode())
    assert not view.ready_after(previous)  # Wrong row and an old ready repaint.
    for byte in b"\x1b[35;5HQueue \x1b[31mfollow-up":
        view.feed(bytes([byte]))
    assert view.phase == "busy" and not view.ready_after(previous)
    for byte in "\x1b[35;5H\x1b[7mA\x1b[0msk pi-batch…".encode():
        view.feed(bytes([byte]))
    assert view.ready_after(previous)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="POSIX provider transport")
def test_provider_terminal_requires_matching_ui_release_token(tmp_path):
    environment, _ = SUPPORT.prepare_environment(tmp_path)
    process = subprocess.Popen([sys.executable, str(TOOLS / "tui_soak_provider.py")],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               text=True, env=environment)
    try:
        process.stdin.write(json.dumps({"id": "one", "type": "prompt", "message": "soak:success:one"}) + "\n")
        process.stdin.flush()
        assert json.loads(process.stdout.readline())["type"] == "response"
        assert json.loads(process.stdout.readline())["type"] == "agent_start"
        release = Path(environment["TUI_SOAK_RELEASE_FILE"])
        release.write_text(json.dumps({"message": "wrong-turn", "pid": process.pid}))
        assert select.select([process.stdout], [], [], 0.05)[0] == []
        release.write_text(json.dumps({"message": "soak:success:one", "pid": process.pid}))
        assert select.select([process.stdout], [], [], 2)[0]
        events = [json.loads(process.stdout.readline())["type"] for _ in range(4)]
        assert events == ["message_start", "message_end", "agent_end", "agent_settled"]
        process.stdin.close()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


def _delayed_finish_entrypoint(tmp_path):
    wrapper = tmp_path / "delayed-finish.py"
    wrapper.write_text(
        "import functools, json, os, pathlib, sys, time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from pbatch.tui import PiBatchTUI\n"
        "from pbatch.cli import main\n"
        "capture = PiBatchTUI._capture_runtime_finish\n"
        "submission = PiBatchTUI._handle_busy_submission\n"
        "def record(kind, **value):\n"
        "    path = pathlib.Path(os.environ['TUI_SOAK_PROVIDER_LOG']).with_name('ui-race.jsonl')\n"
        "    with path.open('a') as output:\n"
        "        output.write(json.dumps(dict(kind=kind, **value)) + '\\n')\n"
        "def delayed(self):\n"
        "    capture(self)\n"
        "    if self._runtime_view.turn_state == 'FAILED':\n"
        "        record('finish_deferred', busy=self._busy)\n"
        "        time.sleep(0.75)\n"
        "        record('finish_released', busy=self._busy)\n"
        "def submitted(self, value, invocation):\n"
        "    if value == '/new':\n"
        "        record('new_submitted', busy=self._busy)\n"
        "    return submission(self, value, invocation)\n"
        "PiBatchTUI._capture_runtime_finish = delayed\n"
        "PiBatchTUI._handle_busy_submission = submitted\n"
        "raise SystemExit(main())\n", encoding="utf-8")
    return wrapper


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux PTY and /proc evidence")
def test_real_pty_waits_for_ui_after_terminal_journal_before_new(tmp_path):
    wrapper = _delayed_finish_entrypoint(tmp_path)
    run = SOAK.SoakRun([sys.executable, str(wrapper)], tmp_path)
    report = run.run(cycles=1)
    traces = [json.loads(line) for line in (tmp_path / "ui-race.jsonl").read_text().splitlines()]
    assert any(event["kind"] == "finish_deferred" and event["busy"] for event in traces)
    assert [event["busy"] for event in traces if event["kind"] == "new_submitted"] == [False] * 3
    commands = [json.loads(line) for line in (tmp_path / "commands.jsonl").read_text().splitlines()]
    assert [item["command"] for item in commands].count("/new") == 3
    assert report["status"] == "passed"
    assert (tmp_path / "terminal-tail.log").stat().st_size <= 128 * 1024


def test_resource_allowances_fail_on_growth_instead_of_hiding_it():
    samples = [{"fds": 8, "rss_kib": 1024, "threads": 3},
               {"fds": 10, "rss_kib": 3072, "threads": 3}]
    with pytest.raises(SUPPORT.SoakFailure, match="FD growth"):
        SUPPORT.resource_growth(samples, 1, 100)
    with pytest.raises(SUPPORT.SoakFailure, match="RSS growth"):
        SUPPORT.resource_growth(samples, 8, 1)
    assert SUPPORT.resource_growth(samples, 8, 100)["peak_growth"]["fds"] == 2


@pytest.mark.parametrize("flags", [
    ["--cycles", "0"], ["--duration", "nan"], ["--duration", "-1"],
    ["--step-timeout", "inf"], ["--turn-timeout", "0"],
    ["--max-fd-growth", "-1"], ["--max-rss-growth-mib", "nan"],
])
def test_invalid_soak_limits_fail_before_launch(flags):
    with pytest.raises(SystemExit) as error:
        SOAK.parse_args([sys.executable, *flags])
    assert error.value.code == 2


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux PTY and /proc evidence")
def test_same_tui_process_survives_faults_and_recovery(tmp_path):
    run = SOAK.SoakRun([sys.executable, str(ROOT / "pi-batch.py")], tmp_path)
    report = run.run(cycles=1)
    assert report["status"] == "passed"
    assert report["cycles_completed"] == 1 and report["recoveries"] == 3
    assert report["turns"] == {"succeeded": 5, "provider_failed": 1,
                               "timeout_unknown": 1, "cancel_unknown": 1}
    assert report["provider_processes_alive_after_exit"] == []
    assert report["single_tui_process"] and report["exit_code"] == 0
    requests = [json.loads(line) for line in (tmp_path / "provider.jsonl").read_text().splitlines()]
    assert {item["ppid"] for item in requests} == {report["tui_pid"]}
    assert len({item["pid"] for item in requests}) == 3
    assert report["journal"]["sessions"] == 4
    assert report["journal"]["kinds"]["turn.completed"] == 5
    assert report["actual_seconds"] >= 2
    assert "simulation" in report["provider"]
