"""Artifact gates fail closed and emit bounded, content-free invocation receipts."""

import hashlib
import json
import math
import os
from pathlib import Path
import threading

import pytest

from pbatch import config, gate_fix, runner, validation_receipts as receipts
from pbatch.models import Stage, Task, TaskResult
from pbatch.process_exec import ValidationResult


@pytest.fixture(autouse=True)
def private_runtime(tmp_path, monkeypatch):
    runtime = tmp_path.parent / (tmp_path.name + "-runtime")
    monkeypatch.setattr(config, "_DATA_DIR_OVERRIDE", str(runtime))
    monkeypatch.setattr(config, "MEMORY_MODE", "off")
    monkeypatch.setattr(config, "STREAM_OUTPUT", "none")
    monkeypatch.setattr(runner, "record_task", lambda result: None)


def test_configured_validator_without_output_fails_closed(tmp_path, monkeypatch, capsys):
    task = Task(prompt="private prompt", cwd=str(tmp_path))
    result = TaskResult(task, True, stdout="must not publish this body")
    invoked = []
    monkeypatch.setattr(runner, "run_validation", lambda *args: invoked.append(args))

    assert runner._save_validated(task, result, "true") is False
    assert result.validation_ok is False
    assert "artifact" in result.validation_stderr
    assert "output" in result.validation_stderr
    assert len(result.validation_stderr) <= 256
    assert invoked == []
    assert capsys.readouterr().out == ""
    assert receipts.read_receipts(str(tmp_path)) == []


def test_missing_output_reason_survives_task_runner(tmp_path, monkeypatch):
    task = Task(prompt="private prompt", cwd=str(tmp_path))
    monkeypatch.setattr(runner, "run_task", lambda *args, **kwargs: TaskResult(
        task, True, stdout="private result"))
    result = runner._run_with_gate(task, "true", 1, 1)
    assert result.success is False
    assert "artifact" in result.reason and "output" in result.reason


def test_explicit_empty_override_allows_stdout(tmp_path, monkeypatch, capsys):
    task = Task(prompt="analysis", cwd=str(tmp_path), validate="")
    monkeypatch.setattr(runner, "run_task", lambda *args, **kwargs: TaskResult(
        task, True, stdout="intentional ungated analysis"))
    result = runner._run_with_gate(task, "exit 7", 1, 1)
    assert result.success is True
    assert result.validation_ok is None
    assert capsys.readouterr().out == "intentional ungated analysis"
    assert receipts.read_receipts(str(tmp_path)) == []


@pytest.mark.parametrize("command,passed", [
    ("true", True), ("exit 7", False),
    ("printf '%s' '{\"status\":\"fail\"}'", False),
])
def test_existing_artifact_gate_still_rejects_before_publication(
        tmp_path, command, passed):
    task = Task(prompt="write", output="artifact.md", cwd=str(tmp_path))
    result = TaskResult(task, True, stdout="candidate")
    assert runner._save_validated(task, result, command) is passed
    assert task.output_path().exists() is passed
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize("outcome", [
    ValidationResult(True, 0, "pass", "", False),
    ValidationResult(False, 7, "", "failed", False),
    ValidationResult(False, -1, "", "deadline", True),
])
def test_one_receipt_per_actual_call_with_measured_outcome(tmp_path, monkeypatch, outcome):
    task = Task(prompt="write", output="out.md", cwd=str(tmp_path))
    result = TaskResult(task, True, stdout="candidate", task_id="task-id", stage_id="stage-id")
    calls, events = [], []
    ticks = iter([12.0, 12.25])

    def clock():
        events.append("clock")
        return next(ticks)

    def invoke(command, cwd):
        events.append("validator")
        calls.append((command, cwd))
        assert Path(command.removeprefix("test -f ")).is_file()
        return outcome

    monkeypatch.setattr(receipts, "_clock", clock)
    monkeypatch.setattr(runner, "run_validation", invoke)
    assert runner._save_validated(task, result, "test -f {output}") is outcome.ok
    records = receipts.read_receipts(str(tmp_path))
    assert len(calls) == len(records) == 1
    assert events == ["clock", "validator", "clock"]
    record = records[0]
    assert record["command_hash"] == hashlib.sha256(b"test -f {output}").hexdigest()
    assert record["exit_code"] == outcome.exit_code
    assert record["timed_out"] is outcome.timed_out
    assert record["ok"] is outcome.ok
    assert record["duration_seconds"] == 0.25
    assert record["artifact_bytes"] == len(b"candidate")
    assert record["artifact_digest"] == hashlib.sha256(b"candidate").hexdigest()
    assert record["artifact_digest_bytes"] == len(b"candidate")
    assert record["task_id_hash"] == receipts.command_identity("task-id")
    assert record["stage_id_hash"] == receipts.command_identity("stage-id")
    assert record["scope"] == "task"


