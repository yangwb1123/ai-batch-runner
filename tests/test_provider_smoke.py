"""Provider acceptance must prove real turns and reject misleading evidence."""

import importlib.util
from contextlib import contextmanager
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
try:
    import provider_smoke_support as SUPPORT
    SPEC = importlib.util.spec_from_file_location("smoke_provider", TOOLS / "smoke-provider.py")
    SMOKE = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(SMOKE)
    AUDIT_SPEC = importlib.util.spec_from_file_location("revalidate_provider", TOOLS / "revalidate-provider.py")
    AUDIT = importlib.util.module_from_spec(AUDIT_SPEC)
    AUDIT_SPEC.loader.exec_module(AUDIT)
finally:
    sys.path.remove(str(TOOLS))


def _event(kind, *, turn="turn_1", seq=1, payload=None):
    return {"event_id": f"evt_{seq}", "seq": seq, "session_id": "ses_1", "run_id": "run_1",
            "workspace_id": "ws_1", "actor_id": "agent_main",
            "kind": kind, "turn_id": turn, "payload": payload or {}}


def test_live_environment_preserves_auth_without_copying_ambient_runner_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-not-a-key")
    monkeypatch.setenv("HOME", "/account-home")
    monkeypatch.setenv("CODEX_HOME", "/account-codex")
    monkeypatch.setenv("PBATCH_DATA_DIR", "/unrelated-project")
    monkeypatch.setenv("PYTHONPATH", "/unrelated-module")
    environment = SUPPORT.prepare_environment(tmp_path)
    assert environment["HOME"] == "/account-home"
    assert environment["CODEX_HOME"] == "/account-codex"
    assert environment["OPENAI_API_KEY"] == "fixture-not-a-key"
    assert "PBATCH_DATA_DIR" not in environment and "PYTHONPATH" not in environment
    assert Path(environment["PBATCH_SCRIPT_DIR"]) == tmp_path / "workspace"
    assert 'default_model: ""' in (tmp_path / "workspace" / "pi-batch.yaml").read_text()
    assert not list(tmp_path.rglob("*auth*"))


def test_requires_explicit_model_call_authorization():
    with pytest.raises(SystemExit) as error:
        SMOKE.parse_args([sys.executable, "--runtime", "pi-rpc"])
    assert error.value.code == 2


@pytest.mark.parametrize("runtime, name", [("codex-app-server", "codex"), ("pi-rpc", "pi")])
def test_runtime_path_resolves_exact_versioned_provider(tmp_path, runtime, name):
    environment = SUPPORT.prepare_environment(tmp_path)
    provider = tmp_path / "selected-provider"
    provider.write_text("selected provider")
    alias = SUPPORT.bind_provider(tmp_path, environment, provider, runtime)
    assert alias.name == name and alias.resolve() == provider
    assert environment["PATH"].split(":")[0] == str(alias.parent)


def test_journal_refuses_duplicate_terminals_and_extra_turns(tmp_path):
    journal = SUPPORT.ProviderJournal(tmp_path / "runtime")
    try:
        journal._accept(_event("turn.started"))
        journal._accept(_event("turn.completed", seq=2, payload={"state": "SUCCEEDED"}))
        with pytest.raises(SUPPORT.SoakFailure, match="multiple terminal"):
            journal._accept(_event("turn.completed", seq=3))
        for number in range(2, 5):
            journal._accept(_event("turn.started", turn=f"turn_{number}", seq=number + 3))
        with pytest.raises(SUPPORT.SoakFailure, match="fifth turn"):
            journal._accept(_event("turn.started", turn="turn_5", seq=8))
    finally:
        journal.close()


@pytest.mark.parametrize("field", ["workspace_id", "session_id", "run_id"])
def test_foreign_scope_message_cannot_supply_success_marker(tmp_path, field):
    journal = SUPPORT.ProviderJournal(tmp_path / "runtime")
    try:
        journal._accept(_event("turn.started"))
        message = _event("message.completed", seq=2, payload={"text": "MARKER"})
        message[field] = "foreign"
        with pytest.raises(SUPPORT.SoakFailure, match="crossed"):
            journal._accept(message)
        journal._accept(_event("turn.completed", seq=3, payload={"state": "SUCCEEDED"}))
        with pytest.raises(SUPPORT.SoakFailure, match="exact requested marker"):
            SUPPORT.validate_success(journal.turns["turn_1"], "MARKER")
    finally:
        journal.close()


