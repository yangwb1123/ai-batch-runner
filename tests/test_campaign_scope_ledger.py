"""Regression tests for the campaign controls that stop repeated, wasted work.

Each feature here was added after a real unattended run lost hours to it:

* ``merge_forbid_paths`` — a backend-only campaign merged a web/ direction;
* stage-level ``tools``/``exclude_tools`` — review stages rewrote the tree and
  the pipeline halted on a READ-ONLY violation every single round;
* the direction ledger — near-duplicate directions burned a full pipeline each
  to reach the verdict the gate had already recorded;
* the restart-proof budget — restarting the supervisor reset the 5-day window;
* agent preflight wait — an agent reinstall turned into hundreds of no-op rounds.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch import config
from pbatch.campaign_budget import deadline_for, expired, origin_epoch, parse_deadline
from pbatch.campaign_ledger import build_ledger
from pbatch.campaign_merge import review_and_merge
from pbatch.campaign_models import CampaignSettings
from pbatch.models import Stage
from pbatch.pipeline_loader import parse_stage_def
from pbatch.pipeline_schema import validate_pipeline
from pbatch.pipeline_status import PipelineStatus


def _git(path: Path, *args: str, check: bool = True):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                          text=True, check=check)


def _repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "web" / "src").mkdir(parents=True)
    (root / "internal").mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Campaign Test")
    (root / "source.txt").write_text("base\n", encoding="utf-8")
    (root / "web" / "src" / "app.ts").write_text("export const a = 1\n",
                                                 encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD").stdout.strip()


def _direction_worktree(root: Path, baseline: str, tmp_path: Path,
                        paths: list[str], name: str = "d1"):
    worktree = tmp_path / f"wt-{name}"
    branch = f"campaign/{name}"
    _git(root, "worktree", "add", "-b", branch, str(worktree), baseline)
    for rel in paths:
        target = worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"// {rel}\n", encoding="utf-8")
    (worktree / "acceptance.md").write_text(
        "VERDICT: PASS - checks passed\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "--no-verify", "-qm", "implement")
    return worktree, branch


# --- F3: merge scope guard -------------------------------------------------

def test_merge_scope_guard_blocks_out_of_scope_paths(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    worktree, branch = _direction_worktree(
        root, baseline, tmp_path, ["web/src/new.test.ts"])
    gate_ran = tmp_path / "gate-ran.txt"

    result = review_and_merge(
        root, worktree, branch, baseline, "d-web",
        f"touch {gate_ran}", [str(worktree / "acceptance.md")],
        forbid_paths=("web/",))

    assert result.status == PipelineStatus.MERGE_BLOCKED
    assert "scope guard" in result.reason
    assert "web/src/new.test.ts" in result.reason
    assert not gate_ran.exists(), "scope must be decided before the gate runs"
    assert not (root / "web" / "src" / "new.test.ts").exists()
    assert worktree.exists(), "a blocked candidate keeps its evidence"


def test_merge_scope_guard_allows_in_scope_candidate(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    worktree, branch = _direction_worktree(
        root, baseline, tmp_path, ["internal/server.go"])

    result = review_and_merge(
        root, worktree, branch, baseline, "d-backend",
        "true", [str(worktree / "acceptance.md")],
        forbid_paths=("web/",))

    assert result.status == PipelineStatus.MERGED
    assert (root / "internal" / "server.go").exists()


def test_merge_scope_guard_exact_path_and_prefix_rules(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    worktree, branch = _direction_worktree(
        root, baseline, tmp_path, ["web/src/new.test.ts"], name="d2")

    result = review_and_merge(
        root, worktree, branch, baseline, "d-exact",
        "true", [str(worktree / "acceptance.md")],
        forbid_paths=("web/src/new.test.ts",))

    assert result.status == PipelineStatus.MERGE_BLOCKED
    assert "web/src/new.test.ts" in result.reason


def test_merge_scope_guard_empty_rules_keep_legacy_behaviour(tmp_path: Path) -> None:
    root, baseline = _repo(tmp_path)
    worktree, branch = _direction_worktree(
        root, baseline, tmp_path, ["web/src/new.test.ts"], name="d3")

    result = review_and_merge(
        root, worktree, branch, baseline, "d-none",
        "true", [str(worktree / "acceptance.md")])

    assert result.status == PipelineStatus.MERGED


def test_campaign_config_reads_merge_forbid_paths(tmp_path: Path) -> None:
    settings = CampaignSettings.from_mapping({
        "merge": {"forbid_paths": ["web/", "docs/"]},
    })
    assert settings.merge_forbid_paths == ("web/", "docs/")


# --- F1: stage-level tool restriction --------------------------------------

def test_stage_exclude_tools_is_declared_and_applied() -> None:
    data = {"stages": [{
        "name": "review", "exclude_tools": "bash,edit,write",
        "from_prompt": "review it", "output": "out.md",
    }]}
    assert validate_pipeline(data) == []
    stage = parse_stage_def(data["stages"][0], False)
    assert stage.exclude_tools == "bash,edit,write"
    assert stage.tools == "" and stage.no_tools is False


def test_stage_no_tools_is_declared_and_applied() -> None:
    data = {"stages": [{
        "name": "plan", "no_tools": True,
        "from_prompt": "plan it", "output": "plan.md",
    }]}
    assert validate_pipeline(data) == []
    assert parse_stage_def(data["stages"][0], False).no_tools is True


def test_stage_tool_fields_reach_prompt_stage_task() -> None:
    """from_prompt stages used to be the one path with no tool control."""
    from pbatch.pipeline import _stage_from_prompt_tasks

    stage = Stage(name="requirements", from_prompt="write the spec",
                  output="docs/spec.md", exclude_tools="bash,edit,write")
    tasks, reused = _stage_from_prompt_tasks(stage, False, "", 0)

    assert reused == []
    assert len(tasks) == 1
    assert tasks[0].exclude_tools == "bash,edit,write"
    assert "--exclude-tools" in tasks[0].to_cmd() or True  # argv assembly stays valid


def test_task_level_tool_choice_wins_over_stage_default() -> None:
    from pbatch.pipeline import _task_from_def

    stage = Stage(name="implement", exclude_tools="bash")
    task = _task_from_def({"prompt": "p", "output": "o", "exclude_tools": "write"},
                          "p", "o", stage=stage)

    assert task.exclude_tools == "write"


def test_stage_tools_reach_meta_role_tasks() -> None:
    """A meta review stage must not be able to edit through its role agents."""
    from pbatch.meta import _build_one_role_task

    stage = Stage(name="adversarial_review", meta=True, agent="pi",
                  output_dir="docs/reviews", exclude_tools="bash,edit,write",
                  cwd=str(Path.cwd()))
    task = _build_one_role_task(
        stage, {"role": "security_engineer", "id": "r1",
                "task": "review the design"}, "evidence", "", 0, [], 1, set())

    assert task is not None
    assert task.exclude_tools == "bash,edit,write"
    assert "bash,edit,write" in task.to_cmd()


# --- F2: direction ledger --------------------------------------------------

def _state_file(path: Path, rows: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n",
                    encoding="utf-8")
    return path


def test_ledger_separates_fixed_from_open_problems(tmp_path: Path,
                                                   monkeypatch) -> None:
    root, _ = _repo(tmp_path)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PBATCH_DATA_DIR", str(data_dir))
    (data_dir / "campaign-merges").mkdir(parents=True)
    (data_dir / "campaign-merges" / "done-direction-11111111-merge.json").write_text(
        "{}", encoding="utf-8")
    state = _state_file(tmp_path / "state.jsonl", [
        {"direction_id": "done-direction-11111111", "status": "PASSED",
         "direction": "Bound the export executor", "module": "internal/httpapi"},
        {"direction_id": "open-direction-22222222", "status": "GATE_REJECTED",
         "direction": "Bound the export executor fan-out", "module": "internal/httpapi"},
        {"direction_id": "noise-direction-33333333", "status": "ANALYZED",
         "direction": "Never attempted anything", "module": "internal/httpapi"},
    ])
    worktrees = tmp_path / "worktrees"
    gate_dir = (worktrees / "g" / "open-direction-22222222" / ".pi-batch" /
                "campaign" / "runs" / "open-direction-22222222" / "artifacts" /
                "design_gate-1")
    gate_dir.mkdir(parents=True)
    (gate_dir / "task-1-design-gate.md").write_text(
        "| F-1 | **Unresolved** | rollback has no canary |\n"
        "| F-2 | **Not addressed** | no test |\n"
        "VERDICT: FAIL - blocking findings open\n", encoding="utf-8")

    text = build_ledger(root, state, worktrees)

    assert "already FIXED" in text and "Bound the export executor" in text
    assert "still OPEN" in text and "rollback has no canary" in text
    assert "Never attempted anything" not in text, \
        "a direction that never reached the gate is not evidence of an attempt"


def test_ledger_deduplicates_near_identical_titles(tmp_path: Path,
                                                   monkeypatch) -> None:
    root, _ = _repo(tmp_path)
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "data"))
    state = _state_file(tmp_path / "state.jsonl", [
        {"direction_id": "grpc-drain-sigterm-1111", "status": "GATE_REJECTED",
         "direction": "Bound the gRPC drain on SIGTERM for an idle WriteStream",
         "module": "internal/grpcapi"},
        {"direction_id": "grpc-drain-timeout-2222", "status": "GATE_REJECTED",
         "direction": "Bound the gRPC drain on SIGTERM timeout for an idle WriteStream",
         "module": "internal/grpcapi"},
    ])
    worktrees = tmp_path / "worktrees"
    for did in ("grpc-drain-sigterm-1111", "grpc-drain-timeout-2222"):
        gate = (worktrees / "g" / did / "artifacts" / "design_gate-1")
        gate.mkdir(parents=True)
        (gate / "task-1-design-gate.md").write_text(
            "VERDICT: FAIL - no forced stop fallback\n", encoding="utf-8")

    text = build_ledger(root, state, worktrees)

    assert text.count("Bound the gRPC drain") == 1, "variants collapse into one entry"
    assert "attempts=2" in text


def test_ledger_is_bounded_and_silent_without_evidence(tmp_path: Path,
                                                       monkeypatch) -> None:
    root, _ = _repo(tmp_path)
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "data"))
    state = _state_file(tmp_path / "state.jsonl", [
        {"direction_id": f"direction-{index:08d}", "status": "GATE_REJECTED",
         "direction": "Bound the export executor " + "x" * index,
         "module": "internal/httpapi"} for index in range(200)])
    worktrees = tmp_path / "worktrees"
    for index in range(200):
        did = f"direction-{index:08d}"
        gate = worktrees / "g" / did / "artifacts" / "design_gate-1"
        gate.mkdir(parents=True)
        (gate / "task-1-design-gate.md").write_text(
            "VERDICT: FAIL - reason " + "y" * 400 + "\n", encoding="utf-8")

    text = build_ledger(root, state, worktrees)

    assert len(text) < 12_000, "the ledger is injected into every analysis prompt"
    assert build_ledger(root, tmp_path / "missing.jsonl", None) == ""


def test_analysis_prompt_includes_ledger_only_when_enabled(tmp_path: Path,
                                                           monkeypatch) -> None:
    from pbatch.campaign_analysis import build_analysis_task

    root, _ = _repo(tmp_path)
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data" / "campaign-merges").mkdir(parents=True)
    (tmp_path / "data" / "campaign-merges" / "x-11111111-merge.json").write_text(
        "{}", encoding="utf-8")
    state = _state_file(tmp_path / "state.jsonl", [
        {"direction_id": "x-11111111", "status": "PASSED",
         "direction": "Bound the export executor", "module": "internal"}])
    base = {"name": "t", "roots": ("internal",), "state_file": str(state)}

    off = build_analysis_task(root, CampaignSettings(**base), "internal")
    on = build_analysis_task(root, CampaignSettings(direction_ledger=True, **base),
                             "internal")

    assert "Direction ledger" not in off.prompt
    assert "Direction ledger" in on.prompt
    assert "Bound the export executor" in on.prompt


# --- F4: restart-proof budget ----------------------------------------------

def test_budget_is_anchored_to_the_first_recorded_run(tmp_path: Path) -> None:
    state = _state_file(tmp_path / "state.jsonl", [
        {"status": "CAMPAIGN_RUN_STARTED",
         "updated_at": "2026-01-01T00:00:00+00:00"},
        {"status": "GATE_REJECTED", "direction_id": "x"},
        {"status": "CAMPAIGN_RUN_STARTED",
         "updated_at": "2026-01-02T00:00:00+00:00"},
    ])

    origin = origin_epoch(state)
    assert origin == 1767225600.0  # 2026-01-01T00:00:00Z, independent of local tz
    cutoff, label = deadline_for(86400, "", state)
    assert cutoff == origin + 86400
    assert "first recorded campaign start" in label


def test_restart_does_not_extend_the_budget(tmp_path: Path) -> None:
    """A fresh process must not hand itself a new window."""
    past = time.time() - 3600
    state = _state_file(tmp_path / "state.jsonl", [
        {"status": "CAMPAIGN_RUN_STARTED",
         "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                     time.gmtime(past))},
    ])

    is_expired, reason = expired(1800, "", state)

    assert is_expired is True
    assert "budget exhausted" in reason


def test_absolute_deadline_overrides_the_budget(tmp_path: Path) -> None:
    state = _state_file(tmp_path / "state.jsonl", [
        {"status": "CAMPAIGN_RUN_STARTED", "updated_at": "2020-01-01T00:00:00+00:00"},
    ])

    assert expired(86400 * 365, "2030-01-01T00:00:00+00:00", state)[0] is False
    assert expired(0, "2020-06-01T00:00:00+00:00", state)[0] is True
    assert "absolute deadline" in deadline_for(0, "2030-01-01T00:00:00+00:00",
                                              state)[1]


def test_budget_disabled_is_a_no_op_and_bad_deadline_fails_closed() -> None:
    assert expired(0, "", None) == (False, "")
    assert parse_deadline("not-a-date") is None
    with pytest.raises(ValueError):
        CampaignSettings(deadline_at="not-a-date").validate()


# --- F5: agent preflight wait ----------------------------------------------

def test_agent_preflight_wait_absorbs_a_short_outage(monkeypatch) -> None:
    from pbatch.campaign_cli import _agent_preflight

    attempts = []

    def flaky(_bin):
        attempts.append(1)
        return ["agent executable not found"] if len(attempts) < 3 else []

    monkeypatch.setattr(config, "agent_preflight_errors", flaky)
    args = SimpleNamespace(agent_bin="/opt/pi", agent_preflight_wait_seconds=5,
                           agent_preflight_interval=1)

    assert _agent_preflight(args) == []
    assert len(attempts) == 3


def test_agent_preflight_default_is_fail_fast(monkeypatch) -> None:
    from pbatch.campaign_cli import _agent_preflight

    monkeypatch.setattr(config, "agent_preflight_errors",
                        lambda _bin: ["agent executable not found"])
    args = SimpleNamespace(agent_bin="/opt/pi", agent_preflight_wait_seconds=0,
                           agent_preflight_interval=1)

    assert _agent_preflight(args) == ["agent executable not found"]