@pytest.mark.parametrize("ticks", [
    (None, None), (math.nan, 2.0), (1.0, math.inf), (2.0, 1.0), (1.0, 1.0),
    (0, 1 << 1024), (True, False), ("unknown", 2.0),
])
def test_unmeasurable_duration_is_null_not_zero(tmp_path, monkeypatch, ticks):
    artifact = tmp_path / "artifact.md"
    artifact.write_text("body", encoding="utf-8")
    clock = iter(ticks)
    monkeypatch.setattr(receipts, "_clock", lambda: next(clock))
    outcome = ValidationResult(True, 0, "", "", False)
    actual = receipts.observe_validation(lambda *args: outcome, "true", str(tmp_path), artifact)
    assert actual is outcome
    record, = receipts.read_receipts(str(tmp_path))
    assert record["duration_seconds"] is None
    assert record["exit_code"] == 0 and record["ok"] is True


def test_clock_failure_is_observational_and_does_not_leak(tmp_path, monkeypatch, caplog):
    def unavailable():
        raise OSError("clock-secret")

    caplog.set_level("DEBUG", logger="pi-batch")
    monkeypatch.setattr(receipts.time, "perf_counter", unavailable)
    outcome = ValidationResult(True, 0, "", "", False)
    assert receipts.observe_validation(
        lambda *args: outcome, "true", str(tmp_path), None) is outcome
    record, = receipts.read_receipts(str(tmp_path))
    assert record["duration_seconds"] is None
    assert "clock unavailable" in caplog.text
    assert "clock-secret" not in caplog.text


def test_executor_exception_retains_unknown_outcome_and_propagates(tmp_path):
    def invoke(*args):
        raise RuntimeError("private failure detail")

    with pytest.raises(RuntimeError, match="private failure"):
        receipts.observe_validation(invoke, "private command", str(tmp_path), None)
    record, = receipts.read_receipts(str(tmp_path))
    for key in ("exit_code", "timed_out", "ok", "artifact_bytes", "artifact_digest"):
        assert record[key] is None
    assert record["duration_seconds"] > 0
    assert "private" not in receipts.receipts_path(str(tmp_path)).read_text()


def test_real_timeout_records_the_single_bounded_execution(tmp_path):
    artifact = tmp_path / "artifact.md"
    artifact.write_text("body", encoding="utf-8")
    calls = []

    def invoke(command, cwd):
        calls.append(command)
        return runner.run_validation(command, cwd, timeout=0.05)

    outcome = receipts.observe_validation(invoke, "sleep 5", str(tmp_path), artifact)
    assert calls == ["sleep 5"]
    record, = receipts.read_receipts(str(tmp_path))
    assert record["timed_out"] is outcome.timed_out is True
    assert record["exit_code"] == outcome.exit_code == -1
    assert record["ok"] is False
    assert record["duration_seconds"] > 0


