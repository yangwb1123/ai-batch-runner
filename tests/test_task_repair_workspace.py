"""Explicit Git diagnostics preserve workspace bytes and grant no recovery authority."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_task_repair_preview import _write
from test_task_repair_requests import source, prepared, register
from pbatch import task_repair_workspace as workspace, task_workspace_git as git_probe
from pbatch import task_workspace_layout as layout_probe, task_repair_store, task_goal_store
from pbatch import task_workspace_sandbox as controls_probe
from pbatch.ops import ops_main
from pbatch.process_exec import ValidationResult, run_bounded
from pbatch.task_repair_preview import REQUIRED_CHECKS

GIT = shutil.which("git", path=os.defpath)
pytestmark = pytest.mark.skipif(GIT is None, reason="Git required for hermetic workspace diagnostics")


def _git(path, *args):
    result = subprocess.run([GIT, "-c", "user.name=Fixture", "-c", "user.email=fixture@invalid",
        "-C", str(path), *args], env=git_probe.environment(), capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _initialize(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    (path / "file.txt").write_text("owned synthetic content\n")
    _git(path, "add", "file.txt")
    _git(path, "commit", "-m", "fixture")
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def owned(prepared):
    value = register(prepared)
    root = Path(prepared[0]["workspace"])
    head = _initialize(root)
    return prepared, value, root, head


def _report(owned, **kwargs):
    _, value, _, head = owned
    return workspace.inspect_workspace(value["repair_id"], head, confirm=True, **kwargs)


def _no_authority(report):
    assert report["decision"] == "blocked" and report["ready_to_execute"] is False
    assert report["grants_execution"] is False and report["live_preflight_performed"] is False
    assert report["writes_workspace"] is False and report["stores_receipt"] is False
    assert report["original_git_baseline"] is None
    assert set(REQUIRED_CHECKS).issubset(report["blockers"])
    assert report["workspace_binding"] == "registered_source_workspace_not_verified_original_run_workspace"
    json.dumps(report, allow_nan=False)


def _bytes(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file() and not path.is_symlink()}


def test_clean_two_sample_git_observation_does_not_grant_ownership_or_original_baseline(owned):
    before = _bytes(owned[2])
    request = task_repair_store.request_path(owned[1]["repair_id"]).read_bytes()
    report = _report(owned)
    assert report["inspection_state"] == "observed", report["diagnostic"]
    assert report["git_sampling_completed"] is True
    observation = report["git_observation"]
    assert observation["head_matches_expectation"] is True
    assert observation["status"]["reported_entries"] == 0
    assert observation["branch_display"] == "refs/heads/main"
    assert before == _bytes(owned[2])
    assert task_repair_store.request_path(owned[1]["repair_id"]).read_bytes() == request
    _no_authority(report)


def test_sha256_object_ids_are_observed_without_shortening_or_inventing_original_baseline(prepared):
    value = register(prepared)
    root = Path(prepared[0]["workspace"])
    _git(root, "init", "-b", "main", "--object-format=sha256")
    (root / "file.txt").write_text("synthetic sha256 fixture\n")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-m", "sha256 fixture")
    head = _git(root, "rev-parse", "HEAD")
    assert len(head) == 64
    report = workspace.inspect_workspace(value["repair_id"], head, confirm=True)
    assert report["inspection_state"] == "observed", report["diagnostic"]
    assert report["git_observation"]["head"] == head
    _no_authority(report)


def test_dirty_untracked_and_ignored_changes_are_reported_and_never_cleaned(owned):
    root = owned[2]
    (root / "file.txt").write_text("preserve failed edits\n")
    (root / "untracked.txt").write_text("preserve unsaved result\n")
    (root / ".gitignore").write_text("ignored.bin\n")
    (root / "ignored.bin").write_text("private retained evidence\n")
    before = _bytes(root)
    report = _report(owned)
    assert report["inspection_state"] == "observed", report["diagnostic"]
    counts = report["git_observation"]["status"]["counts"]
    assert counts == {"tracked": 1, "untracked": 2, "ignored": 1, "conflicted": 0}
    assert "reported_dirty_workspace_preserve_changes" in report["blockers"]
    assert "ignored_entries_present_not_evidence_of_safe_cleanup" in report["blockers"]
    assert _bytes(root) == before
    _no_authority(report)


def test_repository_config_race_cannot_execute_a_filter_from_live_config(owned, monkeypatch, tmp_path):
    root = owned[2]
    marker = tmp_path / "filter-must-not-execute"
    script = root / ".git/hooks/filter-fixture"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat\n")
    script.chmod(0o700)
    (root / ".gitattributes").write_text("*.txt filter=unsafe\n")
    (root / "file.txt").write_text("changed content for status sampling\n")
    original = git_probe.command
    calls = []
    def racing(layout, *args, **kwargs):
        assert layout["git_dir"] != root / ".git"
        if not calls:
            with (root / ".git/config").open("a") as stream:
                stream.write(f'\n[filter "unsafe"]\nclean = {script}\n')
            calls.append(True)
        return original(layout, *args, **kwargs)
    monkeypatch.setattr(git_probe, "command", racing)
    report = _report(owned)
    assert report["inspection_state"] == "unavailable" and report["git_observation"] is None
    assert not marker.exists()
    _no_authority(report)


def test_private_metadata_copies_are_outside_workspace_and_removed_only_in_own_scope(owned, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(owned[2]))
    private_paths = []
    original = workspace._samples
    def sampling(layout, *args):
        private_paths.append(layout["git_dir"])
        assert owned[2] not in layout["git_dir"].parents
        assert (layout["git_dir"] / "index").stat().st_mode & 0o777 == 0o600
        assert (layout["git_dir"] / "config").stat().st_mode & 0o777 == 0o600
        return original(layout, *args)
    monkeypatch.setattr(workspace, "_samples", sampling)
    report = _report(owned)
    assert report["inspection_state"] == "observed", report["diagnostic"]
    assert report["private_temporary_controls_created"] is True
    assert private_paths and not private_paths[0].parent.exists()


def test_index_copy_budget_exhaustion_refuses_instead_of_truncating(owned, monkeypatch):
    monkeypatch.setattr(controls_probe, "MAX_INDEX_BYTES", 1)
    report = _report(owned)
    assert report["inspection_state"] == "unavailable" and not report["git_sampling_attempted"]
    assert "index copy budget" in report["diagnostic"]
    _no_authority(report)


def test_head_mismatch_and_detached_state_are_not_rebased_or_checked_out(owned):
    value, root = owned[1], owned[2]
    _git(root, "checkout", "--detach")
    before = _bytes(root)
    report = workspace.inspect_workspace(value["repair_id"], "0" * 40, confirm=True)
    assert report["inspection_state"] == "observed", report["diagnostic"]
    assert "operator_head_expectation_conflict" in report["blockers"]
    assert "detached_head_requires_original_branch_scope_evidence" in report["blockers"]
    assert _bytes(root) == before
    _no_authority(report)


@pytest.mark.parametrize("marker", ["index.lock", "MERGE_HEAD", "rebase-merge", "CHERRY_PICK_HEAD", "locked"])
def test_operation_markers_refuse_git_without_breaking_lock_or_preserved_state(owned, monkeypatch, marker):
    path = owned[2] / ".git" / marker
    path.write_text("owned marker\n")
    before = _bytes(owned[2])
    def forbidden(*args, **kwargs):
        pytest.fail("operation markers must refuse before spawning Git")
    monkeypatch.setattr(git_probe, "run_bounded", forbidden)
    report = _report(owned)
    assert report["inspection_state"] == "unavailable" and not report["git_sampling_attempted"]
    assert marker in report["operation_markers"]
    assert _bytes(owned[2]) == before
    _no_authority(report)


@pytest.mark.parametrize("section", ['[include]\npath = /private/unselected/config',
    '[includeIf "gitdir:*"]\npath = /private/unselected/config',
    '[filter "unsafe"]\nclean = sh -c "touch /unselected"', '[core]\nworktree = /unselected',
    '[core]\nbare = true', '[core]\nbare', '[remote "origin"]\npromisor = true'])
def test_unsafe_repository_config_never_reads_include_or_executes_filters(owned, monkeypatch, section):
    config = owned[2] / ".git" / "config"
    with config.open("a") as stream:
        stream.write("\n" + section + "\n")
    def forbidden(*args, **kwargs):
        pytest.fail("unsafe local config must refuse before Git")
    monkeypatch.setattr(git_probe, "run_bounded", forbidden)
    before = _bytes(owned[2])
    report = _report(owned)
    assert report["inspection_state"] == "unavailable" and not report["git_sampling_attempted"]
    assert _bytes(owned[2]) == before
    _no_authority(report)


def test_host_environment_and_repo_fsmonitor_cannot_inject_commands_or_repository(owned, monkeypatch, tmp_path):
    marker = tmp_path / "must-not-execute"
    hook = owned[2] / ".git" / "hooks" / "fsmonitor-fixture"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o700)
    _git(owned[2], "config", "core.fsmonitor", str(hook))
    monkeypatch.setenv("GIT_DIR", "/unselected")
    monkeypatch.setenv("GIT_WORK_TREE", "/unselected")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hook))
    report = _report(owned)
    assert report["inspection_state"] == "observed", report["diagnostic"]
    assert not marker.exists()
    _no_authority(report)


def test_linked_worktree_requires_explicit_metadata_scope_and_backlinks(prepared, tmp_path):
    value = register(prepared)
    root = Path(prepared[0]["workspace"])
    parent = tmp_path / "parent"
    head = _initialize(parent)
    _git(parent, "worktree", "add", "-b", "failed-task", str(root))
    report = workspace.inspect_workspace(value["repair_id"], head, confirm=True)
    assert report["inspection_state"] == "unavailable" and not report["git_sampling_attempted"]
    assert "explicit --common-dir" in report["diagnostic"]
    before = _bytes(parent)
    report = workspace.inspect_workspace(value["repair_id"], head, confirm=True, common_dir=str(parent / ".git"))
    assert report["inspection_state"] == "observed", report["diagnostic"]
    assert report["layout"] == "linked_worktree"
    assert _bytes(parent) == before
    _no_authority(report)


@pytest.mark.parametrize("damage", ["symlink", "entry_budget", "alternate", "ref_lock", "inaccessible", "config_fifo"])
def test_unknown_or_unsafe_metadata_cannot_be_called_clean(owned, monkeypatch, tmp_path, damage):
    root = owned[2] / ".git"
    if damage == "symlink":
        (root / "unknown-link").symlink_to(tmp_path, target_is_directory=True)
    elif damage == "entry_budget":
        monkeypatch.setattr(layout_probe, "MAX_ENTRIES", 1)
    elif damage == "alternate":
        (root / "objects/info/alternates").write_text(str(tmp_path) + "\n")
    elif damage == "ref_lock":
        (root / "refs/heads/main.lock").write_text("busy")
    elif damage == "config_fifo":
        (root / "config").unlink()
        os.mkfifo(root / "config")
    else:
        original = layout_probe.os.walk
        def denied(*args, **kwargs):
            kwargs["onerror"](PermissionError("synthetic denied metadata subtree"))
            return original(*args, **kwargs)
        monkeypatch.setattr(layout_probe.os, "walk", denied)
    report = _report(owned)
    assert report["inspection_state"] == "unavailable" and report["git_observation"] is None
    assert not report["git_sampling_attempted"]
    _no_authority(report)


def test_submodule_status_is_explicitly_unverified_and_never_recursed(owned):
    _git(owned[2], "update-index", "--add", "--cacheinfo", "160000," + owned[3] + ",module")
    _git(owned[2], "commit", "-m", "retain gitlink")
    head = _git(owned[2], "rev-parse", "HEAD")
    report = workspace.inspect_workspace(owned[1]["repair_id"], head, confirm=True)
    assert report["inspection_state"] == "observed", report["diagnostic"]
    assert report["git_observation"]["tracked_gitlinks"] == 1
    assert "submodule_workspaces_not_inspected" in report["blockers"]
    _no_authority(report)


def test_large_path_display_keeps_counts_and_omissions_without_secret_names(owned):
    for number in range(15):
        (owned[2] / f"token=secret-{number}.txt").write_text("retained")
    report = _report(owned)
    status = report["git_observation"]["status"]
    assert status["reported_entries"] == status["counts"]["untracked"] == 15
    assert len(status["path_refs"]) == 8 and status["omitted_path_refs"] == 7
    assert "secret-" not in json.dumps(report)


@pytest.mark.parametrize("mode", ["head", "status", "metadata", "request"])
def test_racing_observations_are_withheld_not_destructively_repaired(owned, monkeypatch, mode):
    if mode in {"head", "status"}:
        original = git_probe.identity if mode == "head" else git_probe.status
        calls = []
        def changing(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 2:
                if mode == "head":
                    result["head"] = "0" * 40
                else:
                    result = (result[0] + "?? synthetic\0", result[1])
            return result
        monkeypatch.setattr(git_probe, "identity" if mode == "head" else "status", changing)
    else:
        original = workspace._samples
        def changing(*args):
            result = original(*args)
            if mode == "metadata":
                (owned[2] / ".git/new-file").write_text("changed during sample")
            else:
                task_repair_store.withdraw(owned[1]["repair_id"], "operator", "withdraw in inspection", 1)
            return result
        monkeypatch.setattr(workspace, "_samples", changing)
    report = _report(owned)
    assert report["inspection_state"] == "unavailable" and report["git_observation"] is None
    _no_authority(report)


@pytest.mark.parametrize("result", [ValidationResult(False, -1, "", "output exceeds cap", False),
    ValidationResult(False, -1, "", "", True), ValidationResult(False, 1, "", "git failure", False),
    ValidationResult(True, 0, "partial", "", False), ValidationResult(True, 0, "\ufffd", "", False)])
def test_command_failure_timeout_overflow_or_corrupt_output_stays_unknown(owned, monkeypatch, result):
    monkeypatch.setattr(git_probe, "run_bounded", lambda *args, **kwargs: result)
    report = _report(owned)
    assert report["inspection_state"] == "unavailable" and report["git_observation"] is None
    _no_authority(report)


def test_deadline_is_shared_and_injected_git_environment_is_not_inherited(owned, monkeypatch):
    captured = []
    def executing(cmd, cwd, timeout, cap, shell, label, **kwargs):
        captured.append((cmd, timeout, cap, shell, kwargs["env"]))
        return ValidationResult(True, 0, owned[3] + "\n", "", False)
    monkeypatch.setattr(git_probe, "run_bounded", executing)
    monkeypatch.setattr(git_probe.time, "monotonic", lambda: 10)
    layout = layout_probe.admit_layout(str(owned[2]))
    git_probe.command(layout, ["rev-parse", "HEAD"], 13)
    assert captured[0][1:4] == (3, git_probe.OUTPUT_CAP, False)
    assert captured[0][4]["GIT_OPTIONAL_LOCKS"] == "0"
    assert "GIT_CONFIG_COUNT" not in captured[0][4]
    with pytest.raises(ValueError, match="deadline"):
        git_probe.command(layout, ["rev-parse", "HEAD"], 9)
    assert len(captured) == 1


def test_confirm_and_full_oid_admission_happen_before_request_or_workspace_reads(monkeypatch):
    def forbidden(*args):
        pytest.fail("no request/file reads before explicit consent and valid complete OID")
    monkeypatch.setattr(workspace, "load_request", forbidden)
    with pytest.raises(ValueError, match="--confirm"):
        workspace.inspect_workspace("rq-" + "0" * 32, "0" * 40)
    for oid in ("HEAD", "--help", "0" * 7, "g" * 40, True, "0" * 40 + "\n"):
        with pytest.raises(ValueError, match="--expected-head"):
            workspace.inspect_workspace("rq-" + "0" * 32, oid, confirm=True)
    with pytest.raises(ValueError, match="absolute Git metadata"):
        workspace.inspect_workspace("rq-" + "0" * 32, "0" * 40, confirm=True, common_dir="relative/.git")


@pytest.mark.parametrize("mode", ["withdrawn", "revised", "active_child"])
def test_stale_or_busy_request_never_enters_workspace_inspection(owned, monkeypatch, mode):
    prepared, value, _, _ = owned
    if mode == "withdrawn":
        task_repair_store.withdraw(value["repair_id"], "operator", "stop", 1)
    elif mode == "revised":
        task_goal_store.define(prepared[0]["source_id"], prepared[1]["contract"], "operator", "revision", 2)
    else:
        row = {"run_id": "child", "task_id": "task", "status": "running", "phase": "execution", "attempt": 1,
            "ts": "2026-10-10T00:00:00Z", "parent_run_id": prepared[2]["run_id"], "parent_source_id": prepared[0]["source_id"]}
        _write(Path(prepared[0]["data_dir"]) / "agents/ledger.jsonl", [row])
    monkeypatch.setattr(workspace, "admit_layout", lambda *args: pytest.fail("ineligible request cannot read workspace"))
    with pytest.raises(ValueError, match="no Git inspection"):
        _report(owned)


def test_normal_reads_never_inspect_registered_workspace_or_spawn_git(owned, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("ordinary views cannot turn a metadata request into a Git probe")
    monkeypatch.setattr(workspace, "inspect_workspace", forbidden)
    monkeypatch.setattr(git_probe, "run_bounded", forbidden)
    for action in ("show", "check"):
        expected = 0 if action == "show" else 3
        assert ops_main(["repairs", action, owned[1]["repair_id"]]) == expected
        capsys.readouterr()
    assert ops_main(["repairs", "list", "--json"]) == 0
    assert "git_observation" not in capsys.readouterr().out


def test_cli_workspace_inspection_has_no_save_execution_or_mutation_success(owned, capsys):
    before = _bytes(owned[2])
    args = ["repairs", "inspect-workspace", owned[1]["repair_id"], "--expected-head", owned[3], "--json"]
    assert ops_main(args) == 2
    assert "--confirm" in capsys.readouterr().out
    assert ops_main([*args, "--confirm"]) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["inspection_state"] == "observed", report["diagnostic"]
    _no_authority(report)
    assert _bytes(owned[2]) == before


def test_explicit_subprocess_environment_does_not_mutate_parent_or_default_behavior(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ENV_TEST", "parent")
    cmd = [sys.executable, "-c", "import os;print(os.getenv('WORKSPACE_ENV_TEST'))"]
    result = run_bounded(cmd, str(tmp_path), 2, 1024, False, "env regression", env={"WORKSPACE_ENV_TEST": "child"})
    assert result.ok and result.stdout.strip() == "child"
    result = run_bounded(cmd, str(tmp_path), 2, 1024, False, "default env regression")
    assert result.ok and result.stdout.strip() == "parent"
    assert os.environ["WORKSPACE_ENV_TEST"] == "parent"
