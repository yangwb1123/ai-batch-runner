from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pbatch.campaign_recovery import (
    RECOVERY_BLOCKED,
    RECOVERY_RESTORED,
    recover_worktree,
    recovery_attempt_count,
)
from pbatch.models import Stage
from pbatch.repo_gates import (capture_stage_baseline,
                               capture_stage_dirty_paths,
                               read_only_violations)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(path), *args],
                            capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / ".gitignore").write_text(".pi-batch/\n", encoding="utf-8")
    (tmp_path / "source.py").write_text("original\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path, _git(tmp_path, "rev-parse", "HEAD"), _git(
        tmp_path, "branch", "--show-current")


def test_failed_attempt_is_stashed_and_restored(tmp_path: Path) -> None:
    root, baseline, branch = _repo(tmp_path)
    (root / "source.py").write_text("agent change\n", encoding="utf-8")
    (root / "new.py").write_text("untracked agent change\n", encoding="utf-8")
    recovery_root = root / ".pi-batch" / "campaign-recovery"

    result = recover_worktree(
        root, baseline, branch, recovery_root, "direction-one",
        start_clean=True, reason="design timeout")

    assert result.status == RECOVERY_RESTORED
    assert result.stash_ref
    assert result.report and Path(result.report).is_file()
    assert _git(root, "rev-parse", "HEAD") == baseline
    assert _git(root, "status", "--porcelain") == ""
    payload = json.loads(Path(result.report).read_text(encoding="utf-8"))
    assert payload["status"] == RECOVERY_RESTORED
    assert payload["failure"] == "design timeout"
    assert recovery_attempt_count(root, recovery_root, "direction-one") == 1
    assert "new.py" in _git(root, "stash", "show", "--name-only", "-u", "refs/stash")


def test_recovery_refuses_to_touch_preexisting_dirty_tree(tmp_path: Path) -> None:
    root, baseline, branch = _repo(tmp_path)
    source = root / "source.py"
    source.write_text("user change\n", encoding="utf-8")
    recovery_root = root / ".pi-batch" / "campaign-recovery"

    result = recover_worktree(
        root, baseline, branch, recovery_root, "direction-two",
        start_clean=False, reason="pipeline failed")

    assert result.status == RECOVERY_BLOCKED
    assert "dirty before" in result.reason
    assert source.read_text(encoding="utf-8") == "user change\n"
    assert "source.py" in _git(root, "status", "--porcelain")


def test_read_only_stage_detects_unexpected_source_changes(tmp_path: Path) -> None:
    root, _baseline, _branch = _repo(tmp_path)
    baseline = capture_stage_dirty_paths(str(root))
    source = root / "source.py"
    output = root / "report.md"
    source.write_text("review changed source\n", encoding="utf-8")
    output.write_text("review report\n", encoding="utf-8")

    violations = read_only_violations(
        Stage(name="review", cwd=str(root), read_only=True),
        [str(output)], baseline)

    assert violations == ["source.py"]


def test_read_only_stage_tolerates_a_preexisting_dirty_worktree(
        tmp_path: Path) -> None:
    """A shared worktree that was ALREADY dirty must not fail every read-only
    stage.

    Campaign logs 2026-09-18/19 recorded 43 `read-only stage started with
    existing changes` halts, each aborting the pipeline at requirements — so a
    permanently dirty tree could never pass a single direction, which is why
    that campaign produced 98 PIPELINE_FAILED rounds and 0 commits.
    """
    root, _baseline, _branch = _repo(tmp_path)
    (root / "user_work.py").write_text("unrelated user change\n", encoding="utf-8")

    baseline = capture_stage_baseline(str(root), read_only=True)
    violations = read_only_violations(
        Stage(name="requirements", cwd=str(root), read_only=True), [], baseline)

    assert violations == []


def test_read_only_stage_detects_edits_to_a_preexisting_dirty_file(
        tmp_path: Path) -> None:
    """Comparing dirty PATH sets alone would miss this, so a read-only baseline
    carries content digests."""
    root, _baseline, _branch = _repo(tmp_path)
    dirty = root / "user_work.py"
    dirty.write_text("unrelated user change\n", encoding="utf-8")

    baseline = capture_stage_baseline(str(root), read_only=True)
    dirty.write_text("stage rewrote the user's file\n", encoding="utf-8")
    violations = read_only_violations(
        Stage(name="requirements", cwd=str(root), read_only=True), [], baseline)

    assert violations == ["user_work.py"]