def test_reuse_and_legacy_single_validator_path_emit_once(tmp_path):
    artifact = tmp_path / "artifact.md"
    artifact.write_text("reused body", encoding="utf-8")
    assert runner.revalidate_existing(artifact, "test -f {output}", str(tmp_path))
    spec = config.ValidatorSpec(cmd="test -f {output}")
    assert runner._revalidate_one(spec, artifact, str(tmp_path))
    records = receipts.read_receipts(str(tmp_path))
    assert len(records) == 2
    assert {record["scope"] for record in records} == {"reuse"}
    assert {record["command_hash"] for record in records} == {
        receipts.command_identity("test -f {output}")}
    assert all(record["artifact_bytes"] == len(b"reused body") for record in records)
    assert all(record["ok"] is True for record in records)


def test_reuse_cancellation_reaches_the_same_single_invocation(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.md"
    artifact.write_text("body", encoding="utf-8")
    event, calls = threading.Event(), []

    def invoke(command, cwd, cancel_event=None):
        calls.append(cancel_event)
        return ValidationResult(True, 0, "", "", False)

    monkeypatch.setattr(runner, "run_validation", invoke)
    assert runner.revalidate_existing(artifact, "true", str(tmp_path), event)
    assert calls == [event]
    assert len(receipts.read_receipts(str(tmp_path))) == 1


def test_gate_fix_call_site_is_unchanged_and_labeled(tmp_path, monkeypatch):
    from pbatch import memory

    gate = tmp_path / "review.md"
    gate.write_text("VERDICT: FAIL - missing evidence", encoding="utf-8")
    stage = Stage(name="review", cwd=str(tmp_path))
    monkeypatch.setattr(runner, "run_task", lambda task, **kwargs: TaskResult(
        task, True, stdout="fixed with regression coverage"))
    monkeypatch.setattr(memory, "record_task", lambda result: None)
    result, report = gate_fix.run_gate_fix(stage, [str(gate)], 1, "true", "session")
    assert result.success and Path(report).is_file()
    record, = receipts.read_receipts(str(tmp_path))
    assert record["scope"] == "gate-fix"
    assert record["ok"] is True


@pytest.mark.parametrize("passed", [True, False])
def test_write_failure_never_changes_gate_decision(tmp_path, monkeypatch, caplog, passed):
    blocker = tmp_path.parent / (tmp_path.name + "-blocker")
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(config, "_DATA_DIR_OVERRIDE", str(blocker / "runtime"))
    caplog.set_level("DEBUG", logger="pi-batch")
    task = Task(prompt="write", output="out.md", cwd=str(tmp_path))
    result = TaskResult(task, True, stdout="body")
    assert runner._save_validated(task, result, "true" if passed else "exit 7") is passed
    assert task.output_path().exists() is passed
    assert "validation receipts: observation failed" in caplog.text


def test_rotation_failure_does_not_leak_exception_arguments(tmp_path, monkeypatch, caplog):
    def broken(*args):
        raise OSError("secret-command --token private-token")

    caplog.set_level("DEBUG", logger="pi-batch")
    monkeypatch.setattr(receipts.storage, "_rotate", broken)
    task = Task(prompt="p", output="out.md", cwd=str(tmp_path))
    assert runner._save_validated(task, TaskResult(task, True, stdout="body"), "true")
    assert "observation failed" in caplog.text
    assert "private-token" not in caplog.text
    assert "secret-command" not in caplog.text


def test_storage_is_private_outside_workspace_and_reads_do_not_create(tmp_path):
    ledger = receipts.receipts_path(str(tmp_path))
    assert receipts.read_receipts(str(tmp_path)) == []
    assert not ledger.parent.exists()
    outcome = ValidationResult(True, 0, "", "", False)
    receipts.observe_validation(lambda *args: outcome, "true", str(tmp_path), None)
    assert not ledger.is_relative_to(tmp_path)
    if os.name == "posix":
        assert ledger.stat().st_mode & 0o777 == 0o600
        assert ledger.parent.stat().st_mode & 0o777 == 0o700


def test_in_workspace_data_override_drops_observation_not_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_DATA_DIR_OVERRIDE", str(tmp_path / "runtime"))
    outcome = ValidationResult(True, 0, "", "", False)
    assert receipts.observe_validation(
        lambda *args: outcome, "true", str(tmp_path), None) is outcome
    assert not (tmp_path / "runtime").exists()


def test_bounded_rotation_preserves_recent_complete_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts.storage, "RECEIPTS_MAX_BYTES", 2400)
    monkeypatch.setattr(receipts.storage, "RECEIPTS_KEEP_LINES", 2)
    monkeypatch.setattr(receipts.storage, "RECEIPTS_MAX_LINE_BYTES", 1024)
    outcome = ValidationResult(True, 0, "", "", False)
    for index in range(25):
        receipts.observe_validation(
            lambda *args: outcome, f"validator-{index}", str(tmp_path), None)
        assert receipts.receipts_path(str(tmp_path)).stat().st_size <= 2400
    raw = receipts.receipts_path(str(tmp_path)).read_bytes()
    lines = raw.splitlines()
    assert all(len(line) <= 1024 and json.loads(line) for line in lines)
    records = receipts.read_receipts(str(tmp_path), 10_000)
    assert records[-1]["command_hash"] == receipts.command_identity("validator-24")
    assert records[0]["command_hash"] != receipts.command_identity("validator-0")
    assert len(receipts.read_receipts(str(tmp_path), 1)) == 1
    assert list(receipts.receipts_path(str(tmp_path)).parent.glob("*.tmp")) == []