@pytest.mark.parametrize("kind", ["message.completed", "message.delta"])
def test_child_actor_cannot_supply_reply_or_cancellation_evidence(tmp_path, kind):
    journal = SUPPORT.ProviderJournal(tmp_path / "runtime")
    try:
        journal._accept(_event("turn.started"))
        message = _event(kind, seq=2, payload={"text": "MARKER"})
        message["actor_id"] = "agent_child"
        journal._accept(message)
        journal._accept(_event("turn.completed", seq=3, payload={"state": "SUCCEEDED"}))
        turn = journal.turns["turn_1"]
        assert turn["messages"] == [] and turn["deltas"] == 0
        with pytest.raises(SUPPORT.SoakFailure, match="exact requested marker"):
            SUPPORT.validate_success(turn, "MARKER")
        turn["cancel_requested"] = True
        with pytest.raises(SUPPORT.SoakFailure, match="active model response"):
            SUPPORT.validate_cancel(turn)
    finally:
        journal.close()


@pytest.mark.parametrize("kind", ["message.completed", "message.delta"])
def test_late_text_cannot_fill_terminal_evidence(tmp_path, kind):
    journal = SUPPORT.ProviderJournal(tmp_path / "runtime")
    try:
        journal._accept(_event("turn.started"))
        journal._accept(_event("turn.completed", seq=2, payload={"state": "SUCCEEDED"}))
        with pytest.raises(SUPPORT.SoakFailure, match="after its terminal"):
            journal._accept(_event(kind, seq=3, payload={"text": "MARKER"}))
        assert journal.turns["turn_1"]["messages"] == []
        assert journal.turns["turn_1"]["deltas"] == 0
    finally:
        journal.close()


def test_nontext_delta_cannot_prove_active_response(tmp_path):
    journal = SUPPORT.ProviderJournal(tmp_path / "runtime")
    try:
        journal._accept(_event("turn.started"))
        with pytest.raises(SUPPORT.SoakFailure, match="bounded text"):
            journal._accept(_event("message.delta", seq=2, payload={"text": {"bad": True}}))
        assert journal.turns["turn_1"]["deltas"] == 0
    finally:
        journal.close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process ownership")
@pytest.mark.parametrize("repeat", range(3))
def test_version_probe_rejects_and_cleans_detached_child(tmp_path, repeat):
    provider = tmp_path / "provider"
    marker = tmp_path / "child-pid"
    provider.write_text(
        "#!" + sys.executable + "\nimport pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "start_new_session=True)\n"
        "pathlib.Path('child-pid').write_text(str(child.pid))\n"
        "print('0.87.1')\n")
    provider.chmod(0o755)
    import os
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(SUPPORT.SoakFailure, match="live owned processes"):
            SUPPORT.provider_version(provider, dict(os.environ), tmp_path)
        identity = SUPPORT.process_identity(int(marker.read_text()))
        deadline = time.monotonic() + 5
        while identity and SUPPORT.is_alive(identity) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not identity or not SUPPORT.is_alive(identity)
        assert unrelated.poll() is None
    finally:
        unrelated.kill()
        unrelated.wait(timeout=5)
        if marker.exists():
            identity = SUPPORT.process_identity(int(marker.read_text()))
            if identity and SUPPORT.is_alive(identity):
                os.kill(identity["pid"], 9)


@pytest.mark.parametrize("terminal", [
    {"kind": "turn.completed", "state": "SUCCEEDED"},
    {"kind": "turn.failed", "state": "FAILED", "reason": "authentication failed"},
    {"kind": "turn.failed", "state": "UNKNOWN", "reason": "timed out"},
])
def test_cancel_does_not_accept_completion_errors_or_unrelated_unknown(terminal):
    turn = {"starts": 1, "deltas": 1, "cancel_requested": True, "terminal": terminal}
    with pytest.raises(SUPPORT.SoakFailure, match="not confirmed"):
        SUPPORT.validate_cancel(turn)


@pytest.mark.parametrize("terminal", [
    {"kind": "turn.interrupted", "state": "INTERRUPTED"},
    {"kind": "turn.failed", "state": "UNKNOWN", "reason": "turn cancelled"},
])
def test_cancel_requires_active_response_and_preserves_honest_unknown(terminal):
    turn = {"starts": 1, "deltas": 1, "cancel_requested": True, "terminal": terminal}
    SUPPORT.validate_cancel(turn)
    turn["deltas"] = 0
    with pytest.raises(SUPPORT.SoakFailure, match="active model response"):
        SUPPORT.validate_cancel(turn)


def test_success_marker_is_not_satisfied_by_old_or_partial_text():
    turn = {"starts": 1, "messages": ["MARKER", "different final"],
            "terminal": {"kind": "turn.completed", "state": "SUCCEEDED"}}
    with pytest.raises(SUPPORT.SoakFailure, match="exact requested marker"):
        SUPPORT.validate_success(turn, "MARKER")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux PTY and /proc")
