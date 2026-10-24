"""Two-lane concurrent execution with a serial, gated merge into main."""
from __future__ import annotations

import json
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


def _gate_script(root: Path, name: str) -> str:
    return str(root.parent / name)


def test_gate_failure_without_test_ids_is_not_retried(tmp_path):
    root = _repo(tmp_path)
    _commit_script(root, "alpha", "alpha.txt", "alpha")
    lane = lane_run.Lane(lane_id="alpha", branch="feat/alpha",
                         worktree=root.parent / "wt-alpha",
                         command=(str(root.parent / "alpha.sh"),))
    receipt = lane_run.run_round(root, [lane], gate="echo broken >&2; exit 1")
    row = receipt["lanes"][0]
    assert row["merge_status"] == "merge_blocked" and row["merge_attempts"] == 1
    assert row["flake_evidence"]["failed_node_ids"] == []
    assert row["flake_evidence"]["isolated_rerun_passed"] is False


def test_gate_flake_is_retried_once_when_the_named_test_passes_in_isolation(tmp_path):
    root = _repo(tmp_path)
    _commit_script(root, "alpha", "alpha.txt", "alpha")
    lane = lane_run.Lane(lane_id="alpha", branch="feat/alpha",
                         worktree=root.parent / "wt-alpha",
                         command=(str(root.parent / "alpha.sh"),))
    state = root.parent / "gate-count"
    gate = ("n=$(cat %s 2>/dev/null || echo 0); echo $((n+1)) > %s; "
            "[ $n -ge 1 ] || { echo 'FAILED tests/test_x.py::test_y' >&2; exit 1; }"
            % (state, state))
    seen = []
    receipt = lane_run.run_round(root, [lane], gate=gate,
                                 rerun=lambda worktree, ids: seen.append(list(ids)) or (True, "1 passed"))
    row = receipt["lanes"][0]
    assert seen == [["tests/test_x.py::test_y"]]
    assert row["merge_status"] == "merged" and row["merge_attempts"] == 2
    assert row["flake_evidence"]["isolated_rerun_passed"] is True


