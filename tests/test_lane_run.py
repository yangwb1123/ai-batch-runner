"""Two-lane concurrent execution with a serial, gated merge into main."""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from pbatch import lane_run

pytestmark = pytest.mark.skipif(subprocess.run(["git", "--version"],
                                               capture_output=True).returncode != 0,
                                reason="git required")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    run = lambda *args: subprocess.run(["git", "-C", str(root), *args], check=True,
                                       capture_output=True, text=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    run("add", "base.txt")
    run("commit", "-qm", "base")
    return root


def _lane(root: Path, lane_id: str, script: str) -> lane_run.Lane:
    return lane_run.Lane(
        lane_id=lane_id, branch="feat/" + lane_id,
        worktree=root.parent / ("wt-" + lane_id),
        command=(str(root.parent / (lane_id + ".sh")),), timeout=60)


def _script(root: Path, lane_id: str, body: str) -> None:
    path = root.parent / (lane_id + ".sh")
    path.write_text("#!/bin/sh\nset -e\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)


def _commit_script(root: Path, lane_id: str, filename: str, text: str) -> None:
    _script(root, lane_id, f"""
        printf '%s\\n' '{text}' > {filename}
        git add {filename}
        git -c user.email=t@t -c user.name=t commit -qm 'lane {lane_id}'
    """)


def test_two_independent_lanes_merge_into_main_one_after_another(tmp_path):
    root = _repo(tmp_path)
    _commit_script(root, "alpha", "alpha.txt", "alpha")
    _commit_script(root, "beta", "beta.txt", "beta")
    lanes = [_lane(root, "alpha", ""), _lane(root, "beta", "")]
    receipt = lane_run.run_round(root, lanes, gate="test -f alpha.txt -o -f beta.txt", jobs=2)
    assert receipt["merged"] == ["alpha", "beta"]
    assert receipt["blocked"] == []
    tracked = subprocess.run(["git", "-C", str(root), "ls-tree", "--name-only", "HEAD"],
                             capture_output=True, text=True).stdout.split()
    assert "alpha.txt" in tracked and "beta.txt" in tracked
    log = subprocess.run(["git", "-C", str(root), "log", "--oneline"], capture_output=True,
                         text=True).stdout
    assert log.count("merge: session") == 2


def test_a_failed_lane_is_never_merged_and_keeps_its_worktree(tmp_path):
    root = _repo(tmp_path)
    _commit_script(root, "alpha", "alpha.txt", "alpha")
    _script(root, "beta", "exit 7\n")
    lanes = [_lane(root, "alpha", ""), _lane(root, "beta", "")]
    receipt = lane_run.run_round(root, lanes, gate="test -f alpha.txt", jobs=2)
    assert receipt["merged"] == ["alpha"]
    assert receipt["blocked"] == ["beta"]
    assert (root.parent / "beta.sh").exists()
    tracked = subprocess.run(["git", "-C", str(root), "ls-tree", "--name-only", "HEAD"],
                             capture_output=True, text=True).stdout.split()
    assert "alpha.txt" in tracked and "beta.txt" not in tracked


def test_a_red_gate_blocks_the_merge_and_leaves_main_unchanged(tmp_path):
    root = _repo(tmp_path)
    _commit_script(root, "alpha", "alpha.txt", "alpha")
    lanes = [_lane(root, "alpha", "")]
    before = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                            text=True).stdout.strip()
    receipt = lane_run.run_round(root, lanes, gate="exit 1")
    assert receipt["merged"] == [] and receipt["blocked"] == ["alpha"]
    after = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True,
                           text=True).stdout.strip()
    assert before == after


def test_an_uncommitted_lane_is_refused_instead_of_merged(tmp_path):
    root = _repo(tmp_path)
    _script(root, "alpha", "printf 'x\\n' > alpha.txt\n")
    lanes = [_lane(root, "alpha", "")]
    receipt = lane_run.run_round(root, lanes, gate="test -f alpha.txt")
    assert receipt["blocked"] == ["alpha"]
    statuses = {row["lane"]: row["status"] for row in receipt["lanes"]}
    assert statuses["alpha"] == "lane_uncommitted"