def test_line_budget_failure_never_changes_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(receipts.storage, "RECEIPTS_MAX_LINE_BYTES", 8)
    outcome = ValidationResult(True, 0, "", "", False)
    assert receipts.observe_validation(
        lambda *args: outcome, "true", str(tmp_path), None) is outcome
    assert not receipts.receipts_path(str(tmp_path)).exists()


def test_receipts_never_store_prompts_bodies_paths_or_raw_arguments(tmp_path, monkeypatch):
    template = "secret-validator --token argument-secret {output}"
    task = Task(prompt="prompt-secret", output="path-secret.md", cwd=str(tmp_path),
                env={"TOKEN": "env-secret"}, memory={"stage": "stage-secret"})
    result = TaskResult(task, True, stdout="body-secret", stderr="agent-stderr-secret",
                        task_id="task-secret", stage_id="stage-secret")
    outcome = ValidationResult(True, 0, "validator-stdout-secret", "stderr-secret", False)
    calls = []

    def invoke(command, cwd):
        calls.append(command)
        return outcome

    monkeypatch.setattr(runner, "run_validation", invoke)
    assert runner._save_validated(task, result, template)
    assert runner._save_validated(task, result, template)
    assert runner.revalidate_existing(task.output_path(), template, str(tmp_path))
    records = receipts.read_receipts(str(tmp_path))
    raw = receipts.receipts_path(str(tmp_path)).read_text()
    assert len(calls) == len(records) == 3
    assert "secret" not in raw and str(tmp_path) not in raw
    assert template not in raw and "secret-validator" not in raw
    assert {record["command_hash"] for record in records} == {
        hashlib.sha256(template.encode()).hexdigest()}


