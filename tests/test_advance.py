"""Tests for the self-driving iteration engine (pbatch/advance.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch.advance import (CheckerError, _run_checker, advance_main,
                            classify_violation, format_plan, record_round,
                            scan_project)


def _scan(target: str, findings: dict[str, int]) -> dict:
    dimensions = {}
    for dim_id, count in findings.items():
        priority = 0 if dim_id.startswith("fe_") else 1
        dimensions[dim_id] = {
            "count": count, "priority": priority, "label": dim_id,
            "fix": f"fix {dim_id}", "files": [f"src/{dim_id}.py"] if count else [],
            "samples": [f"{dim_id} finding"] if count else [],
        }
    return {"target": target, "files_scanned": 3, "dimensions": dimensions}


def test_classify_violation_dimensions():
    assert classify_violation("spacing: SizedBox(height: 10)") == "ui_spacing"
    assert classify_violation("color: Color(0xFF6366F1)") == "ui_color"
    assert classify_violation("style: style={{") == "ui_style"
    assert classify_violation("swallowed exception (empty catch)") == "fe_errors"
    assert classify_violation("N+1 request risk") == "fe_n1"
    assert classify_violation("nesting depth 20 > 8") == "fe_complexity"
    assert classify_violation("test file without assertions") == "be_errors"
    assert classify_violation("dangerous DDL") == "be_errors"
    assert classify_violation("single-implementation interface") == "be_architecture"
    assert classify_violation(
        "single-implementation interface",
        "check-frontend-quality.py") == "fe_complexity"
    assert classify_violation(
        "single-implementation interface",
        "check-backend-quality.py") == "be_architecture"


def test_scan_project_on_self(tmp_path, monkeypatch):
    """Scanning the tool's own repo: no lib/ frontend code, so UI/fe dims
    stay empty; any Python issues land in the backend dimensions."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pbatch.advance.STATE_DIR", tmp_path / "docs" / "advance")
    scan = scan_project(".")
    assert scan["target"] == "."
    dims = scan["dimensions"]
    for dim_id in ("ui_spacing", "ui_color", "ui_style", "fe_errors", "fe_n1",
                   "be_errors", "be_architecture", "knowledge",
                   "design_intelligence", "backend_experience"):
        assert dim_id in dims
        assert "count" in dims[dim_id]
        assert "priority" in dims[dim_id]


def test_scan_real_project_ui_clean(tmp_path, monkeypatch):
    """The scanner must work on a real external tree and report every
    dimension with countable numbers. Exact violation counts are EXTERNAL
    state (the console repo evolves independently — retro lesson D4:
    never assert external drift), so this test asserts structure, not
    cleanliness."""
    target = "/home/u1/workspace/demo/snaplink-console"
    if not __import__("pathlib").Path(target).exists():
        return  # environment without the demo project: skip
    scan = scan_project(target)
    dims = scan["dimensions"]
    for dim_id in ("ui_spacing", "ui_color", "ui_style", "fe_errors",
                   "fe_complexity", "fe_n1", "be_errors", "be_architecture"):
        assert dim_id in dims, dim_id
        assert isinstance(dims[dim_id]["count"], int), dim_id
        assert "files" in dims[dim_id], dim_id