def test_dirty_root_is_refused_before_anything_is_created(tmp_path):
    root = _repo(tmp_path)
    (root / "base.txt").write_text("dirty\n", encoding="utf-8")
    lanes = [_lane(root, "alpha", "")]
    with pytest.raises(lane_run.LanePlanError, match="dirty"):
        lane_run.run_round(root, lanes, gate="true")
    assert not (root.parent / "wt-alpha").exists()


@pytest.mark.parametrize("lanes,gate,message", [
    ([], "true", "1..8"),
    (None, "true", "1..8"),
])
def test_invalid_plans_fail_closed(tmp_path, lanes, gate, message):
    root = _repo(tmp_path)
    with pytest.raises(lane_run.LanePlanError, match=message):
        lane_run.run_round(root, lanes, gate=gate)


def test_gate_and_overlapping_scopes_are_required(tmp_path):
    root = _repo(tmp_path)
    lane = _lane(root, "alpha", "")
    with pytest.raises(lane_run.LanePlanError, match="gate"):
        lane_run.run_round(root, [lane], gate="")
    duplicate = lane_run.Lane(lane_id="alpha", branch="feat/other",
                              worktree=lane.worktree, command=("true",))
    with pytest.raises(lane_run.LanePlanError, match="worktree|duplicate"):
        lane_run.validate_plan(root, [lane, duplicate], "true")
    inside = lane_run.Lane(lane_id="inside", branch="feat/inside",
                           worktree=root / "nested", command=("true",))
    with pytest.raises(lane_run.LanePlanError, match="outside"):
        lane_run.validate_plan(root, [inside], "true")


def test_round_receipt_is_appended_for_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
    root = _repo(tmp_path)
    _commit_script(root, "alpha", "alpha.txt", "alpha")
    lane_run.run_round(root, [_lane(root, "alpha", "")], gate="test -f alpha.txt")
    path = Path(os.environ["PBATCH_RUNTIME_HOME"]) / "projects"
    receipts = list(path.rglob("lanes/rounds.jsonl"))
    assert receipts and '"merged": ["alpha"]' in receipts[0].read_text(encoding="utf-8")


def test_a_lane_touching_paths_outside_its_scope_is_not_merged(tmp_path):
    root = _repo(tmp_path)
    _script(root, "alpha", "printf 'x\\n' > outside.txt\ngit add outside.txt\n"
                          "git -c user.email=t@t -c user.name=t commit -qm sneaky\n")
    lane = lane_run.Lane(lane_id="alpha", branch="feat/alpha",
                         worktree=root.parent / "wt-alpha",
                         command=(str(root.parent / "alpha.sh"),),
                         scope=("pbatch/",))
    receipt = lane_run.run_round(root, [lane], gate="true")
    assert receipt["merged"] == [] and receipt["blocked"] == ["alpha"]
    statuses = {row["lane"]: row["status"] for row in receipt["lanes"]}
    assert statuses["alpha"] == "lane_scope_violation"
    tracked = subprocess.run(["git", "-C", str(root), "ls-tree", "--name-only", "HEAD"],
                             capture_output=True, text=True).stdout.split()
    assert "outside.txt" not in tracked


def test_a_lane_with_a_declared_scope_can_still_merge(tmp_path):
    root = _repo(tmp_path)
    _script(root, "alpha", "mkdir -p pbatch\nprintf 'x\\n' > pbatch/allowed.txt\ngit add pbatch/allowed.txt\n"
                          "git -c user.email=t@t -c user.name=t commit -qm in-scope\n")
    lane = lane_run.Lane(lane_id="alpha", branch="feat/alpha",
                         worktree=root.parent / "wt-alpha",
                         command=(str(root.parent / "alpha.sh"),),
                         scope=("pbatch/*",))
    receipt = lane_run.run_round(root, [lane], gate="true")
    assert receipt["merged"] == ["alpha"]


def test_a_blocked_gate_records_its_output_tail_in_the_receipt(tmp_path):
    root = _repo(tmp_path)
    _commit_script(root, "alpha", "alpha.txt", "alpha")
    lane = lane_run.Lane(lane_id="alpha", branch="feat/alpha",
                         worktree=root.parent / "wt-alpha",
                         command=(str(root.parent / "alpha.sh"),))
    receipt = lane_run.run_round(root, [lane], gate="echo synthetic-gate-failure >&2; exit 1")
    row = receipt["lanes"][0]
    assert row["merge_status"] == "merge_blocked"
    assert "synthetic-gate-failure" in row["merge_tail"]