@pytest.fixture
def rounds_path(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("PBATCH_DATA_DIR", str(runtime))
    return runtime / "lanes" / "rounds.jsonl"


@pytest.mark.parametrize("limit", [None, 0, 1, 3, 20, 40])
def test_read_rounds_returns_bounded_receipts_newest_last(tmp_path, rounds_path, limit):
    receipts = [{"at": 100 - index, "merged": [str(index)]} for index in range(30)]
    rounds_path.parent.mkdir(parents=True)
    original = "".join(json.dumps(row) + "\n" for row in receipts).encode("utf-8")
    rounds_path.write_bytes(original)
    modified = rounds_path.stat().st_mtime_ns
    if limit is None:
        actual = lane_run.read_rounds(tmp_path)
        limit = 20
    else:
        actual = lane_run.read_rounds(tmp_path, limit=limit)
    expected = receipts[-limit:] if limit else []
    assert actual == expected
    assert rounds_path.read_bytes() == original
    assert rounds_path.stat().st_mtime_ns == modified


def test_read_rounds_skips_malformed_lines_without_spending_the_limit(tmp_path, rounds_path):
    rounds_path.parent.mkdir(parents=True)
    original = (b'{"at": 1}\nnot-json\n\n[]\nnull\ntrue\n42\n"text"\n{broken\n\xff\n'
                + b"[" * 2000 + b"]" * 2000 + b'\n{"at": 2}\n{"at":')
    rounds_path.write_bytes(original)
    assert lane_run.read_rounds(tmp_path, limit=2) == [{"at": 1}, {"at": 2}]
    assert lane_run.read_rounds(tmp_path, limit=1) == [{"at": 2}]
    assert rounds_path.read_bytes() == original


def test_read_rounds_missing_does_not_create_a_ledger(tmp_path, rounds_path):
    assert lane_run.read_rounds(tmp_path) == []
    assert not rounds_path.parent.exists()


@pytest.mark.parametrize("raw", [b"", b"\n", b"bad JSON\n[]\nnull\n"])
def test_read_rounds_empty_or_malformed_ledger_returns_no_receipts(tmp_path, rounds_path, raw):
    rounds_path.parent.mkdir(parents=True)
    rounds_path.write_bytes(raw)
    assert lane_run.read_rounds(tmp_path) == []
    assert rounds_path.read_bytes() == raw


@pytest.mark.parametrize("limit", [-1, True, 1.5, None])
def test_read_rounds_rejects_invalid_limits_before_reading(tmp_path, rounds_path, limit):
    with pytest.raises(ValueError, match="limit"):
        lane_run.read_rounds(tmp_path, limit=limit)
    assert not rounds_path.parent.exists()


@pytest.mark.parametrize("aligned", [False, True])
def test_read_rounds_respects_tail_line_boundaries(tmp_path, rounds_path, monkeypatch, aligned):
    tail = b'{"at": 2}\n{"at": 3}\n'
    prefix = b'{"at": 1}\n' if aligned else b"not-a-receipt"
    rounds_path.parent.mkdir(parents=True)
    rounds_path.write_bytes(prefix + tail)
    monkeypatch.setattr(lane_run, "ROUND_READ_MAX_BYTES", len(tail))
    expected = [{"at": 2}, {"at": 3}] if aligned else [{"at": 3}]
    assert lane_run.read_rounds(tmp_path) == expected


def test_read_rounds_reads_only_a_bounded_tail(tmp_path, rounds_path, monkeypatch):
    maximum = lane_run.ROUND_READ_MAX_BYTES
    rounds_path.parent.mkdir(parents=True)
    rounds_path.write_bytes(b"x" * (maximum + 100) + b'\n{"at": 1}\n{"at": 2}\n')
    open_file, reads = Path.open, []

    def bounded_open(path, mode="r", *args, **kwargs):
        assert path == rounds_path and mode == "rb"
        handle = open_file(path, mode, *args, **kwargs)
        read = handle.read

        def bounded_read(size=-1):
            assert 0 <= size <= maximum
            reads.append((handle.tell(), size))
            return read(size)

        monkeypatch.setattr(handle, "read", bounded_read)
        return handle

    monkeypatch.setattr(Path, "open", bounded_open)
    assert lane_run.read_rounds(tmp_path) == [{"at": 1}, {"at": 2}]
    assert reads and all(offset > 0 for offset, _ in reads)
    assert sum(size for _, size in reads) <= maximum + 1


def test_a_flake_that_fails_in_isolation_is_not_retried(tmp_path):
    root = _repo(tmp_path)
    _commit_script(root, "alpha", "alpha.txt", "alpha")
    lane = lane_run.Lane(lane_id="alpha", branch="feat/alpha",
                         worktree=root.parent / "wt-alpha",
                         command=(str(root.parent / "alpha.sh"),))
    gate = "echo 'FAILED tests/test_x.py::test_y' >&2; exit 1"
    receipt = lane_run.run_round(root, [lane], gate=gate,
                                 rerun=lambda worktree, ids: (False, "1 failed"))
    row = receipt["lanes"][0]
    assert row["merge_status"] == "merge_blocked" and row["merge_attempts"] == 1
    assert row["flake_evidence"]["isolated_rerun_passed"] is False


def test_a_conflicting_merge_never_leaves_the_root_repository_conflicted(tmp_path):
    from pbatch import session_merge

    root = _repo(tmp_path)
    run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,
                                    capture_output=True, text=True)
    (root / "shared.txt").write_text("base\n", encoding="utf-8")
    run("add", "shared.txt"); run("commit", "-qm", "shared base")
    run("checkout", "-q", "-b", "feat/conflict")
    (root / "shared.txt").write_text("lane side\n", encoding="utf-8")
    run("commit", "-qam", "lane side")
    run("checkout", "-q", "main")
    (root / "shared.txt").write_text("main side\n", encoding="utf-8")
    run("commit", "-qam", "main side")

    merged, detail = session_merge._merge_session(root, "feat/conflict", False)

    assert merged is False
    assert "conflict" in detail.lower() or detail == ""
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                            capture_output=True, text=True).stdout
    assert status.strip() == "", "the root repository must be restored after a blocked merge"
    assert subprocess.run(["git", "-C", str(root), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                          capture_output=True).returncode != 0
    assert (root / "shared.txt").read_text(encoding="utf-8") == "main side\n"


def test_a_dirty_root_blocks_the_merge_instead_of_merging_on_it(tmp_path):
    root = _repo(tmp_path)
    _commit_script(root, "alpha", "alpha.txt", "alpha")
    lane = lane_run.Lane(lane_id="alpha", branch="feat/alpha",
                         worktree=root.parent / "wt-alpha",
                         command=(str(root.parent / "alpha.sh"),))
    original = lane_run._merge_ready
    def dirty_then_merge(rt, ready, blocked, gate, base, rerun=None):
        (rt / "base.txt").write_text("dirty before merge\n", encoding="utf-8")
        return original(rt, ready, blocked, gate, base, rerun)
    lane_run._merge_ready = dirty_then_merge
    try:
        receipt = lane_run.run_round(root, [lane], gate="true")
    finally:
        lane_run._merge_ready = original
    assert receipt["blocked"] == ["alpha"]
    assert "dirty" in receipt["lanes"][0]["reason"]