def test_record_round_append(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path))
    scan = {"target": "x", "dimensions": {"ui_spacing": {"count": 3}}}
    record_round(scan, 1)
    record_round(scan, 2)
    lines = (tmp_path / "advance" / "state.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["round"] == 1
    assert json.loads(lines[1])["round"] == 2


def test_format_plan_empty_and_batches():
    empty = format_plan([], 0)
    assert "无需推进" in empty
    plan = format_plan([{"priority": "P0", "id": "fe_errors", "count": 2,
                         "label": "x", "fix": "y", "files": [], "samples": []}], 2)
    assert "[P0]" in plan and "x（2 项）" in plan


def test_advance_cli_json(capsys):
    sys.argv = ["pi-batch.py", "advance", "--dir", ".", "--json"]
    assert advance_main(sys.argv[2:]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "target" in payload and "batches" in payload
    assert "total" in payload


def test_scan_runs_every_registered_checker_and_preserves_source(monkeypatch):
    calls = []

    def fake_run(script, _target):
        name = __import__("pathlib").Path(script).name
        calls.append(name)
        detail = {
            "check-ui-spec.py": "spacing: 10px",
            "check-frontend-quality.py": "single-implementation interface",
            "check-backend-quality.py": "single-implementation interface",
            "check-knowledge-freshness.py": "unknown knowledge finding",
            "check-design-intelligence.py": "unknown design finding",
            "check-backend-experience.py": "unknown lifecycle finding",
        }[name]
        return {"files_scanned": 1,
                "violations": [{"file": "x", "detail": detail}]}

    monkeypatch.setattr("pbatch.advance._run_checker", fake_run)
    scan = scan_project(".")
    assert len(calls) == 6
    assert scan["dimensions"]["ui_spacing"]["count"] == 1
    assert scan["dimensions"]["fe_complexity"]["count"] == 1
    assert scan["dimensions"]["be_architecture"]["count"] == 1
    assert scan["dimensions"]["knowledge"]["count"] == 1
    assert scan["dimensions"]["design_intelligence"]["count"] == 1
    assert scan["dimensions"]["backend_experience"]["count"] == 1


def test_run_checker_fails_closed_on_execution_or_bad_json(tmp_path, monkeypatch):
    checker = tmp_path / "checker.py"
    checker.write_text("# test", encoding="utf-8")
    monkeypatch.setattr(
        "pbatch.advance.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=2, stdout="", stderr="usage error"))
    with pytest.raises(CheckerError, match="exited 2"):
        _run_checker(str(checker), ".")

    monkeypatch.setattr(
        "pbatch.advance.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="not-json", stderr=""))
    with pytest.raises(CheckerError, match="invalid JSON"):
        _run_checker(str(checker), ".")


def test_run_checker_accepts_only_known_trailing_summary(tmp_path, monkeypatch):
    checker = tmp_path / "check-frontend-quality.py"
    checker.write_text("# test", encoding="utf-8")
    report = {"files_scanned": 1, "violations": []}
    monkeypatch.setattr(
        "pbatch.advance.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(report) + "\nUI-QUALITY: OK (1 file(s) scanned)\n",
            stderr=""))
    assert _run_checker(str(checker), ".") == report

    monkeypatch.setattr(
        "pbatch.advance.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(report) + "\nCHECKER: OK\n", stderr=""))
    with pytest.raises(CheckerError, match="unexpected trailing data"):
        _run_checker(str(checker), ".")

    monkeypatch.setattr(
        "pbatch.advance.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(report) + "\nBACKEND-QUALITY: OK (1 file(s) scanned)\n",
            stderr=""))
    with pytest.raises(CheckerError, match="unexpected trailing data"):
        _run_checker(str(checker), ".")


def test_run_checker_rejects_exit_report_contradictions(tmp_path, monkeypatch):
    checker = tmp_path / "check-frontend-quality.py"
    checker.write_text("# test", encoding="utf-8")
    clean = {"files_scanned": 1, "violations": []}
    monkeypatch.setattr(
        "pbatch.advance.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout=json.dumps(clean), stderr=""))
    with pytest.raises(CheckerError, match="contradicts"):
        _run_checker(str(checker), ".")

    findings = {"files_scanned": 1,
                "violations": [{"file": "x", "detail": "spacing"}]}
    monkeypatch.setattr(
        "pbatch.advance.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(findings), stderr=""))
    with pytest.raises(CheckerError, match="contradicts"):
        _run_checker(str(checker), ".")


def test_advance_rejects_missing_target_directory(tmp_path):
    with pytest.raises(SystemExit) as exc:
        advance_main(["--dir", str(tmp_path / "missing")])
    assert exc.value.code == 2


def test_advance_rejects_symlinked_state_parent(tmp_path, monkeypatch):
    from pbatch import advance

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "docs").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(advance, "scan_project",
                        lambda target: _scan(target, {"fe_errors": 1}))
    state_file = tmp_path / "docs" / "advance" / "state.jsonl"
    assert advance_main(["--dir", str(tmp_path), "--max-rounds", "1",
                         "--state-file", str(state_file)]) == 2
    assert not (outside / "advance" / "state.jsonl").exists()


def test_recorded_rounds_rescan_and_stop_when_clean(tmp_path, monkeypatch, capsys):
    state_file = tmp_path / "state.jsonl"
    scans = [
        {"target": ".", "files_scanned": 1,
         "dimensions": {"x": {"count": 1, "priority": 0, "label": "x",
                               "fix": "x", "files": [], "samples": []}}},
        {"target": ".", "files_scanned": 1,
         "dimensions": {"x": {"count": 0, "priority": 0, "label": "x",
                               "fix": "x", "files": [], "samples": []}}},
    ]
    monkeypatch.setattr("pbatch.advance.scan_project", lambda _target: scans.pop(0))
    assert advance_main(["--max-rounds", "5", "--state-file", str(state_file),
                         "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rounds_run"] == 2
    assert payload["total"] == 0
    assert len(state_file.read_text().splitlines()) == 2


def test_default_scan_never_calls_agent_or_writes_state(tmp_path, monkeypatch, capsys):
    from pbatch import advance

    monkeypatch.setattr(advance, "scan_project",
                        lambda target: _scan(target, {"fe_errors": 1}))
    monkeypatch.setattr(advance, "_run_agent_batch",
                        lambda *_args: pytest.fail("default scan must stay read-only"))

    assert advance_main(["--dir", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execute"] is False
    assert payload["rounds_run"] == 0
    assert not (tmp_path / "docs" / "advance" / "state.jsonl").exists()


def test_execute_repairs_highest_priority_batch_and_records_before_after(
        tmp_path, monkeypatch, capsys):
    from pbatch import advance

    scans = [_scan(str(tmp_path), {"ui_color": 8, "fe_errors": 2}),
             _scan(str(tmp_path), {"ui_color": 0, "fe_errors": 0})]
    observed = {}
    monkeypatch.setattr(advance, "scan_project", lambda _target: scans.pop(0))

    def fake_agent(args, batch, round_no):
        observed.update({"args": args, "batch": batch, "round": round_no})
        return {"success": True, "status": "AGENT_PASSED", "returncode": 0,
                "reason": "", "elapsed": 0.1, "output": "report.md"}

    monkeypatch.setattr(advance, "_run_agent_batch", fake_agent)
    monkeypatch.setattr(
        advance, "_run_validators",
        lambda value, target: {"requested": bool(value), "success": True,
                               "results": [{"command": value, "target": str(target)}]})
    code = advance_main([
        "--dir", str(tmp_path), "--execute", "--max-rounds", "3", "--json",
        "--model", "m1", "--provider", "p1", "--timeout", "9",
        "--retries", "2", "--validate", "unit-gate",
    ])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 0
    assert observed["batch"]["id"] == "fe_errors"
    assert observed["round"] == 1
    assert (observed["args"].model, observed["args"].provider,
            observed["args"].timeout, observed["args"].retries) == ("m1", "p1", 9, 2)
    from pbatch import config
    state = config.data_dir(str(tmp_path)) / "advance" / "state.jsonl"
    event = json.loads(state.read_text(encoding="utf-8"))
    assert event["round"] == 1
    assert event["before"]["total"] == 10
    assert event["after"]["total"] == 0
    assert event["batch"]["id"] == "fe_errors"
    assert event["execution"]["validation"]["success"] is True


def test_execute_resumes_round_in_target_state_and_honors_override(
        tmp_path, monkeypatch):
    from pbatch import advance

    from pbatch import config
    default_state = config.data_dir(str(tmp_path)) / "advance" / "state.jsonl"
    default_state.parent.mkdir(parents=True)
    default_state.write_text('{"round": 4}\ncorrupt\n', encoding="utf-8")
    scans = [_scan(str(tmp_path), {"fe_errors": 1}),
             _scan(str(tmp_path), {"fe_errors": 0})]
    rounds = []
    monkeypatch.setattr(advance, "scan_project", lambda _target: scans.pop(0))
    monkeypatch.setattr(
        advance, "_run_agent_batch",
        lambda _args, _batch, round_no: rounds.append(round_no) or {
            "success": True, "status": "AGENT_PASSED", "returncode": 0,
            "reason": "", "elapsed": 0, "output": "report.md"})
    assert advance_main(["--dir", str(tmp_path), "--execute"]) == 0
    assert rounds == [5]
    assert json.loads(default_state.read_text().splitlines()[-1])["round"] == 5

    override = tmp_path / "custom" / "advance.jsonl"
    override.parent.mkdir()
    override.write_text('{"round": 9}\n', encoding="utf-8")
    scans.extend([_scan(str(tmp_path), {"fe_errors": 1}),
                  _scan(str(tmp_path), {"fe_errors": 0})])
    assert advance_main(["--dir", str(tmp_path), "--execute",
                         "--state-file", str(override)]) == 0
    assert rounds[-1] == 10
    assert json.loads(override.read_text().splitlines()[-1])["round"] == 10


@pytest.mark.parametrize("failure_kind", ["agent", "validator"])
def test_execute_failure_is_nonzero_records_result_and_stops(
        tmp_path, monkeypatch, failure_kind):
    from pbatch import advance

    scan = _scan(str(tmp_path), {"fe_errors": 1})
    calls = {"scan": 0, "agent": 0}

    def fake_scan(_target):
        calls["scan"] += 1
        return scan

    def fake_agent(*_args):
        calls["agent"] += 1
        success = failure_kind != "agent"
        return {"success": success,
                "status": "AGENT_PASSED" if success else "AGENT_FAILED",
                "returncode": 0 if success else 7, "reason": "agent failed",
                "elapsed": 0, "output": "report.md"}

    monkeypatch.setattr(advance, "scan_project", fake_scan)
    monkeypatch.setattr(advance, "_run_agent_batch", fake_agent)
    monkeypatch.setattr(
        advance, "_run_validators",
        lambda *_args: {"requested": True, "success": failure_kind != "validator",
                        "results": [], "reason": "validator failed"})
    code = advance_main(["--dir", str(tmp_path), "--execute", "--max-rounds", "5",
                         "--validate", "gate"])

    assert code == 1
    assert calls == {"scan": 2, "agent": 1}
    from pbatch import config
    state = config.data_dir(str(tmp_path)) / "advance" / "state.jsonl"
    events = [json.loads(line) for line in state.read_text().splitlines()]
    assert len(events) == 1
    expected = "AGENT_FAILED" if failure_kind == "agent" else "VALIDATION_FAILED"
    assert events[0]["execution"]["status"] == expected
    assert events[0]["before"] and events[0]["after"]


def test_execute_stops_no_progress_instead_of_spinning(tmp_path, monkeypatch):
    from pbatch import advance

    scan = _scan(str(tmp_path), {"fe_errors": 1})
    calls = []
    monkeypatch.setattr(advance, "scan_project", lambda _target: scan)
    monkeypatch.setattr(
        advance, "_run_agent_batch",
        lambda *_args: calls.append("agent") or {
            "success": True, "status": "AGENT_PASSED", "returncode": 0,
            "reason": "", "elapsed": 0, "output": "report.md"})

    assert advance_main(["--dir", str(tmp_path), "--execute",
                         "--max-rounds", "9"]) == 1
    assert calls == ["agent"]
    from pbatch import config
    state = config.data_dir(str(tmp_path)) / "advance" / "state.jsonl"
    event = json.loads(state.read_text())
    assert event["execution"]["status"] == "NO_PROGRESS"


def test_agent_task_receives_execution_options_and_restores_global(tmp_path, monkeypatch):
    from pbatch import advance, config

    captured = {}
    monkeypatch.setattr(config, "AGENT_BIN", "original-agent")
    monkeypatch.setattr(config, "agent_preflight_errors", lambda _agent: [])

    def fake_serial(tasks, retries):
        captured.update({"task": tasks[0], "retries": retries,
                         "active_agent": config.AGENT_BIN})
        return [SimpleNamespace(success=True, returncode=0, reason="", elapsed=0.2)]

    monkeypatch.setattr(advance, "run_serial", fake_serial)
    args = SimpleNamespace(
        dir=str(tmp_path), agent_bin="mock-agent", model="model-x",
        provider="provider-y", timeout=17, retries=3)
    result = advance._run_agent_batch(
        args, {"id": "fe_errors", "label": "errors", "priority": "P0",
               "count": 1, "fix": "fix", "files": [], "samples": []}, 6)

    task = captured["task"]
    assert result["success"] is True
    assert captured["active_agent"] == "mock-agent"
    assert captured["retries"] == 3
    assert (task.model, task.provider, task.timeout, task.cwd) == (
        "model-x", "provider-y", 17, str(tmp_path.resolve()))
    assert config.AGENT_BIN == "original-agent"


def test_execute_with_mock_agent_really_changes_target(tmp_path, monkeypatch, capsys):
    from pbatch import advance

    agent = tmp_path / "mock-agent.py"
    agent.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "Path('fixed.txt').write_text('fixed\\n', encoding='utf-8')\n"
        "print('repair complete')\n",
        encoding="utf-8")
    agent.chmod(0o755)

    def marker_scan(target):
        count = 0 if (Path(target) / "fixed.txt").exists() else 1
        return _scan(target, {"fe_errors": count})

    monkeypatch.setattr(advance, "scan_project", marker_scan)
    code = advance_main(["--dir", str(tmp_path), "--execute",
                         "--agent-bin", str(agent), "--json"])

    assert code == 0
    assert (tmp_path / "fixed.txt").read_text(encoding="utf-8") == "fixed\n"
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution"]["status"] == "AGENT_PASSED"
    assert Path(payload["execution"]["output"]).is_file()


def test_validator_command_failure_is_fail_closed(tmp_path):
    from pbatch import advance

    result = advance._run_validators("exit 7", Path(tmp_path))
    assert result["requested"] is True
    assert result["success"] is False
    assert result["results"][0]["exit"] == 7
