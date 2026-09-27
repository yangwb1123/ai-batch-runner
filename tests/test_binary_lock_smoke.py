"""Lock smoke must detect refusal, broken waiting, and cleanup its real children."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
try:
    import binary_smoke_lock as LOCKS
    SPEC = importlib.util.spec_from_file_location("lock_binary_smoke", TOOLS / "smoke-binary.py")
    SMOKE = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(SMOKE)
finally:
    sys.path.remove(str(TOOLS))


def _fixture(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    environment = SMOKE.isolated_environment(tmp_path)
    command = [sys.executable, str(ROOT / "pi-batch.py")]
    config = SMOKE._command_output([*command, "config", "explain", "--json"],
                                   workspace, environment, 15, "config", (0,))
    SMOKE.prepare_fake_agent(workspace, config)
    assert "--no-lock" not in SMOKE._task_flags()
    checks = []
    run = LOCKS._LockSmoke(command, workspace, environment, 10, SMOKE._task_flags(),
                          SMOKE._command_output, SMOKE.check_artifact,
                          lambda name, status, **kw: checks.append((name, status, kw)),
                          SMOKE._stop_process)
    return run, checks


def test_source_lock_handshake_checks_conflicts_ps_waiting_and_release(tmp_path):
    run, checks = _fixture(tmp_path)
    run.run()
    assert len(checks) == 4 and all(status == "passed" for _, status, _ in checks)
    assert not list(Path(run.environment["PBATCH_RUNTIME_HOME"]).rglob(".pi-batch.lock"))
    assert all(child.process.poll() == 0 for child in run.children)
    assert run._event("holder", "done") and run._event("waiter", "done")


@pytest.mark.parametrize("failure", ["ps", "waiting"])
def test_lock_failure_is_recorded_and_still_releases_owned_provider(tmp_path, monkeypatch, failure):
    run, checks = _fixture(tmp_path)
    original = run.execute

    def execute(command, *args):
        result = original(command, *args)
        if failure == "ps" and "ps" in command:
            return "[]"
        return result

    run.execute = execute
    if failure == "waiting":
        original_wait = run._wait
        def wait(predicate, label, **kwargs):
            if label == "waiter lock queue":
                raise LOCKS.LockSmokeFailure("injected waiter observation deadline")
            return original_wait(predicate, label, **kwargs)
        monkeypatch.setattr(run, "_wait", wait)
    with pytest.raises(LOCKS.LockSmokeFailure):
        run.run()
    assert checks[-1][1] == "failed" and checks[-1][2]["reason"]
    assert run._event("holder", "done")
    assert all(child.process.poll() is not None for child in run.children)


def test_isolation_redirects_actual_homes_and_provider_handshake_is_not_user_state(tmp_path):
    environment = SMOKE.isolated_environment(tmp_path)
    for key in ("HOME", "USERPROFILE", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_CACHE_HOME", "APPDATA", "LOCALAPPDATA"):
        assert Path(environment[key]).is_relative_to(tmp_path)
    assert not list(Path(environment["PATH"]).iterdir())


def test_waiting_probe_rejects_a_process_that_bypasses_the_lock(tmp_path, monkeypatch):
    run, checks = _fixture(tmp_path)
    original = run._argv
    def argv(output, session, *extra):
        command = original(output, session, *extra)
        return [*command, "--no-lock"] if "--wait-lock" in extra else command
    monkeypatch.setattr(run, "_argv", argv)
    with pytest.raises(LOCKS.LockSmokeFailure):
        run.run()
    assert checks[-1][0] == "instance lock queued handoff and release"
    assert checks[-1][1] == "failed"
    assert run._event("holder", "done")
    assert all(child.process.poll() is not None for child in run.children)


def test_subprocess_diagnostics_do_not_move_the_child_output_offset(tmp_path):
    marker = tmp_path / "continue"
    code = ("import pathlib,sys,time; print('first', flush=True); "
            f"p=pathlib.Path({str(marker)!r}); "
            "exec('while not p.exists(): time.sleep(0.01)'); print('second', flush=True)")
    process = LOCKS._Process([sys.executable, "-c", code], tmp_path,
                             dict(os.environ), SMOKE._stop_process)
    try:
        import time
        deadline = time.monotonic() + 5
        while "first" not in process.diagnostics() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "first" in process.diagnostics()
        marker.write_text("continue")
        process.finish(5)
        assert process.diagnostics().splitlines() == ["first", "second"]
    finally:
        process.close()