@pytest.mark.parametrize("kind", ["missing", "directory", "symlink", "empty", "large"])
def test_artifact_identity_is_bounded_and_unknown_is_null(tmp_path, kind):
    artifact = tmp_path / "artifact.md"
    if kind == "directory":
        artifact.mkdir()
    elif kind == "symlink":
        artifact.symlink_to(tmp_path / "missing.md")
    elif kind in ("empty", "large"):
        artifact.write_bytes(b"" if kind == "empty" else
                             b"x" * receipts.DIGEST_PREFIX_BYTES + b"unhashed tail")
    outcome = ValidationResult(True, 0, "", "", False)
    receipts.observe_validation(lambda *args: outcome, "true", str(tmp_path), artifact)
    record, = receipts.read_receipts(str(tmp_path))
    if kind in ("missing", "directory", "symlink"):
        assert record["artifact_bytes"] is None
        assert record["artifact_digest"] is None
        assert record["artifact_digest_bytes"] is None
    else:
        prefix = b"" if kind == "empty" else b"x" * receipts.DIGEST_PREFIX_BYTES
        assert record["artifact_bytes"] == artifact.stat().st_size
        assert record["artifact_digest"] == hashlib.sha256(prefix).hexdigest()
        assert record["artifact_digest_bytes"] == len(prefix)


def test_all_invocations_recorded_while_and_failure_uses_declaration_order(tmp_path, monkeypatch):
    task = Task(prompt="write", output="out.md", cwd=str(tmp_path))
    result = TaskResult(task, True, stdout="body")
    calls = []

    def invoke(command, cwd):
        calls.append(command)
        code = {"first": 7, "second": 9, "third": 0}[command]
        return ValidationResult(code == 0, code, "", command, False)

    monkeypatch.setattr(runner, "run_validation", invoke)
    assert not runner._save_validated(task, result, "first,second,third")
    assert set(calls) == {"first", "second", "third"}
    assert result.validation_exit == 7
    records = receipts.read_receipts(str(tmp_path))
    assert len(records) == 3
    assert {record["exit_code"] for record in records} == {0, 7, 9}
    assert not task.output_path().exists()


def test_deferred_and_nonapplicable_validators_are_not_invented_invocations(tmp_path, monkeypatch):
    monkeypatch.setitem(config.VALIDATOR_SPECS, "repo_gate",
                        config.ValidatorSpec(cmd="false", scope="repo"))
    monkeypatch.setitem(config.VALIDATOR_SPECS, "conditional_gate",
                        config.ValidatorSpec(cmd="false", requires_any=("missing",)))
    task = Task(prompt="write", output="out.md", cwd=str(tmp_path))
    assert runner._save_validated(task, TaskResult(task, True, stdout="body"),
                                  "repo_gate,conditional_gate")
    assert receipts.read_receipts(str(tmp_path)) == []


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "public-file"])
def test_unsafe_ledger_target_drops_observation_without_touching_target(tmp_path, kind):
    ledger = receipts.receipts_path(str(tmp_path))
    ledger.parent.mkdir(parents=True, mode=0o700)
    target = tmp_path / "untouched"
    target.write_text("original", encoding="utf-8")
    if kind == "symlink":
        ledger.symlink_to(target)
    elif kind == "directory":
        ledger.mkdir()
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO creation is unavailable on this platform")
        os.mkfifo(ledger, mode=0o600)
    else:
        ledger.write_text("public ledger", encoding="utf-8")
        ledger.chmod(0o644)
    outcome = ValidationResult(True, 0, "", "", False)
    assert receipts.observe_validation(
        lambda *args: outcome, "true", str(tmp_path), None) is outcome
    assert receipts.read_receipts(str(tmp_path)) == []
    assert target.read_text() == "original"
    if kind == "public-file":
        assert ledger.read_text() == "public ledger"


def test_descriptor_is_closed_when_stream_wrapper_cannot_be_created(tmp_path, monkeypatch):
    opened = []
    real_open = os.open

    def capture_open(*args):
        descriptor = real_open(*args)
        opened.append(descriptor)
        return descriptor

    def broken_wrapper(*args):
        raise OSError("cannot create stream wrapper")

    monkeypatch.setattr(receipts.os, "open", capture_open)
    monkeypatch.setattr(receipts.os, "fdopen", broken_wrapper)
    with pytest.raises(OSError, match="stream wrapper"):
        receipts._open_file(tmp_path / "owned", os.O_WRONLY | os.O_CREAT, "ab")
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])