def test_read_only_baseline_digest_follows_the_stage_wiring(tmp_path: Path) -> None:
    """Read-only stages get a digest-bearing baseline; commit stages keep the
    two-element baseline that stage ownership relies on."""
    from pbatch.pipeline import _stage_git_baseline

    root, _baseline, _branch = _repo(tmp_path)
    (root / "dirty.py").write_text("x\n", encoding="utf-8")

    read_only = _stage_git_baseline(
        Stage(name="requirements", cwd=str(root), read_only=True))
    assert len(read_only) == 3
    assert read_only[2]

    committing = _stage_git_baseline(
        Stage(name="implement", cwd=str(root), git_commit=True))
    assert len(committing) == 2


def test_campaign_retry_profile_narrows_meta_stage(tmp_path: Path) -> None:
    from pbatch.campaign_models import CampaignSettings, Direction
    from pbatch.campaign_pipeline import materialize_pipeline
    import yaml

    root, _baseline, _branch = _repo(tmp_path)
    template = root / "pipeline.yaml"
    template.write_text(
        "git_commit: true\n"
        "stages:\n"
        "  - name: requirements\n"
        "    from_prompt: start\n"
        "    output: docs/requirements.md\n"
        "  - name: review\n"
        "    from_outputs: requirements\n"
        "    meta: true\n"
        "    role_dir: prompts\n"
        "    output_dir: docs/reviews\n"
        "    max_roles_per_iteration: 4\n"
        "    meta_role_retries: 2\n"
        "    tasks: []\n",
        encoding="utf-8")
    (root / "prompts").mkdir()
    (root / "prompts" / "qa_lead.md").write_text("review", encoding="utf-8")
    analysis = root / "analysis.json"
    analysis.write_text("[]\n", encoding="utf-8")
    direction = Direction(module="src", title="Retry safely")
    settings = CampaignSettings(
        pipeline_template="pipeline.yaml", output_dir="out",
        recovery_enabled=True, recovery_max_roles_per_iteration=2,
        recovery_allow_failed_roles=1)
    run_dir = root / "out" / "runs" / direction.direction_id
    run_dir.mkdir(parents=True)
    (run_dir / "wreckage.md").write_text(
        "stage failed: review timeout\n", encoding="utf-8")

    path = materialize_pipeline(root, settings, direction, analysis, True)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    requirements, review = data["stages"]
    assert requirements["read_only"] is True
    assert review["read_only"] is True
    assert review["max_roles_per_iteration"] == 2
    assert review["meta_role_retries"] == 0
    assert review["meta_max_failed_roles"] == 1
    assert "SELF-REPAIR RETRY" in requirements["from_prompt"]


def test_failed_read_only_pipeline_is_recovered_before_retry(tmp_path: Path, monkeypatch) -> None:
    import sys
    from pbatch import config
    from pbatch.campaign_models import CampaignSettings, Direction
    from pbatch.campaign_pipeline import run_direction

    root, baseline, _branch = _repo(tmp_path)
    template = root / "pipeline.yaml"
    template.write_text(
        "stages:\n"
        "  - name: requirements\n"
        "    from_prompt: inspect\n"
        "    output: docs/requirements.md\n",
        encoding="utf-8")
    analysis = root / "analysis.json"
    analysis.write_text("[]\n", encoding="utf-8")
    agent = root / "agent.py"
    agent.write_text(
        "import pathlib, sys\n"
        "pathlib.Path('source.py').write_text('mutated\\n')\n"
        "print('report')\n",
        encoding="utf-8")
    settings = CampaignSettings(
        pipeline_template="pipeline.yaml", output_dir="out",
        recovery_enabled=True, recovery_max_attempts=1)
    direction = Direction(module="src", title="Guard phase", problem="review edits",
                          evidence=("source.py",), acceptance=("clean retry",))
    monkeypatch.setattr(config, "AGENT_BIN", sys.executable)
    # The executable receives -p and the prompt; the script path is not part
    # of the configured argv, so use a small adapter wrapper instead.
    wrapper = root / "agent.sh"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {agent}\n", encoding="utf-8")
    wrapper.chmod(0o755)
    _git(root, "add", "pipeline.yaml", "analysis.json", "agent.py", "agent.sh")
    _git(root, "commit", "-qm", "test inputs")
    baseline = _git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(config, "AGENT_BIN", str(wrapper))

    outcome = run_direction(root, settings, direction, analysis)

    assert outcome.status == "PIPELINE_FAILED"
    assert outcome.recovery_status == "RESTORED"
    from pbatch.campaign_state import git_worktree_evidence
    assert _git(root, "rev-parse", "HEAD") == baseline
    assert git_worktree_evidence(root, excludes=("out",))["clean_commit"] == baseline
    assert "source.py" not in _git(root, "status", "--porcelain")
    assert (root / "source.py").read_text(encoding="utf-8") == "original\n"
    second = run_direction(root, settings, direction, analysis)
    assert second.recovery_status == "RESTORED"
    third = run_direction(root, settings, direction, analysis)
    assert third.recovery_status == "BLOCKED"
    assert "attempt budget exhausted" in third.reason


