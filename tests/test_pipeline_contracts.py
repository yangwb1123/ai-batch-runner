"""High-trust contracts for reuse, retries, gate-fix, and worktree safety."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from pbatch import cli, config, from_dir, gate_fix, meta, pipeline, reuse, runner
from pbatch.models import Pipeline, Stage, Task, TaskResult


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _init_repo(repo: Path) -> Path:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Pipeline Tests")
    source = repo / "source.py"
    source.write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "source.py")
    _git(repo, "commit", "-qm", "baseline")
    return source


def test_reuse_resolves_cwd_and_fingerprint_tracks_dirty_state(tmp_path):
    source = _init_repo(tmp_path)
    output = tmp_path / "artifact.md"
    output.write_text("artifact", encoding="utf-8")
    fp_clean = reuse.fingerprint("prompt", "artifact.md", "", workdir=str(tmp_path))
    reuse.write_sidecar("artifact.md", "prompt", "", workdir=str(tmp_path))
    assert reuse.reuse_decision("artifact.md", "", str(tmp_path),
                                expected_fp=fp_clean)

    source.write_text("value = 2\n", encoding="utf-8")
    fp_dirty = reuse.fingerprint("prompt", "artifact.md", "", workdir=str(tmp_path))
    assert fp_dirty != fp_clean


def test_reuse_rejects_artifact_older_than_tracked_dirty_file(tmp_path):
    source = _init_repo(tmp_path)
    output = tmp_path / "artifact.md"
    output.write_text("old", encoding="utf-8")
    future = time.time() + 20
    os.utime(output, (future, future))
    source.write_text("value = 3\n", encoding="utf-8")
    os.utime(source, (future + 10, future + 10))

    assert not reuse.reuse_decision("artifact.md", "", str(tmp_path))
    assert not output.exists()


def test_from_prompt_fingerprint_uses_effective_prompt_and_absolute_output(
        tmp_path, monkeypatch):
    calls = tmp_path / "calls"
    agent = tmp_path / "agent.sh"
    agent.write_text(
        f"#!/bin/sh\nprintf x >> {calls}\nprintf '%s\\n' \"$2\"\n",
        encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setattr(config, "AGENT_BIN", str(agent))
    spec = Pipeline(stages=[Stage(
        name="start", from_prompt="do {prompt}", output="out.md",
        cwd=str(tmp_path))])

    first, failed = pipeline.run_pipeline(
        spec, fingerprint_mode=True, original_prompt="the work")
    second, failed_again = pipeline.run_pipeline(
        spec, reuse=True, fingerprint_mode=True, original_prompt="the work")

    assert not failed and not failed_again
    assert calls.read_text(encoding="utf-8") == "x"
    assert second[0].reason == "reused"
    assert Path(second[0].task.output).is_absolute()
    assert first[0].task.prompt == "do the work"


def test_parallel_runner_retries_and_pipeline_forwards_policy(tmp_path, monkeypatch):
    task = Task(prompt="p", cwd=str(tmp_path))
    attempts = []

    def fake_gate(*args, **kwargs):
        attempts.append(1)
        return TaskResult(task, len(attempts) == 2, returncode=0 if len(attempts) == 2 else 1,
                          reason="failed" if len(attempts) == 1 else "")

    monkeypatch.setattr(runner, "_run_with_gate", fake_gate)
    results = runner.run_parallel([task], workers=1, retries=1,
                                  retry_delay=0, backoff=1)
    assert results[0].success and len(attempts) == 2

    forwarded = {}

    def fake_parallel(tasks, workers, **kwargs):
        forwarded.update(kwargs)
        return [TaskResult(tasks[0], True, stdout="ok", returncode=0)]

    monkeypatch.setattr(pipeline, "run_parallel", fake_parallel)
    stage = Stage(name="parallel", from_prompt="work", output="out.md",
                  mode="parallel", cwd=str(tmp_path))
    _, ok = pipeline.execute_stage(stage, {}, retries=3,
                                   retry_delay=4, backoff=5)
    assert ok
    assert forwarded == {"validate_cmd": "", "retries": 3,
                         "retry_delay": 4, "backoff": 5}


def test_parallel_batch_forwards_retry_policy(monkeypatch):
    forwarded = {}

    def fake_parallel(tasks, workers, **kwargs):
        forwarded.update(kwargs)
        return [TaskResult(tasks[0], True, returncode=0)]

    monkeypatch.setattr(cli, "run_parallel", fake_parallel)
    args = type("Args", (), {
        "mode": "parallel", "workers": 2, "retries": 3,
        "retry_delay": 4, "retry_backoff": 5,
    })()
    _, failed = cli._execute_batch([Task(prompt="work")], args, "", "gate")
    assert not failed
    assert forwarded == {"validate_cmd": "gate", "retries": 3,
                         "retry_delay": 4, "backoff": 5}


def test_meta_role_retries_execute_configured_number(tmp_path, monkeypatch):
    task = Task(prompt="review", output="role.md", cwd=str(tmp_path))
    original = TaskResult(task, False, returncode=1, reason="initial")
    calls = []

    def fake_run(current, **kwargs):
        calls.append(1)
        ok = len(calls) == 2
        if ok:
            current.output_path().write_text("reviewed", encoding="utf-8")
        return TaskResult(current, ok, stdout="reviewed" if ok else "",
                          returncode=0 if ok else 1, reason="" if ok else "again")

    monkeypatch.setattr(meta, "run_task", fake_run)
    monkeypatch.setattr(meta, "_save_validated", lambda *args: True)
    monkeypatch.setattr(meta, "record_task", lambda *args: None)
    role_outputs, evidence = [], []
    remaining = meta._retry_failed_roles(
        Stage(name="review"), [original], role_outputs, evidence,
        "", "session", retries=2)

    assert remaining == [] and len(calls) == 2
    assert original.success and original.reason == "retried"
    assert role_outputs == [str(task.output_path())] == evidence


def test_gate_fix_inherits_effective_validator_and_caps_prompt(
        tmp_path, monkeypatch):
    verdict = tmp_path / "gate.md"
    verdict.write_text("VERDICT: FAIL - fix it", encoding="utf-8")
    stage = Stage(name="gate", gate=True, cwd=str(tmp_path),
                  gate_fix_validate=None)
    observed = []

    def fake_run(task, **kwargs):
        return TaskResult(task, True, stdout="fixed", returncode=0)

    def fake_save(task, result, validate_cmd):
        observed.append(validate_cmd)
        task.output_path().write_text(result.stdout, encoding="utf-8")
        return True

    monkeypatch.setattr(runner, "run_task", fake_run)
    monkeypatch.setattr(runner, "_save_validated", fake_save)
    result, report = gate_fix.run_gate_fix(
        stage, [str(verdict)], 1, "effective-validator", "session")
    assert result.success and Path(report).exists()
    assert result.task.validate == "effective-validator"
    assert observed == ["effective-validator"]

    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 128)
    prompt = gate_fix.gate_fix_prompt(stage, "界" * 1000,
                                      [{"problem": "x" * 1000}])
    assert len(prompt.encode("utf-8")) <= 128


def _gate_fix_agent(tmp_path: Path) -> Path:
    agent = tmp_path / "gate-agent.sh"
    agent.write_text(
        "#!/bin/sh\n"
        'prompt="$2"\n'
        'case "$prompt" in\n'
        '  *"GATE VERDICT REASON"*) echo "fixed report" ;;\n'
        '  *"fixed report"*) echo "VERDICT: PASS - fixed" ;;\n'
        '  *gatekeeper*) echo "VERDICT: FAIL - blocked" ;;\n'
        '  *) echo "plan" ;;\n'
        'esac\n', encoding="utf-8")
    agent.chmod(0o755)
    return agent


def test_gate_fix_report_is_result_decision_evidence_and_archived(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "AGENT_BIN", str(_gate_fix_agent(tmp_path)))
    decision_log = tmp_path / "decisions.md"
    archive = tmp_path / "archive"
    spec = Pipeline(name="contract", decision_log=str(decision_log),
                    archive_dir=str(archive), stages=[
        Stage(name="plan", from_prompt="plan", output="plan.md",
              cwd=str(tmp_path)),
        Stage(name="gate", from_outputs="plan", aggregate=True, gate=True,
              gate_fix_rounds=1, cwd=str(tmp_path), tasks=[{
                  "prompt": "gatekeeper {input_content}", "output": "gate.md"}]),
    ])
    results, failed = pipeline.run_pipeline(spec)
    pipeline.print_summary(results)
    summary = capsys.readouterr().out

    assert failed == [] and len(results) == 4
    assert "total:     4" in summary
    fix_results = [item for item in results
                   if Path(item.task.output).name == "gate-fix-1.md"]
    assert len(fix_results) == 1 and fix_results[0].success
    assert Path(fix_results[0].task.output).parent.parent == archive
    assert "gate-fix-1.md" in decision_log.read_text(encoding="utf-8")


def test_fully_reused_stage_is_logged_summarized_and_archived(tmp_path):
    output = tmp_path / "existing.md"
    output.write_text("# Existing\nReusable evidence.", encoding="utf-8")
    decision_log = tmp_path / "decisions.md"
    archive = tmp_path / "archive"
    spec = Pipeline(name="reuse", decision_log=str(decision_log),
                    archive_dir=str(archive), stages=[Stage(
                        name="start", from_prompt="work", output="existing.md",
                        cwd=str(tmp_path))])

    results, failed = pipeline.run_pipeline(spec, reuse=True)

    assert failed == [] and len(results) == 1
    assert results[0].success and results[0].reason == "reused"
    assert Path(results[0].task.output).exists()
    assert Path(results[0].task.output).parent.parent == archive
    logged = decision_log.read_text(encoding="utf-8")
    assert "stage 'start' — PASS" in logged and "existing.md" in logged


def test_parallel_duplicate_output_fails_before_spawn(tmp_path, monkeypatch):
    source = tmp_path / "source.md"
    source.write_text("input", encoding="utf-8")
    stage = Stage(
        name="parallel", from_outputs="up", aggregate=True, mode="parallel",
        cwd=str(tmp_path), tasks=[
            {"prompt": "one {input_content}", "output": "same.md"},
            {"prompt": "two {input_content}", "output": "same.md"},
        ])
    monkeypatch.setattr(
        pipeline, "run_parallel",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("spawned")))

    results, ok = pipeline.execute_stage(stage, {"up": [str(source)]})

    assert not ok and results == []
    assert not (tmp_path / "same.md").exists()


def test_failed_task_preserves_source_changes_and_worktree_evidence(
        tmp_path, monkeypatch):
    source = _init_repo(tmp_path)
    task = Task(prompt="edit", output="result.md", cwd=str(tmp_path))

    def failing_agent(current, **kwargs):
        source.write_text("value = broken\n", encoding="utf-8")
        return TaskResult(current, False, returncode=1, reason="agent failed")

    monkeypatch.setattr(runner, "run_task", failing_agent)
    monkeypatch.setattr(runner, "record_task", lambda *args: None)
    result = runner._run_with_gate(task, "", 1, 1)

    assert not result.success
    assert source.read_text(encoding="utf-8") == "value = broken\n"
    assert "worktree evidence:" in result.reason
    evidence = Path(result.reason.split("worktree evidence: ", 1)[1])
    assert evidence.exists()
    assert "BEFORE" in evidence.read_text(encoding="utf-8")
    assert "source.py" in evidence.read_text(encoding="utf-8")


def test_stage_commit_isolates_outputs_from_user_staged_changes(tmp_path):
    source = _init_repo(tmp_path)
    source.write_text("user change\n", encoding="utf-8")
    _git(tmp_path, "add", "source.py")
    report = tmp_path / "report.md"
    report.write_text("pipeline output\n", encoding="utf-8")
    stage = Stage(name="report", git_commit=True, cwd=str(tmp_path))

    assert pipeline._git_commit_stage(stage, [str(report)])
    committed = subprocess.run(
        ["git", "show", "--pretty=", "--name-only", "HEAD"], cwd=tmp_path,
        check=True, capture_output=True, text=True).stdout.splitlines()
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=tmp_path,
        check=True, capture_output=True, text=True).stdout.splitlines()
    assert committed == ["report.md"]
    assert staged == ["source.py"]
    assert source.read_text(encoding="utf-8") == "user change\n"


def test_failed_stage_commit_restores_only_its_intent_to_add(tmp_path):
    source = _init_repo(tmp_path)
    source.write_text("user staged\n", encoding="utf-8")
    _git(tmp_path, "add", "source.py")
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    report = tmp_path / "report.md"
    report.write_text("not committed\n", encoding="utf-8")

    assert not pipeline._git_commit_stage(
        Stage(name="report", git_commit=True, cwd=str(tmp_path)), [str(report)])
    status = subprocess.run(
        ["git", "status", "--short"], cwd=tmp_path, check=True,
        capture_output=True, text=True).stdout.splitlines()
    assert "M  source.py" in status
    assert "?? report.md" in status


def _context_project(root: Path, loaded: str = "docs/contract.md") -> None:
    context_dir = root / "docs" / "agent-context"
    context_dir.mkdir(parents=True)
    context_dir.joinpath("context-map.yaml").write_text(
        "routes:\n"
        "  - when:\n"
        "      task_contains: [payment]\n"
        f"    load: [{loaded}]\n", encoding="utf-8")


def test_run_task_injects_project_context_before_agent(tmp_path, monkeypatch):
    _context_project(tmp_path)
    (tmp_path / "docs" / "contract.md").write_text(
        "payment contract evidence", encoding="utf-8")
    agent = tmp_path / "context-agent.sh"
    agent.write_text("#!/bin/sh\nprintf '%s\\n' \"$2\"\n", encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setattr(config, "AGENT_BIN", str(agent))

    result = runner.run_task(Task(prompt="implement payment", cwd=str(tmp_path)))

    assert result.success
    assert '<project-context trust="untrusted-data">' in result.stdout
    assert "payment contract evidence" in result.stdout


def test_run_task_rejects_unsafe_context_before_invocation(tmp_path, monkeypatch):
    _context_project(tmp_path, "../outside.md")
    calls = []
    original = runner._task_invocation

    def spy(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(runner, "_task_invocation", spy)
    result = runner.run_task(Task(prompt="payment", cwd=str(tmp_path)))

    assert not result.success
    assert result.reason.startswith("context injection rejected:")
    assert calls == []


def _add_tracked(repo: Path, name: str, content: str) -> Path:
    path = repo / name
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", f"add {name}")
    return path


def _successful_stage_runner(source: Path, user_file: Path | None = None):
    def run(tasks, **kwargs):
        source.write_text("value = 2\n", encoding="utf-8")
        if user_file:
            assert user_file.exists()
        source.parent.joinpath(".pi-batch", "runtime").mkdir(
            parents=True, exist_ok=True)
        source.parent.joinpath(".pi-batch", "runtime", "state").write_text(
            "operational", encoding="utf-8")
        for task in tasks:
            task.output_path().write_text("report\n", encoding="utf-8")
        return [TaskResult(task, True, stdout="report\n", returncode=0)
                for task in tasks]
    return run


def test_pipeline_stage_commits_new_code_but_preserves_preexisting_index(
        tmp_path, monkeypatch):
    source = _init_repo(tmp_path)
    user_file = _add_tracked(tmp_path, "user.txt", "clean\n")
    user_file.write_text("user staged\n", encoding="utf-8")
    _git(tmp_path, "add", "user.txt")
    monkeypatch.setattr(
        pipeline, "run_serial", _successful_stage_runner(source, user_file))
    spec = Pipeline(stages=[Stage(
        name="build", from_prompt="build", output="report.md",
        cwd=str(tmp_path), git_commit=True)])

    _, failed = pipeline.run_pipeline(spec)

    committed = subprocess.run(
        ["git", "show", "--pretty=", "--name-only", "HEAD"], cwd=tmp_path,
        check=True, capture_output=True, text=True).stdout.splitlines()
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=tmp_path,
        check=True, capture_output=True, text=True).stdout.splitlines()
    assert failed == []
    assert sorted(committed) == ["report.md", "source.py"]
    assert staged == ["user.txt"]
    assert ".pi-batch/runtime/state" not in committed


def test_pipeline_commit_hook_failure_fails_stage_and_suppresses_archive(
        tmp_path, monkeypatch):
    source = _init_repo(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    monkeypatch.setattr(
        pipeline, "run_serial", _successful_stage_runner(source))
    archive = tmp_path / "archive"
    spec = Pipeline(archive_dir=str(archive), stages=[Stage(
        name="build", from_prompt="build", output="report.md",
        cwd=str(tmp_path), git_commit=True)])

    _, failed = pipeline.run_pipeline(spec)

    assert failed == ["build"]
    assert not archive.exists()
    assert (tmp_path / "report.md").exists()
    assert subprocess.run(
        ["git", "log", "-1", "--pretty=%s"], cwd=tmp_path, check=True,
        capture_output=True, text=True).stdout.strip() == "baseline"


def test_gate_fix_code_changes_are_committed_only_after_final_pass(
        tmp_path, monkeypatch):
    source = _init_repo(tmp_path)
    upstream = _add_tracked(tmp_path, "plan.md", "plan\n")
    attempts = []

    def gate_runner(tasks, **kwargs):
        attempts.append(1)
        verdict = ("VERDICT: FAIL - repair source\n" if len(attempts) == 1
                   else "VERDICT: PASS - repaired\n")
        task = tasks[0]
        task.output_path().write_text(verdict, encoding="utf-8")
        return [TaskResult(task, True, stdout=verdict, returncode=0)]

    def fix_once(stage, outputs, fix_round, validate_cmd, session_name):
        source.write_text("value = fixed\n", encoding="utf-8")
        report = tmp_path / "gate-fix-1.md"
        report.write_text("fixed report\n", encoding="utf-8")
        task = Task("fix", output=str(report), cwd=str(tmp_path))
        return TaskResult(task, True, stdout="fixed report", returncode=0), str(report)

    monkeypatch.setattr(pipeline, "run_serial", gate_runner)
    monkeypatch.setattr(gate_fix, "run_gate_fix", fix_once)
    stage = Stage(
        name="gate", from_outputs="plan", aggregate=True, gate=True,
        gate_fix_rounds=1, cwd=str(tmp_path), git_commit=True,
        tasks=[{"prompt": "gate {input_content}", "output": "gate.md"}])
    failed = []

    stop, results = pipeline._run_pipeline_stage(
        stage, {"plan": [str(upstream)]}, failed, "", False, 0,
        "new", "", "", False, False, "", "", "")

    committed = subprocess.run(
        ["git", "show", "--pretty=", "--name-only", "HEAD"], cwd=tmp_path,
        check=True, capture_output=True, text=True).stdout.splitlines()
    assert not stop and failed == [] and len(results) == 3
    assert sorted(committed) == ["gate-fix-1.md", "gate.md", "source.py"]


def test_failed_task_or_approval_skips_post_stage_gates(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pipeline, "_run_stage_commands", lambda stage: calls.append("command"))
    monkeypatch.setattr(
        pipeline, "run_stage_repo_validators",
        lambda *args, **kwargs: calls.append("repo"))
    failed_task = TaskResult(Task("work"), False, reason="failed")
    _, ok = pipeline._finalize_stage(
        Stage(name="failed", commands=["post"]), [failed_task], [], False, "")
    assert not ok and calls == []

    monkeypatch.setattr(pipeline, "_check_approval", lambda *args: False)
    successful = TaskResult(Task("work"), True, returncode=0)
    _, ok = pipeline._finalize_stage(
        Stage(name="approval", approval=True, commands=["post"]),
        [successful], [], True, "")
    assert not ok and calls == []


def test_initial_gate_task_failure_never_evaluates_or_fixes(monkeypatch):
    monkeypatch.setattr(pipeline, "execute_stage", lambda *args, **kwargs: ([], False))
    monkeypatch.setattr(
        pipeline, "_handle_gate",
        lambda *args: (_ for _ in ()).throw(AssertionError("gate evaluated")))
    monkeypatch.setattr(
        pipeline, "_run_gate_fix_loop",
        lambda *args: (_ for _ in ()).throw(AssertionError("fix started")))
    failed = []
    stage = Stage(name="gate", gate=True, gate_fix_rounds=2)

    stop, results = pipeline._run_pipeline_stage(
        stage, {}, failed, "", False, 0, "new", "", "", False,
        False, "", "", "")

    assert stop and results == [] and failed == ["gate"]


def test_failed_batch_skips_deferred_repo_validators(monkeypatch):
    task = Task("work")
    failure = TaskResult(task, False, reason="agent failed")
    monkeypatch.setattr(cli, "_load_batch_tasks", lambda *args: [task])
    monkeypatch.setattr(cli, "_apply_task_overrides", lambda *args: None)
    monkeypatch.setattr(cli, "_execute_batch", lambda *args: ([failure], True))
    monkeypatch.setattr(
        cli, "_run_batch_repo_validators",
        lambda *args: (_ for _ in ()).throw(AssertionError("validator ran")))
    args = SimpleNamespace(
        git_commit=False, no_git_commit=False, kill_orphans=False,
        reuse=False, force=False, reuse_legacy=False, reuse_fingerprint=False,
        dry_run=False, mode="serial", decision_log="", archive_dir="")

    failed, _ = cli._run_batch_round(args, None, "", "", 1, 1)

    assert failed


def test_single_batch_commit_includes_new_code_and_hook_failure_blocks_archive(
        tmp_path, monkeypatch):
    source = _init_repo(tmp_path)
    baseline = capture = pipeline.capture_stage_dirty_paths(str(tmp_path))
    source.write_text("value = batch\n", encoding="utf-8")
    output = tmp_path / "batch.md"
    output.write_text("batch report\n", encoding="utf-8")
    result = TaskResult(Task("work", output=str(output), cwd=str(tmp_path)),
                        True, returncode=0)
    args = SimpleNamespace(
        git_commit=True, no_git_commit=False, commit_prefix="[test]",
        decision_log="", archive_dir="", source="", from_dir="")
    assert not cli._finalize_batch_round(args, [result], False, baseline)
    committed = subprocess.run(
        ["git", "show", "--pretty=", "--name-only", "HEAD"], cwd=tmp_path,
        check=True, capture_output=True, text=True).stdout.splitlines()
    assert sorted(committed) == ["batch.md", "source.py"]

    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    baseline = pipeline.capture_stage_dirty_paths(str(tmp_path))
    source.write_text("value = blocked\n", encoding="utf-8")
    output.write_text("blocked\n", encoding="utf-8")
    archived = []
    args.archive_dir = str(tmp_path / "archive")
    monkeypatch.setattr(cli, "_archive_outputs", lambda *args: archived.append(True))
    assert cli._finalize_batch_round(args, [result], False, baseline)
    assert archived == []


def _path_context_project(root: Path) -> None:
    root.joinpath(".git").mkdir(exist_ok=True)
    context_dir = root / "docs" / "agent-context"
    context_dir.mkdir(parents=True)
    context_dir.joinpath("context-map.yaml").write_text(
        "routes:\n"
        "  - when:\n"
        "      paths: [inputs/**, artifacts/**]\n"
        "    load: [docs/path-contract.md]\n", encoding="utf-8")
    root.joinpath("docs", "path-contract.md").write_text(
        "path routed context", encoding="utf-8")


def test_from_dir_and_from_outputs_route_context_by_provenance(
        tmp_path, monkeypatch):
    _path_context_project(tmp_path)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    inputs.joinpath("one.md").write_text("plain input", encoding="utf-8")
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    upstream = artifact_dir / "plan.md"
    upstream.write_text("plain plan", encoding="utf-8")
    observed = []

    def fake_run(tasks, **kwargs):
        results = []
        for task in tasks:
            observed.append((task.prompt, list(task.memory.get("paths", []))))
            task.output_path().write_text("done", encoding="utf-8")
            results.append(TaskResult(task, True, stdout="done", returncode=0))
        return results

    monkeypatch.setattr(pipeline, "run_serial", fake_run)
    _, from_dir_ok = pipeline.execute_stage(
        Stage(name="dir", from_dir=str(inputs)), {})
    _, outputs_ok = pipeline.execute_stage(Stage(
        name="out", from_outputs="up", cwd=str(tmp_path),
        tasks=[{"prompt": "review {input_content}", "output": "review.md"}]),
        {"up": [str(upstream)]})

    assert from_dir_ok and outputs_ok
    assert all("path routed context" in prompt for prompt, _ in observed)
    assert observed[0][1] == ["inputs/one.md"]
    assert observed[1][1] == ["artifacts/plan.md"]


def test_context_document_change_invalidates_untracked_reuse_fingerprint(
        tmp_path, monkeypatch):
    _init_repo(tmp_path)
    _context_project(tmp_path)
    contract = tmp_path / "docs" / "contract.md"
    contract.write_text("contract v1", encoding="utf-8")
    calls = tmp_path / "calls"
    agent = tmp_path / "context-agent.sh"
    agent.write_text(
        f"#!/bin/sh\nprintf x >> {calls}\nprintf '%s\\n' \"$2\"\n",
        encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setattr(config, "AGENT_BIN", str(agent))
    spec = Pipeline(stages=[Stage(
        name="start", from_prompt="implement payment", output="out.md",
        cwd=str(tmp_path))])

    pipeline.run_pipeline(spec, fingerprint_mode=True)
    pipeline.run_pipeline(spec, reuse=True, fingerprint_mode=True)
    contract.write_text("contract v2", encoding="utf-8")
    pipeline.run_pipeline(spec, reuse=True, fingerprint_mode=True)

    assert calls.read_text(encoding="utf-8") == "xx"
    assert "contract v2" in tmp_path.joinpath("out.md").read_text(encoding="utf-8")


def test_cli_sidecar_uses_task_workdir_and_invalid_env_is_preflight_rejected(
        tmp_path, monkeypatch):
    output = tmp_path / "out.md"
    output.write_text("done", encoding="utf-8")
    task = Task("prompt", output="out.md", cwd=str(tmp_path))
    result = TaskResult(task, True, returncode=0)
    cli._write_sidecars(SimpleNamespace(), [task], [result], "")
    expected = reuse.reuse_fingerprint(task, "")
    assert reuse.sidecar_matches(output, expected)

    calls = []
    monkeypatch.setattr(
        runner, "_task_invocation",
        lambda *args: calls.append(True))
    rejected = runner.run_task(Task("work", cwd=str(tmp_path), env={1: "bad"}))
    assert not rejected.success
    assert rejected.reason == (
        "context injection rejected: task env must map strings to strings")
    assert calls == []


def test_pipeline_git_commit_and_fingerprint_reuse_are_stable(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    calls = tmp_path / "calls"
    agent = tmp_path / "agent.sh"
    agent.write_text(
        f"#!/bin/sh\nprintf x >> {calls}\nprintf 'report\\n'\n",
        encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setattr(config, "AGENT_BIN", str(agent))
    spec = Pipeline(stages=[Stage(
        name="report", from_prompt="report", output="report.md",
        cwd=str(repo), git_commit=True)])

    pipeline.run_pipeline(spec, fingerprint_mode=True)
    os.utime(repo / "report.md", (1, 1))
    second, failed = pipeline.run_pipeline(
        spec, reuse=True, fingerprint_mode=True)

    assert failed == [] and second[0].reason == "reused"
    assert calls.read_text(encoding="utf-8") == "x"
    committed = subprocess.run(
        ["git", "show", "--pretty=", "--name-only", "HEAD"], cwd=repo,
        check=True, capture_output=True, text=True).stdout.splitlines()
    assert sorted(committed) == ["report.md", "report.md.meta.json"]
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True,
        capture_output=True, text=True).stdout == ""


def _batch_args() -> SimpleNamespace:
    return SimpleNamespace(
        prompt="report", output="report.md", from_dir="", source="",
        suffix=".md", model="", provider="", timeout=0,
        git_commit=True, no_git_commit=False, commit_prefix="[test]",
        kill_orphans=False, reuse=False, force=False, reuse_legacy=False,
        reuse_fingerprint=True, dry_run=False, mode="serial", workers=1,
        retries=0, retry_delay=0, retry_backoff=1, min_interval=0,
        session_mode="new", decision_log="", archive_dir="")


def test_single_batch_git_commit_commits_sidecar_and_second_run_reuses(
        tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    calls = tmp_path / "batch-calls"
    agent = tmp_path / "batch-agent.sh"
    agent.write_text(
        f"#!/bin/sh\nprintf x >> {calls}\nprintf 'report\\n'\n",
        encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(config, "AGENT_BIN", str(agent))
    args = _batch_args()

    first_failed, _ = cli._run_batch_round(args, None, "batch", "", 1, 1)
    args.reuse = True
    second_failed, _ = cli._run_batch_round(args, None, "batch", "", 1, 2)

    assert not first_failed and not second_failed
    assert calls.read_text(encoding="utf-8") == "x"
    committed = subprocess.run(
        ["git", "show", "--pretty=", "--name-only", "HEAD"], cwd=repo,
        check=True, capture_output=True, text=True).stdout.splitlines()
    assert sorted(committed) == ["report.md", "report.md.meta.json"]
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, check=True,
        capture_output=True, text=True).stdout == ""


def test_pipeline_unsafe_context_fails_cleanly_before_reuse_or_agent(
        tmp_path, monkeypatch):
    _context_project(tmp_path, "../outside.md")
    tmp_path.joinpath("out.md").write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        pipeline, "run_serial",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("agent ran")))
    spec = Pipeline(stages=[Stage(
        name="unsafe", from_prompt="payment", output="out.md",
        cwd=str(tmp_path))])

    results, failed = pipeline.run_pipeline(spec, reuse=True,
                                            fingerprint_mode=True)

    assert len(results) == 1 and not results[0].success
    assert results[0].reason.startswith("stage input rejected:")
    assert failed == ["unsafe"]


def test_from_dir_aggregate_fingerprint_receives_task_workdir(
        tmp_path, monkeypatch):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    inputs.joinpath("one.md").write_text("one", encoding="utf-8")
    seen = []
    original = from_dir._expected_fp

    def capture(*args, **kwargs):
        seen.append(args[-1])
        return original(*args, **kwargs)

    monkeypatch.setattr(from_dir, "_expected_fp", capture)
    tasks, reused = from_dir._stage_from_dir_aggregate(
        Stage(name="aggregate", from_dir=str(inputs), aggregate=True),
        [inputs / "one.md"], False, "", 0, "", False, True)

    assert len(tasks) == 1 and reused == []
    assert seen == [str(inputs.resolve())]


def test_from_prompt_file_reference_resolves_against_stage_cwd(
        tmp_path, monkeypatch):
    tmp_path.joinpath("requirement.md").write_text(
        "cwd requirement", encoding="utf-8")
    observed = []

    def fake_run(tasks, **kwargs):
        observed.append(tasks[0].prompt)
        tasks[0].output_path().write_text("done", encoding="utf-8")
        return [TaskResult(tasks[0], True, stdout="done", returncode=0)]

    monkeypatch.setattr(pipeline, "run_serial", fake_run)
    _, ok = pipeline.execute_stage(Stage(
        name="start", from_prompt="@requirement.md", output="out.md",
        cwd=str(tmp_path)), {})

    assert ok and observed == ["cwd requirement"]
