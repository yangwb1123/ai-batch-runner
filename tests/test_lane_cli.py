"""`pi-batch lanes` CLI: plan validation, dry run and one gated round."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pbatch import lane_cli, lane_loop, lane_run


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


def _plan(root: Path, lanes: list, gate: str = "test -f lane.txt") -> Path:
    path = root.parent / "plan.json"
    path.write_text(json.dumps({"gate": gate, "lanes": lanes}), encoding="utf-8")
    return path


def _lane(root: Path, lane_id: str) -> dict:
    script = root.parent / (lane_id + ".sh")
    script.write_text("#!/bin/sh\nset -e\nprintf 'x\\n' > lane-%s.txt\n"
                      "git add lane-%s.txt\n"
                      "git -c user.email=t@t -c user.name=t commit -qm %s\n"
                      % (lane_id, lane_id, lane_id), encoding="utf-8")
    script.chmod(0o755)
    return {"id": lane_id, "branch": "feat/" + lane_id,
            "worktree": str(root.parent / ("wt-" + lane_id)),
            "command": [str(script)]}


@pytest.fixture(autouse=True)
def _runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))


def test_dry_run_creates_nothing_and_prints_resolved_lanes(tmp_path, capsys):
    root = _repo(tmp_path)
    plan = _plan(root, [_lane(root, "a"), _lane(root, "b")])
    code = lane_cli.lanes_main(["run", "--plan", str(plan), "--root", str(root),
                                "--hours", "12", "--invocations", "10", "--dry-run"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["window"]["invocation_budget"] == 10
    assert [lane["id"] for lane in payload["lanes"]] == ["a", "b"]
    assert not (root.parent / "wt-a").exists()


def test_first_run_requires_a_declared_window(tmp_path, capsys):
    root = _repo(tmp_path)
    plan = _plan(root, [_lane(root, "a")])
    assert lane_cli.lanes_main(["run", "--plan", str(plan), "--root", str(root)]) == 2
    assert "requires --hours and --invocations" in capsys.readouterr().out


def test_one_round_merges_through_the_gate_and_records_a_decision(tmp_path, capsys):
    root = _repo(tmp_path)
    plan = _plan(root, [_lane(root, "a")], gate="test -f lane-a.txt")
    code = lane_cli.lanes_main(["run", "--plan", str(plan), "--root", str(root),
                                "--hours", "1", "--invocations", "4", "--json"])
    assert code == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[-1]["receipt"]["merged"] == ["a"]
    assert lines[-1]["decision"] == "finish"
    tracked = subprocess.run(["git", "-C", str(root), "ls-tree", "--name-only", "HEAD"],
                             capture_output=True, text=True).stdout.split()
    assert "lane-a.txt" in tracked


def test_restart_reuses_the_anchored_window_instead_of_extending_it(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, [_lane(root, "a")], gate="test -f lane-a.txt")
    lane_cli.lanes_main(["run", "--plan", str(plan), "--root", str(root),
                         "--hours", "1", "--invocations", "4", "--dry-run"])
    first = lane_loop.status(root)
    lane_cli.lanes_main(["run", "--plan", str(plan), "--root", str(root),
                         "--hours", "12", "--invocations", "4", "--dry-run"])
    second = lane_loop.status(root)
    assert first["deadline_at"] == second["deadline_at"]


@pytest.mark.parametrize("as_json", [False, True])
def test_status_is_read_only(tmp_path, capsys, as_json):
    root = _repo(tmp_path)
    args = ["status", "--root", str(root)] + (["--json"] if as_json else [])
    assert lane_cli.lanes_main(args) == 0
    output = capsys.readouterr().out
    if as_json:
        payload = json.loads(output)
        assert payload["anchored"] is False
        assert payload["last_round"] is None
    else:
        assert "anchored: False" in output
        assert "last_round: null" in output
    assert not lane_loop.ledger_path(root).exists()
    assert not lane_loop.ledger_path(root).parent.exists()


@pytest.mark.parametrize("as_json", [False, True])
def test_status_includes_last_round_without_writing_ledgers(tmp_path, capsys, monkeypatch, as_json):
    root = _repo(tmp_path)
    lane_loop.start_window(root, hours=1, invocations=4)
    path = lane_run._append_receipt(root, {"at": 200, "merged": ["old"], "head": "old"})
    expected = {"at": 100, "merged": ["alpha"], "blocked": ["beta"], "head": "abc123"}
    lane_run._append_receipt(root, dict(expected, gate="private gate", lanes=[{"lane": "alpha"}]))
    with path.open("ab") as handle:
        handle.write(b"not-json\n[]\nnull\n\xff\n{\"at\":")
    ledgers = (path, lane_loop.ledger_path(root))
    before = {item: (item.read_bytes(), item.stat().st_mtime_ns) for item in ledgers}
    open_file = Path.open

    def read_only(path, mode="r", *args, **kwargs):
        assert mode in ("r", "rb"), "status must not open a ledger for writing"
        return open_file(path, mode, *args, **kwargs)

    args = ["status", "--root", str(root)] + (["--json"] if as_json else [])
    with monkeypatch.context() as guard:
        guard.setattr(Path, "open", read_only)
        assert lane_cli.lanes_main(args) == 0
    output = capsys.readouterr().out
    summary = (json.loads(output)["last_round"] if as_json
               else json.loads(output.split("last_round: ", 1)[1]))
    assert summary == expected
    assert {item: (item.read_bytes(), item.stat().st_mtime_ns) for item in ledgers} == before
    assert set(path.parent.iterdir()) == set(ledgers)


def test_a_blocked_merge_stops_with_a_nonzero_exit(tmp_path, capsys):
    root = _repo(tmp_path)
    plan = root.parent / "plan.json"
    lane = _lane(root, "a")
    plan.write_text(json.dumps({"gate": "exit 1", "lanes": [lane]}), encoding="utf-8")
    code = lane_cli.lanes_main(["run", "--plan", str(plan), "--root", str(root),
                                "--hours", "1", "--invocations", "4", "--json"])
    assert code == 4
    payload = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert payload[-1]["decision"] == "stop"
    assert lane_loop.status(root)["stopped"].startswith("round gate_failed")


@pytest.mark.parametrize("plan_body", [
    "not json",
    json.dumps({"lanes": []}),
    json.dumps({"gate": "true", "lanes": [{"id": "a", "branch": "b",
                                           "worktree": "/tmp/x", "command": [],
                                           "unexpected": 1}]}),
])
def test_invalid_plans_fail_closed_without_touching_the_repository(tmp_path, plan_body, capsys):
    root = _repo(tmp_path)
    plan = root.parent / "plan.json"
    plan.write_text(plan_body, encoding="utf-8")
    assert lane_cli.lanes_main(["run", "--plan", str(plan), "--root", str(root),
                                "--hours", "1", "--invocations", "1"]) == 2
    assert not lane_loop.ledger_path(root).exists()
    assert "lanes:" in capsys.readouterr().out


def test_a_failed_lane_still_consumes_the_invocation_budget(tmp_path, capsys):
    root = _repo(tmp_path)
    lane = _lane(root, "a")
    lane["command"] = ["/bin/sh", "-c", "exit 7"]
    plan = _plan(root, [lane], gate="true")
    lane_cli.lanes_main(["run", "--plan", str(plan), "--root", str(root),
                         "--hours", "1", "--invocations", "2", "--json"])
    status = lane_loop.status(root)
    assert status["consumed"] == 1, "a lane that ran and failed still spent budget"
    assert status["stopped"].startswith("round gate_failed")


def test_resume_lifts_only_a_resumable_stop(tmp_path, capsys):
    root = _repo(tmp_path)
    lane_loop.start_window(root, hours=1, invocations=2, now=1000.0)
    lane_loop.stop(root, "backlog exhausted", now=1100.0)
    assert lane_cli.lanes_main(["resume", "--root", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["resumed"] == "backlog exhausted"
    lane_loop.stop(root, "deadline reached", now=1200.0)
    assert lane_cli.lanes_main(["resume", "--root", str(root)]) == 2
    assert "not resumable" in capsys.readouterr().out


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