def test_owned_cleanup_leaves_unrelated_process_alive(tmp_path):
    owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    tracker = SUPPORT.OwnedProcesses(owned.pid)
    try:
        tracker.cleanup()
        assert owned.wait(timeout=5) < 0
        assert unrelated.poll() is None
        assert tracker.forced_cleanup == [owned.pid]
    finally:
        for process in (owned, unrelated):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("phase", ["opening", "first_entry", "after_child"])
def test_process_exit_during_task_enumeration_preserves_observed_children(tmp_path, monkeypatch, phase):
    _failing_task_scan(tmp_path, monkeypatch, phase, FileNotFoundError)
    assert SUPPORT.OwnedProcesses._child_pids(12345) == ({321, 654} if phase == "after_child" else set())


@pytest.mark.parametrize("phase", ["opening", "first_entry", "after_child"])
@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_task_enumeration_does_not_hide_permission_or_io_failure(tmp_path, monkeypatch, phase, error):
    _failing_task_scan(tmp_path, monkeypatch, phase, error)
    with pytest.raises(error, match="task scan failed"):
        SUPPORT.OwnedProcesses._child_pids(12345)


def _failing_task_scan(tmp_path, monkeypatch, phase, error):
    task = tmp_path / "12345"
    task.mkdir()
    (task / "children").write_text("321 654")

    def entries():
        if phase == "after_child":
            yield SimpleNamespace(path=str(task))
        raise error("task scan failed")

    @contextmanager
    def scan(path):
        assert path == Path("/proc/12345/task")
        if phase == "opening":
            raise error("task scan failed")
        yield entries()

    monkeypatch.setattr(SUPPORT.os, "scandir", scan)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux /proc thread ownership")
def test_process_tracking_discovers_children_spawned_by_worker_threads(tmp_path):
    marker = tmp_path / "child-pid"
    script = (
        "import pathlib, subprocess, sys, threading, time\n"
        "def worker():\n"
        " child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        " pathlib.Path(sys.argv[1]).write_text(str(child.pid))\n"
        " child.wait()\n"
        "threading.Thread(target=worker).start()\n"
        "time.sleep(30)\n")
    process = subprocess.Popen([sys.executable, "-c", script, str(marker)])
    tracker = SUPPORT.OwnedProcesses(process.pid)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = int(marker.read_text())
        tracker.poll()
        assert child_pid in tracker.identities
        tracker.cleanup()
        assert process.wait(timeout=5) < 0
        assert child_pid in tracker.forced_cleanup
        deadline = time.monotonic() + 5
        while tracker.live() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not tracker.live()
    finally:
        tracker.cleanup()
        process.wait(timeout=5)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux PTY and /proc")
def test_four_turn_acceptance_against_local_fixture(tmp_path, monkeypatch):
    provider = tmp_path / "fixture-pi"
    script = ROOT / "tests" / "fixtures" / "provider_acceptance_rpc.py"
    provider.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(script)]) + ' "$@"\n')
    provider.chmod(0o755)
    root = tmp_path / "evidence"
    root.mkdir()
    monkeypatch.delenv("PBATCH_TUI_TEST_MODE", raising=False)
    run = SMOKE.ProviderRun([sys.executable, str(ROOT / "pi-batch.py")], root,
                            provider=provider, runtime="pi-rpc", timeout=10, overall_timeout=40)
    report = run.run()
    assert report["status"] == "passed", report
    assert report["model_prompts_sent"] == 4
    assert [turn["label"] for turn in report["turns"]] == [
        "exact_reply", "continuation", "cancel", "recovery"]
    assert report["turns"][0]["session_id"] == report["turns"][1]["session_id"]
    assert report["turns"][0]["session_id"] != report["turns"][3]["session_id"]
    assert report["forced_cleanup_pids"] == report["owned_processes_alive_after_exit"] == []
    assert report["journal"]["duplicate_ids"] == report["journal"]["sequence_regressions"] == 0
    assert "--model" not in json.loads((root / "provider-args.json").read_text())
    SUPPORT.write_report(root / "report.json", report)
    before = {path: path.read_bytes() for path in root.rglob("*.jsonl")}
    audit = AUDIT.revalidate(root)
    assert audit["status"] == "passed" and audit["model_calls"] == 0
    assert before == {path: path.read_bytes() for path in root.rglob("*.jsonl")}
    _check_offline_scope_rejection(root)


def _check_offline_scope_rejection(root):
    for path in (root / "runtime").rglob("events.jsonl"):
        events = [json.loads(line) for line in path.read_text().splitlines()]
        message = next((event for event in events if event["kind"] == "message.completed"), None)
        if message:
            message["run_id"] = "run_foreign"
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            break
    with pytest.raises(SUPPORT.SoakFailure, match="crossed"):
        AUDIT.revalidate(root)