def test_global_direction_cap_limits_one_pipeline_per_round(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace
    from pbatch import campaign_exec
    from pbatch.campaign_models import CampaignSettings, Direction
    from pbatch.campaign_state import StateStore

    root, _baseline, _branch = _repo(tmp_path)
    for module in ("one", "two"):
        directory = root / "src" / module
        directory.mkdir(parents=True)
        (directory / "code.py").write_text("pass\n", encoding="utf-8")
    (root / "pipeline.yaml").write_text(
        "stages:\n  - name: requirements\n    from_prompt: work\n    output: report.md\n",
        encoding="utf-8")
    settings = CampaignSettings(
        pipeline_template="pipeline.yaml", output_dir="out",
        max_directions=1, max_total_directions=1)
    args = SimpleNamespace(
        force=False, reuse=False, skip_passed=False, retry_failed=False,
        model="", provider="")
    context = SimpleNamespace(
        root=root, settings=settings, args=args,
        store=StateStore(root / "out" / "state.jsonl"), snapshot={})
    directions = {
        "src/one": [Direction(module="src/one", title="One", problem="gap",
                               evidence=("src/one/code.py",), acceptance=("test",))],
        "src/two": [Direction(module="src/two", title="Two", problem="gap",
                               evidence=("src/two/code.py",), acceptance=("test",))],
    }
    events = []
    monkeypatch.setattr(campaign_exec, "direction_fingerprint",
                        lambda *args: "fingerprint")
    monkeypatch.setattr(campaign_exec, "_direction_event",
                        lambda *args, **kwargs: events.append((args, kwargs)))

    selected, failures = campaign_exec._select(
        context, ["src/one", "src/two"], directions)

    assert failures == 0
    assert [item[0].title for item in selected] == ["One"]
    assert any("global selection quota" in item[0][5]
               for item in events if item[0][2] == "DIRECTION_REJECTED")


def test_stall_stop_fires_with_recovery_disabled():
    """The round stall stop is a SAFETY circuit, not part of the opt-in
    stash-based self-repair.

    Coupling the two made the stop unreachable at the shipped default
    (``recovery.enabled = false`` → ``return 0, ""``): on 2026-09-19 an iris-ui
    campaign churned 123 consecutive zero-progress rounds against a provider
    quota outage for ~8h because the counter was reset every round.
    """
    from types import SimpleNamespace

    from pbatch.campaign_models import CampaignSettings
    from pbatch.campaign_recovery import update_round_recovery

    settings = CampaignSettings(recovery_enabled=False,
                                recovery_stop_after_stalled_rounds=2)
    context = SimpleNamespace(round_progress=0, recovery_blocked=False,
                              settings=settings)

    stalled, stop = update_round_recovery(context, 0)
    assert stalled == 1
    assert stop == ""

    stalled, stop = update_round_recovery(context, stalled)
    assert stalled == 2
    assert "made no progress for 2 round(s)" in stop


def test_stall_counter_resets_on_a_productive_round():
    from types import SimpleNamespace

    from pbatch.campaign_models import CampaignSettings
    from pbatch.campaign_recovery import update_round_recovery

    settings = CampaignSettings(recovery_enabled=False,
                                recovery_stop_after_stalled_rounds=2)
    context = SimpleNamespace(round_progress=0, recovery_blocked=False,
                              settings=settings)

    stalled, _ = update_round_recovery(context, 0)
    assert stalled == 1

    context.round_progress = 1
    stalled, stop = update_round_recovery(context, stalled)
    assert stalled == 0
    assert stop == ""


def test_stall_stop_zero_limit_opts_out_and_blocked_recovery_short_circuits():
    from types import SimpleNamespace

    from pbatch.campaign_models import CampaignSettings
    from pbatch.campaign_recovery import update_round_recovery

    disabled = SimpleNamespace(
        round_progress=0, recovery_blocked=False,
        settings=CampaignSettings(recovery_enabled=False,
                                  recovery_stop_after_stalled_rounds=0))
    stalled, stop = update_round_recovery(disabled, 0)
    assert stalled == 1
    assert stop == ""

    blocked = SimpleNamespace(
        round_progress=0, recovery_blocked=True,
        settings=CampaignSettings(recovery_enabled=True,
                                  recovery_stop_after_stalled_rounds=2))
    stalled, stop = update_round_recovery(blocked, 0)
    assert stalled == 0
    assert "self-repair blocked" in stop
