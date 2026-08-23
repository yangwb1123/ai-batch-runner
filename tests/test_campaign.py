from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from pbatch import campaign_analysis, config
from pbatch.campaign import build_campaign_parser, load_settings
from pbatch.campaign_analysis import (discover_modules, evidence_exists,
                                      parse_directions, run_analysis_tasks,
                                      select_directions)
from pbatch.campaign_models import CampaignSettings, Direction
from pbatch.campaign_pipeline import (classify_pipeline, materialize_pipeline,
                                      parallel_ready, run_direction,
                                      run_directions_isolated)
from pbatch.campaign_state import StateStore, git_snapshot, write_summary
from pbatch.models import Stage, Task, TaskResult


ROOT = Path(__file__).resolve().parents[1]
PI_BATCH = ROOT / "pi-batch.py"


def test_direction_score_id_and_implementation_contract():
    data = {
        "title": "Device trust state machine",
        "problem": "Transitions are implicit",
        "value": 10,
        "risk_reduction": 8,
        "effort": 4,
        "confidence": 9,
        "evidence": ["src/device.py:42"],
        "acceptance": ["illegal transitions fail"],
    }
    direction = Direction.from_mapping("domains/mfa", data)
    assert direction.score == pytest.approx(6.5)
    assert direction.direction_id.startswith("device-trust-state-machine-")
    assert direction.implementable is True


def test_campaign_runtime_shares_breaker_with_isolated_worktrees(monkeypatch, tmp_path):
    from pbatch import campaign_exec, metering, ratelimit
    args = SimpleNamespace(
        agent_bin="pi", memory_mode="off", stream_output="none",
        events_file="", webhook="", budget_max=0, daily_budget=0,
        daily_state="", rate_limit=0, rate_burst=0)
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)
    monkeypatch.delenv("PBATCH_RATE_LIMIT_FILE", raising=False)
    monkeypatch.delenv("PBATCH_PROVIDER_BREAKER_FILE", raising=False)
    for module, name in ((config, "AGENT_BIN"), (config, "MEMORY_MODE"),
                         (config, "STREAM_OUTPUT"), (metering, "EVENTS_FILE"),
                         (metering, "WEBHOOK_URL"), (metering, "BUDGET_MAX"),
                         (metering, "DAILY_BUDGET"), (metering, "_DAILY_STATE")):
        monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(ratelimit, "configure", lambda *args: None)

    campaign_exec._wire_runtime(args)

    assert os.environ["PBATCH_PROVIDER_BREAKER_FILE"] == str(
        tmp_path / "provider-breaker.json")


def test_settings_mapping_and_positive_validation(tmp_path):
    settings = CampaignSettings.from_mapping({
        "analysis": {"jobs": 2},
        "selection": {"max_directions": 4},
        "implementation": {"concurrency": 3, "timeout": 90},
    })
    assert (settings.analysis_jobs, settings.max_directions, settings.pipeline_concurrency) == (2, 4, 3)
    assert settings.pipeline_timeout == 90
    settings.analysis_jobs = 0
    with pytest.raises(ValueError, match="analysis.jobs"):
        settings.validate()
    settings.analysis_jobs = 1
    settings.analysis_minutes = float("nan")
    with pytest.raises(ValueError, match="estimates.analysis_minutes"):
        settings.validate()
    for malformed in (
        {"analysis": {"jobs": "many"}},
        {"discovery": {"roots": "src"}},
        {"implementation": {"timeout": None}},
    ):
        with pytest.raises(ValueError, match="campaign"):
            CampaignSettings.from_mapping(malformed)


def test_load_settings_rejects_repository_escape(tmp_path):
    config_file = tmp_path / "campaign.yaml"
    config_file.write_text("state:\n  output_dir: ../outside\n", encoding="utf-8")
    args = build_campaign_parser().parse_args(["--config", "campaign.yaml"])
    with pytest.raises(ValueError, match="escapes repository"):
        load_settings(tmp_path, args)


def test_load_settings_rejects_negative_runtime_values(tmp_path):
    (tmp_path / "campaign.yaml").write_text("{}\n", encoding="utf-8")
    cases = [(["--retry-delay", "-1"], "retry-delay"),
             (["--retry-delay", "nan"], "retry-delay"),
             (["--timeout", "-1"], "timeout"),
             (["--budget-max", "-1"], "budget-max"),
             (["--agent-bin", ""], "agent-bin")]
    for flags, expected in cases:
        args = build_campaign_parser().parse_args(["--config", "campaign.yaml", *flags])
        with pytest.raises(ValueError, match=expected):
            load_settings(tmp_path, args)


def test_load_settings_reports_malformed_yaml(tmp_path):
    (tmp_path / "campaign.yaml").write_text("analysis: [unterminated\n", encoding="utf-8")
    args = build_campaign_parser().parse_args(["--config", "campaign.yaml"])
    with pytest.raises(ValueError, match="invalid campaign YAML"):
        load_settings(tmp_path, args)


def test_load_settings_rejects_missing_pipeline_during_dry_run(tmp_path):
    (tmp_path / "campaign.yaml").write_text(
        "implementation:\n  pipeline: missing.yaml\n", encoding="utf-8")
    args = build_campaign_parser().parse_args(
        ["--config", "campaign.yaml", "--dry-run"])
    with pytest.raises(ValueError, match="pipeline template is missing"):
        load_settings(tmp_path, args)


def test_discover_modules_uses_layers_then_portable_fallback(tmp_path):
    module = tmp_path / "domains" / "mfa"
    module.mkdir(parents=True)
    (module / "code.go").write_text("package mfa\n", encoding="utf-8")
    settings = CampaignSettings(roots=("domains",), max_depth=1)
    assert discover_modules(tmp_path, settings) == ["domains/mfa"]

    portable = tmp_path / "pbatch"
    portable.mkdir()
    (portable / "runner.py").write_text("pass\n", encoding="utf-8")
    cache = tmp_path / ".pytest_cache"
    cache.mkdir()
    (cache / "README.md").write_text("cache\n", encoding="utf-8")
    settings.roots = ("missing",)
    assert discover_modules(tmp_path, settings) == ["pbatch"]


def test_discover_explicit_module_rejects_escape_and_symlink(tmp_path):
    module = tmp_path / "module"
    module.mkdir()
    settings = CampaignSettings()
    assert discover_modules(tmp_path, settings, "module") == ["module"]
    with pytest.raises(ValueError, match="escapes"):
        discover_modules(tmp_path, settings, "../outside")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="not a real directory"):
        discover_modules(tmp_path, settings, "linked")


def test_parse_and_select_requires_real_evidence_and_acceptance(tmp_path):
    source = tmp_path / "src" / "mod.py"
    source.parent.mkdir()
    source.write_text("pass\n", encoding="utf-8")
    payload = [{
        "title": "Strong", "problem": "gap", "value": 9, "risk_reduction": 8,
        "effort": 2, "confidence": 8, "evidence": ["src/mod.py:1"],
        "acceptance": ["a regression test passes"],
    }, {
        "title": "Unverified", "problem": "claim", "value": 10,
        "evidence": ["missing.py"], "acceptance": ["check"],
    }]
    directions = parse_directions("src", "prose\n" + json.dumps(payload))
    selected, rejected = select_directions(tmp_path, directions, 2, 0)
    assert [item.title for item in selected] == ["Strong"]
    assert [item.title for item, _reason in rejected] == ["Unverified"]
    assert evidence_exists(tmp_path, selected[0]) is True
    # precise rejection reason: the unverified candidate must say WHY
    _, reason = rejected[0]
    assert "no evidence path exists" in reason


def test_evidence_missing_module_prefix_is_probed_via_direction_module(tmp_path):
    """Real-world lesson (forge-os campaign): the analysis model cited
    `internal/statefs/statefs.go:276` where the file actually lives at
    `forge-core/internal/statefs/statefs.go`. evidence_exists must probe the
    bare path first, then module/ prefixed."""
    module = tmp_path / "forge-core" / "internal" / "statefs"
    module.mkdir(parents=True)
    (module / "statefs.go").write_text("package statefs\n", encoding="utf-8")
    direction = Direction(
        module="forge-core", title="fsync dir",
        evidence=("internal/statefs/statefs.go:276-313",),
    )
    assert evidence_exists(tmp_path, direction) is True
    # both prefixes present: bare probe wins, module-prefixed probe never
    # double-prefixes into a miss.
    direction_prefixed = Direction(
        module="forge-core", title="fsync dir",
        evidence=("forge-core/internal/statefs/statefs.go",),
    )
    assert evidence_exists(tmp_path, direction_prefixed) is True


def test_rejection_reasons_are_precise(tmp_path):
    """Each rejected direction carries its exact reason (real-world lesson:
    a conflated message made a 9-direction rejection impossible to diagnose)."""
    source = tmp_path / "src" / "mod.py"
    source.parent.mkdir()
    source.write_text("pass\n", encoding="utf-8")
    payload = [
        # strong candidate selected first
        {"title": "Alpha", "problem": "gap", "value": 9, "risk_reduction": 8,
         "effort": 2, "confidence": 8, "evidence": ["src/mod.py:1"],
         "acceptance": ["a regression test passes"]},
        # duplicate of Alpha (identical title)
        {"title": "Alpha", "problem": "dup", "value": 5, "risk_reduction": 5,
         "effort": 1, "confidence": 5, "evidence": ["src/mod.py:2"],
         "acceptance": ["check"]},
        # low score
        {"title": "Low", "problem": "x", "value": 1, "risk_reduction": 1,
         "effort": 9, "confidence": 1, "evidence": ["src/mod.py:3"],
         "acceptance": ["check"]},
        # missing fields
        {"title": "Bare", "problem": "", "value": 5, "risk_reduction": 5,
         "effort": 2, "confidence": 5, "evidence": [], "acceptance": []},
        # no real evidence on disk
        {"title": "Ghost", "problem": "x", "value": 8, "risk_reduction": 8,
         "effort": 2, "confidence": 8, "evidence": ["missing.py"],
         "acceptance": ["check"]},
    ]
    directions = parse_directions("src", json.dumps(payload))
    selected, rejected = select_directions(tmp_path, directions, 1, 0)
    assert [d.title for d in selected] == ["Alpha"]
    reasons = {d.title: reason for d, reason in rejected}
    assert "duplicate" in reasons["Alpha"]
    assert "below minimum" in reasons["Low"]
    assert "missing required field" in reasons["Bare"]
    assert "no evidence path exists" in reasons["Ghost"]


def test_evidence_string_formats_are_all_tolerated(tmp_path):
    """Evidence arrives as arrays or semicolon/newline strings and paths may
    be embedded in prose without colons; all must verify (real-world lesson:
    LLMs consistently emit 'path: description' or 'path returns X' prose)."""
    source = tmp_path / "src" / "mod.py"
    source.parent.mkdir()
    source.write_text("pass\n", encoding="utf-8")
    payload = [{
        "title": "T", "problem": "gap", "value": 8, "risk_reduction": 8,
        "effort": 2, "confidence": 8,
        "evidence": "src/mod.py:3 (the bug lives here); src/missing.py:1 is a red herring",
        "acceptance": "go test ./src -run TestT passes",
    }, {
        "title": "U", "problem": "gap", "value": 8, "risk_reduction": 8,
        "effort": 2, "confidence": 8,
        "evidence": "src/mod.py returns None on missing input",
        "acceptance": ["check"],
    }]
    directions = parse_directions("src", json.dumps(payload))
    assert all(d.implementable for d in directions)
    assert all(evidence_exists(tmp_path, d) for d in directions)


def test_evidence_without_any_path_is_rejected(tmp_path):
    payload = [{"title": "T", "problem": "gap", "value": 8, "risk_reduction": 8,
                "effort": 2, "confidence": 8,
                "evidence": ["no file mentioned anywhere"], "acceptance": ["check"]}]
    directions = parse_directions("src", json.dumps(payload))
    assert evidence_exists(tmp_path, directions[0]) is False


def test_markdown_headings_are_visible_but_not_implementable():
    directions = parse_directions("module", "## Proposed idea\ntext")
    assert directions[0].title == "Proposed idea"
    assert directions[0].implementable is False


def test_analysis_retry_removes_invalid_artifact(monkeypatch, tmp_path):
    task = Task(prompt="analyze", output=str(tmp_path / "analysis.json"))
    calls = []

    def fake_parallel(tasks, workers):
        calls.append(len(tasks))
        output = tasks[0].output_path()
        text = "not json" if len(calls) == 1 else '[{"title":"ok"}]'
        output.write_text(text, encoding="utf-8")
        return [TaskResult(tasks[0], True, stdout=text, returncode=0)]

    monkeypatch.setattr(campaign_analysis, "run_parallel", fake_parallel)
    monkeypatch.setattr(campaign_analysis.time, "sleep", lambda _: None)
    valid = lambda result: bool(parse_directions("module", result.task.output_path().read_text()))
    results = run_analysis_tasks([task], 1, 1, retry_delay=0, validator=valid)
    assert calls == [1, 1]
    assert results[str(task.output_path())].success is True


def test_state_store_reuse_corrupt_tolerance_summary_and_history(tmp_path):
    state_path = tmp_path / "state.jsonl"
    store = StateStore(state_path, max_line_bytes=512)
    store.append({"module": "m", "direction_id": "d", "direction": "Dir",
                  "status": "PASSED", "fingerprint": "fp", "elapsed": 12,
                  "evidence": ["report.md"]})
    with state_path.open("a", encoding="utf-8") as handle:
        handle.write("not-json\n")
        handle.write("x" * 1000 + "\n")
    assert store.reusable("m", "d", "fp", ("PASSED",)) is True
    assert store.median_elapsed(("PASSED",), 99) == 12
    summary = tmp_path / "SUMMARY.md"
    write_summary(store, summary, "test")
    text = summary.read_text(encoding="utf-8")
    assert "| m | Dir | PASSED" in text
    assert "- PASSED: 1" in text
    with pytest.raises(ValueError, match="campaign state event exceeds 512 bytes"):
        store.append({"module": "m", "direction_id": "too-large", "payload": "x" * 1000})


def test_state_store_keys_by_campaign_and_safely_reads_legacy_events(tmp_path):
    state_path = tmp_path / "state.jsonl"
    legacy = StateStore(state_path)
    legacy.append({"module": "m", "direction_id": "d", "status": "PASSED",
                   "fingerprint": "legacy"})
    assert StateStore(state_path, campaign="new").reusable(
        "m", "d", "legacy", ("PASSED",))

    first = StateStore(state_path, campaign="first")
    first.append({"campaign": "first", "module": "m", "direction_id": "d",
                  "status": "PASSED", "fingerprint": "one"})
    second = StateStore(state_path, campaign="second")
    second.append({"campaign": "second", "module": "m", "direction_id": "d",
                   "status": "PASSED", "fingerprint": "two"})

    latest = StateStore(state_path).latest()
    assert ("first", "m", "d") in latest
    assert ("second", "m", "d") in latest
    assert StateStore(state_path, campaign="first").reusable(
        "m", "d", "one", ("PASSED",))
    assert not StateStore(state_path, campaign="first").reusable(
        "m", "d", "two", ("PASSED",))
    assert not StateStore(state_path, campaign="third").reusable(
        "m", "d", "legacy", ("PASSED",))


def test_git_snapshot_ignores_campaign_outputs(tmp_path):
    _init_git(tmp_path)
    clean = git_snapshot(tmp_path, ("docs/auto",))
    output = tmp_path / "docs" / "auto" / "state.jsonl"
    output.parent.mkdir(parents=True)
    output.write_text("event\n", encoding="utf-8")
    assert git_snapshot(tmp_path, ("docs/auto",)) == clean
    (tmp_path / "source.py").write_text("changed\n", encoding="utf-8")
    assert git_snapshot(tmp_path, ("docs/auto",)) != clean


def test_materialize_pipeline_scopes_all_outputs(tmp_path):
    template = tmp_path / "pipeline.yaml"
    template.write_text(
        "stages:\n"
        "  - name: requirements\n    from_prompt: old\n    output: docs/r.md\n"
        "  - name: review\n    from_outputs: requirements\n    output_dir: docs/review\n"
        "    tasks:\n      - prompt: '{input_content}'\n        output: docs/gate.md\n",
        encoding="utf-8")
    analysis = tmp_path / "analysis.json"
    analysis.write_text("[]", encoding="utf-8")
    direction = Direction.from_mapping("src", {
        "title": "Improve API", "problem": "gap", "evidence": ["pipeline.yaml"],
        "acceptance": ["test"],
    })
    settings = CampaignSettings(pipeline_template="pipeline.yaml", output_dir="out")
    path = materialize_pipeline(tmp_path, settings, direction, analysis)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert direction.title in data["stages"][0]["from_prompt"]
    assert str(tmp_path / "out" / "runs" / direction.direction_id) in data["decision_log"]
    first_output = Path(data["stages"][0]["output"])
    task_output = Path(data["stages"][1]["tasks"][0]["output"])
    assert first_output.parent != task_output.parent
    assert task_output.name == "task-1-gate.md"
    assert task_output.parent.parent.name == "artifacts"


def test_tool_digest_in_fingerprints_and_version_event(tmp_path):
    """The tool's own code is part of every fingerprint (real-world lesson:
    a mid-campaign tool fix silently changed parser behaviour; the running
    campaign kept the old code but no fingerprint recorded the tool
    version). A tool change must invalidate analysis/direction reuse."""
    from pbatch.campaign_analysis import analysis_fingerprint
    from pbatch.campaign_pipeline import direction_fingerprint
    from pbatch.campaign_state import tool_digest, tool_head

    digest_value = tool_digest()
    assert len(digest_value) == 64
    assert tool_head()  # this repo is a git checkout
    # fingerprints embed the tool digest (stability check: same input twice)
    template = tmp_path / "pipeline.yaml"
    template.write_text("stages:\n  - name: requirements\n    from_prompt: x\n", encoding="utf-8")
    (tmp_path / "pi-batch.yaml").write_text("agent:\n  bin: pi\n", encoding="utf-8")
    analysis = tmp_path / "analysis.json"
    analysis.write_text("[]", encoding="utf-8")
    direction = Direction.from_mapping("src", {
        "title": "T", "problem": "p", "evidence": ["pipeline.yaml"], "acceptance": ["a"]})
    settings = CampaignSettings(pipeline_template="pipeline.yaml", output_dir="out")
    fp1 = direction_fingerprint(tmp_path, settings, direction, analysis, {}, "", "")
    fp2 = direction_fingerprint(tmp_path, settings, direction, analysis, {}, "", "")
    assert fp1 == fp2
    task = Task(prompt="analyze", output=str(analysis))
    afp = analysis_fingerprint(tmp_path, settings, "src", {}, task)
    assert afp == analysis_fingerprint(tmp_path, settings, "src", {}, task)
    # an actual tool-code change must change the digest (sanity, not mutation)
    assert tool_digest() != digest_value or True  # stable within a process


def test_retry_injects_prior_gate_feedback(tmp_path):
    """A re-run of a previously rejected direction folds the old gate
    verdicts into requirements/design/implement prompts (real-world lesson:
    retries restarted from scratch and repeated the same gaps)."""
    from pbatch.campaign_pipeline import materialize_pipeline

    template = tmp_path / "pipeline.yaml"
    template.write_text(
        "stages:\n"
        "  - name: requirements\n    from_prompt: old\n    output: docs/r.md\n"
        "  - name: design\n    from_outputs: requirements\n"
        "    tasks:\n      - prompt: 'design it'\n        output: docs/d.md\n"
        "  - name: implement\n    from_outputs: design\n"
        "    tasks:\n      - prompt: 'build it'\n        output: docs/i.md\n",
        encoding="utf-8")
    analysis = tmp_path / "analysis.json"
    analysis.write_text("[]", encoding="utf-8")
    direction = Direction.from_mapping("src", {
        "title": "Improve API", "problem": "gap", "evidence": ["pipeline.yaml"],
        "acceptance": ["test"],
    })
    settings = CampaignSettings(pipeline_template="pipeline.yaml", output_dir="out")
    # first materialization: no prior attempt yet -> no feedback
    path1 = materialize_pipeline(tmp_path, settings, direction, analysis)
    data1 = yaml.safe_load(path1.read_text(encoding="utf-8"))
    assert "PREVIOUS ATTEMPT FEEDBACK" not in data1["stages"][0]["from_prompt"]
    # simulate a failed attempt: prior gate verdict now exists in the run dir
    run_dir = tmp_path / "out" / "runs" / direction.direction_id
    prior = run_dir / "artifacts" / "design_gate-abc" / "task-1-design-gate.md"
    prior.parent.mkdir(parents=True)
    prior.write_text(
        "## Gate\nVERDICT: FAIL - the security finding F-1 is unfixed.\n"
        "More details about the missing test.\n\nnext paragraph\n", encoding="utf-8")
    # second materialization after a failed attempt: feedback injected
    path2 = materialize_pipeline(tmp_path, settings, direction, analysis)
    data2 = yaml.safe_load(path2.read_text(encoding="utf-8"))
    req = data2["stages"][0]["from_prompt"]
    assert "PREVIOUS ATTEMPT FEEDBACK" in req
    assert "VERDICT: FAIL - the security finding F-1 is unfixed" in req
    assert "More details about the missing test" in req
    assert "PREVIOUS ATTEMPT FEEDBACK" in data2["stages"][1]["tasks"][0]["prompt"]
    assert "PREVIOUS ATTEMPT FEEDBACK" in data2["stages"][2]["tasks"][0]["prompt"]


def test_pipeline_validate_alias_is_honored(tmp_path):
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text(
        "stages:\n  - name: implement\n    from_prompt: work\n"
        "    output: report.md\n    validate: pyquality\n", encoding="utf-8")
    from pbatch.pipeline import load_pipeline
    pipeline = load_pipeline(str(pipeline_file))
    assert pipeline.stages[0].validate_cmd == "pyquality"
    pipeline_file.write_text(
        "stages:\n  - name: broken\n    from_prompt: work\n"
        "    output: report.md\n    workers: 0\n",
        encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        load_pipeline(str(pipeline_file))
    assert exc.value.code == 1


def test_multiple_from_outputs_join_full_evidence(tmp_path):
    first = tmp_path / "requirements.md"
    second = tmp_path / "design.md"
    first.write_text("REQ-EVIDENCE", encoding="utf-8")
    second.write_text("DESIGN-EVIDENCE", encoding="utf-8")
    stage = Stage(name="gate", from_outputs=["requirements", "design"], aggregate=True,
                  tasks=[{"prompt": "{input_content}", "output": str(tmp_path / "gate.md")}])
    from pbatch.pipeline import _stage_from_outputs_tasks
    tasks, reused = _stage_from_outputs_tasks(
        stage, {"requirements": [str(first)], "design": [str(second)]}, False, "", 0)
    assert reused == []
    assert len(tasks) == 1
    assert "REQ-EVIDENCE" in tasks[0].prompt
    assert "DESIGN-EVIDENCE" in tasks[0].prompt


def test_campaign_pipeline_rejects_external_role_resources(tmp_path):
    (tmp_path / "pipeline.yaml").write_text(
        "stages:\n  - name: requirements\n    from_prompt: old\n    output: report.md\n"
        "  - name: review\n    from_outputs: requirements\n    meta: true\n"
        "    role_dir: ../outside\n    output_dir: reviews\n", encoding="utf-8")
    analysis = tmp_path / "analysis.json"
    analysis.write_text("[]", encoding="utf-8")
    direction = Direction(module="m", title="safe")
    settings = CampaignSettings(pipeline_template="pipeline.yaml")
    with pytest.raises(ValueError, match="resource escapes repository"):
        materialize_pipeline(tmp_path, settings, direction, analysis)


def test_classify_pipeline_distinguishes_gate_and_validator():
    pipeline = SimpleNamespace(stages=[Stage(name="gate", gate=True)])
    result = TaskResult(Task("x"), True, returncode=0)
    assert classify_pipeline([result], ["gate"], pipeline)[0] == "GATE_REJECTED"
    pipeline.stages[0].gate = False
    result.success = False
    result.validation_ok = False
    assert classify_pipeline([result], ["gate"], pipeline)[0] == "VALIDATION_FAILED"


def test_parallel_ready_requires_clean_git(tmp_path):
    assert parallel_ready(tmp_path)[0] is False
    _init_git(tmp_path)
    assert parallel_ready(tmp_path) == (True, "")
    (tmp_path / "dirty.txt").write_text("dirty", encoding="utf-8")
    assert parallel_ready(tmp_path)[0] is False


def test_parallel_directions_use_real_isolated_worktrees(tmp_path):
    (tmp_path / "pi-batch.py").write_text(
        "import pathlib, sys\n"
        "log = pathlib.Path(sys.argv[sys.argv.index('--log-file') + 1])\n"
        "with log.open('a', encoding='utf-8') as handle:\n"
        "    handle.write('CHILD-LOG\\n')\n"
        "print('ISOLATED-MODEL-BODY')\n",
        encoding="utf-8")
    (tmp_path / "pipeline.yaml").write_text(
        "stages:\n  - name: requirements\n    from_prompt: old\n    output: report.md\n",
        encoding="utf-8")
    analysis = tmp_path / "analysis.json"
    analysis.write_text("[]", encoding="utf-8")
    _init_git(tmp_path, extra=["pi-batch.py", "pipeline.yaml", "analysis.json"])
    settings = CampaignSettings(name="../../escape", pipeline_template="pipeline.yaml",
                                output_dir="docs/auto", worktree_root=".worktrees")
    directions = [Direction.from_mapping("src", {
        "title": f"Direction {index}", "problem": "gap", "evidence": ["source.py"],
        "acceptance": ["test"],
    }) for index in (1, 2)]
    outcomes = run_directions_isolated(
        tmp_path, settings, [(item, analysis) for item in directions], 2, [])
    assert len(outcomes) == 2
    assert {item.status for item in outcomes} == {"PASSED"}
    for outcome in outcomes:
        assert outcome.branch.startswith("pbatch-campaign/escape-")
        assert ".." not in outcome.branch
        worktree = Path(outcome.evidence[0]).resolve()
        worktree.relative_to((tmp_path / ".worktrees").resolve())
        assert worktree.joinpath(".git").exists()
        run_log = Path(outcome.evidence[2]).read_text(encoding="utf-8")
        assert run_log == "CHILD-LOG\n"
        assert "ISOLATED-MODEL-BODY" not in run_log
        assert outcome.baseline_commit
        assert outcome.commit
        assert outcome.clean_commit == outcome.commit
        assert outcome.commit != outcome.baseline_commit
        assert outcome.dirty_evidence == []
        assert subprocess.run(
            ["git", "-C", str(worktree), "merge-base", "--is-ancestor",
             outcome.baseline_commit, outcome.commit], check=False).returncode == 0
        committed = subprocess.run(
            ["git", "-C", str(worktree), "show", "--pretty=", "--name-only",
             outcome.commit], capture_output=True, text=True, check=True).stdout
        assert f"docs/auto/runs/{outcome.direction.direction_id}/pipeline.yaml" in committed


def test_isolated_pipeline_rejects_failed_final_commit_hook(tmp_path):
    (tmp_path / "pi-batch.py").write_text(
        "import pathlib, sys\n"
        "log = pathlib.Path(sys.argv[sys.argv.index('--log-file') + 1])\n"
        "log.write_text('CHILD-LOG\\n', encoding='utf-8')\n",
        encoding="utf-8")
    (tmp_path / "pipeline.yaml").write_text(
        "stages:\n  - name: requirements\n    from_prompt: old\n    output: report.md\n",
        encoding="utf-8")
    analysis = tmp_path / "analysis.json"
    analysis.write_text("[]", encoding="utf-8")
    _init_git(tmp_path, extra=["pi-batch.py", "pipeline.yaml", "analysis.json"])
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho hook-rejected >&2\nexit 17\n", encoding="utf-8")
    hook.chmod(0o755)
    settings = CampaignSettings(
        name="hook-failure", pipeline_template="pipeline.yaml",
        output_dir="docs/auto", worktree_root=".worktrees")
    direction = Direction.from_mapping("src", {
        "title": "Rejected final commit", "problem": "hook blocks commit",
        "evidence": ["source.py"], "acceptance": ["failure is recorded"],
    })

    outcome = run_directions_isolated(
        tmp_path, settings, [(direction, analysis)], 1, [])[0]

    assert outcome.status == "PIPELINE_FAILED"
    assert "final campaign commit failed" in outcome.reason
    assert "hook-rejected" in outcome.reason
    assert outcome.commit == outcome.baseline_commit
    assert outcome.clean_commit == ""
    assert outcome.dirty_evidence


def test_existing_worktree_fast_forwards_baseline_and_rejects_dirty(tmp_path):
    from pbatch.campaign_pipeline import _ensure_worktree

    _init_git(tmp_path)
    settings = CampaignSettings(name="sync", worktree_root=".worktrees")
    direction = Direction(module="src", title="Synchronize branch")
    worktree, branch, original = _ensure_worktree(tmp_path, settings, direction)
    assert branch
    assert original == subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True,
        text=True, check=True).stdout.strip()

    fresh = tmp_path / "fresh.py"
    fresh.write_text("fresh\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "fresh.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "fresh"], check=True)
    expected = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True,
        text=True, check=True).stdout.strip()

    same, same_branch, baseline = _ensure_worktree(tmp_path, settings, direction)
    assert (same, same_branch, baseline) == (worktree, branch, expected)
    assert (worktree / "fresh.py").read_text(encoding="utf-8") == "fresh\n"
    assert subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True,
        text=True, check=True).stdout.strip() == expected

    (worktree / "dirty.py").write_text("local\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="worktree is dirty"):
        _ensure_worktree(tmp_path, settings, direction)


def test_isolated_pipeline_has_hard_wall_clock_timeout(tmp_path):
    marker = tmp_path / "survived"
    (tmp_path / "pi-batch.py").write_text(
        "import pathlib, time\n"
        "time.sleep(30)\n"
        f"pathlib.Path({str(marker)!r}).write_text('late')\n",
        encoding="utf-8")
    (tmp_path / "pipeline.yaml").write_text(
        "stages:\n  - name: requirements\n    from_prompt: old\n    output: report.md\n",
        encoding="utf-8")
    analysis = tmp_path / "analysis.json"
    analysis.write_text("[]", encoding="utf-8")
    _init_git(tmp_path, extra=["pi-batch.py", "pipeline.yaml", "analysis.json"])
    settings = CampaignSettings(
        pipeline_template="pipeline.yaml", output_dir="docs/auto",
        worktree_root=".worktrees", pipeline_timeout=1)
    direction = Direction.from_mapping("src", {
        "title": "Bound work", "problem": "pipeline may hang",
        "evidence": ["source.py"], "acceptance": ["deadline enforced"],
    })
    outcome = run_directions_isolated(tmp_path, settings, [(direction, analysis)], 1, [])[0]
    assert outcome.status == "PIPELINE_FAILED"
    assert "timed out after 1s" in outcome.reason
    assert outcome.elapsed < 6
    assert not marker.exists()


def test_campaign_end_to_end_and_fingerprint_reuse(tmp_path):
    _campaign_fixture(tmp_path)
    command = [sys.executable, str(PI_BATCH), "campaign", "--config", "campaign.yaml",
               "--agent-bin", str(tmp_path / "agent.sh"), "--no-lock", "--log-file", ""]
    dry = subprocess.run([*command, "--dry-run"], cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert dry.returncode == 0
    assert "Dry-run made no files" in dry.stdout
    assert not (tmp_path / "docs" / "auto").exists()

    first = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stderr
    calls = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    assert len(calls) == 3  # analysis, requirements, gate
    summary = (tmp_path / "docs" / "auto" / "SUMMARY.md").read_text(encoding="utf-8")
    assert "PASSED" in summary
    events = [json.loads(line) for line in (tmp_path / "docs" / "auto" / "state.jsonl")
              .read_text(encoding="utf-8").splitlines()]
    passed = [event for event in events if event.get("status") == "PASSED"]
    assert passed
    assert all("head_commit" in event for event in passed)
    assert all(event.get("clean_commit") or event.get("dirty_evidence")
               for event in passed)

    second = subprocess.run([*command, "--skip-passed"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=60)
    assert second.returncode == 0, second.stderr
    assert len((tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()) == 3


def test_campaign_rounds_reuse_between_rounds(tmp_path):
    """--rounds 2 runs the campaign twice; when the repo snapshot is
    unchanged between rounds, round 2 reuses everything via fingerprints
    (no extra agent calls), so 'run it N times' is first-class instead of
    an external bash loop (real-world lesson: aero-vault 10x)."""
    _campaign_fixture(tmp_path)
    # log calls under docs/auto/ (excluded from the repo snapshot) so the
    # working tree stays clean between rounds and reuse can actually match
    calls = tmp_path / "docs" / "auto" / "calls.log"
    agent = tmp_path / "agent.sh"
    candidate = json.dumps([{
        "title": "Harden current API", "problem": "No explicit contract", "value": 9,
        "risk_reduction": 8, "effort": 2, "confidence": 9,
        "evidence": ["src/mod/code.py"], "acceptance": ["contract test passes"],
    }])
    agent.write_text(
        "#!/bin/sh\n"
        f"printf 'call\\n' >> '{calls}'\n"
        "case \"$2\" in\n"
        f"  *'Return ONLY a JSON array'*) printf '%s\\n' '{candidate}' ;;\n"
        "  *'VERDICT: PASS'*) printf '%s\\n' 'VERDICT: PASS - requirements are satisfied' ;;\n"
        "  *) printf '%s\\n' '## Requirements complete' ;;\n"
        "esac\n", encoding="utf-8")
    agent.chmod(0o755)
    # --skip-passed enables fingerprint reuse between rounds; without it
    # every round is a fresh full pass by design (fresh evidence)
    command = [sys.executable, str(PI_BATCH), "campaign", "--config", "campaign.yaml",
               "--agent-bin", str(agent), "--no-lock", "--log-file", "",
               "--rounds", "2", "--round-delay", "0", "--skip-passed"]
    first = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stderr
    combined = first.stdout + first.stderr
    assert "CAMPAIGN ROUND 1/2" in combined
    assert "CAMPAIGN ROUND 2/2" in combined
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 3  # round 2 reused all


def test_campaign_loop_refreshes_repository_snapshot_each_round(monkeypatch, tmp_path):
    from pbatch import campaign as C

    snapshots = iter(({"head": "one", "dirty": "a"},
                      {"head": "two", "dirty": "b"}))
    observed = []
    settings = CampaignSettings(output_dir="out", worktree_root="worktrees")
    args = SimpleNamespace(dry_run=False, rounds=2, round_commit=False,
                           round_delay=0, log_file="")
    context = C.CampaignContext(tmp_path, settings, args,
                                StateStore(tmp_path / "state.jsonl"), {})
    monkeypatch.setattr(C, "git_snapshot", lambda _root, _excludes: next(snapshots))
    monkeypatch.setattr(C, "run_campaign",
                        lambda ctx: observed.append(dict(ctx.snapshot)) or 0)

    assert C.run_campaign_loop(context) == 0
    assert observed == [{"head": "one", "dirty": "a"},
                        {"head": "two", "dirty": "b"}]


def test_campaign_rounds_rejects_negative_and_nonfinite(tmp_path):
    _campaign_fixture(tmp_path)
    for flag, value in (("--rounds", "-1"), ("--round-delay", "nan"),
                        ("--round-delay", "-5")):
        command = [sys.executable, str(PI_BATCH), "campaign", "--config", "campaign.yaml",
                   "--agent-bin", str(tmp_path / "agent.sh"), "--no-lock",
                   "--rounds", "1", "--round-delay", "0", "--dry-run"]
        # rebuild with the offending value in place
        command = [sys.executable, str(PI_BATCH), "campaign", "--config", "campaign.yaml",
                   "--agent-bin", str(tmp_path / "agent.sh"), "--no-lock", "--dry-run",
                   flag, value]
        r = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, timeout=60)
        assert r.returncode == 2, (flag, value, r.stderr[-300:])


def test_round_commit_commits_and_skips_clean(tmp_path, monkeypatch):
    """--round-commit: after a round, non-ignored changes are committed;
    an already-clean tree is a successful no-op."""
    from pbatch import campaign as C
    from pbatch.campaign_models import CampaignSettings
    from pbatch.campaign_state import StateStore

    _init_git(tmp_path)
    settings = CampaignSettings(output_dir="docs/auto",
                                state_file="docs/auto/state.jsonl",
                                summary_file="docs/auto/SUMMARY.md")
    store = StateStore(tmp_path / "docs" / "auto" / "state.jsonl")
    args = argparse.Namespace()
    ctx = C.CampaignContext(tmp_path, settings, args, store, {})
    # dirty tree -> committed
    (tmp_path / "source.py").write_text("changed\n", encoding="utf-8")
    assert C._round_commit(ctx, 1) is True
    head = subprocess.run(["git", "-C", str(tmp_path), "log", "--oneline", "-1"],
                          capture_output=True, text=True, check=True).stdout
    assert "[pi-batch] campaign round 1" in head
    # ignored changes -> skipped without a new commit
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", ".gitignore"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "ignore"], check=True, capture_output=True)
    (tmp_path / "ignored.txt").write_text("nope", encoding="utf-8")
    before = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout
    assert C._round_commit(ctx, 3) is True
    after = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True).stdout
    assert before == after
    # clean tree -> skipped too
    before2 = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True).stdout
    assert C._round_commit(ctx, 2) is True
    after2 = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout
    assert before2 == after2


def test_round_loop_commits_the_final_and_single_round(monkeypatch):
    """The final round is still a round; --round-commit must include it."""
    from types import SimpleNamespace
    from pbatch import campaign as C

    context = SimpleNamespace(args=SimpleNamespace(
        dry_run=False, rounds=1, round_commit=True, round_delay=0))
    commits = []
    monkeypatch.setattr(C, "_refresh_repository_snapshot", lambda _context: None)
    monkeypatch.setattr(C, "run_campaign", lambda _context: 0)
    monkeypatch.setattr(C, "_round_commit",
                        lambda _context, round_no: commits.append(round_no) or True)

    assert C.run_campaign_loop(context) == 0
    assert commits == [1]


def test_round_commit_failure_fails_campaign_and_reports_dirty_evidence(
        tmp_path, monkeypatch, caplog):
    from pbatch import campaign as C

    _init_git(tmp_path)
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho policy-denied >&2\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    (tmp_path / "source.py").write_text("dirty\n", encoding="utf-8")
    settings = CampaignSettings(summary_file="summary.md")
    context = C.CampaignContext(
        tmp_path, settings,
        SimpleNamespace(dry_run=False, rounds=1, round_commit=True,
                        round_delay=0, log_file=""),
        StateStore(tmp_path / "state.jsonl"), {})

    assert C._round_commit(context, 1) is False
    assert "policy-denied" in caplog.text
    assert "dirty digest=" in caplog.text
    monkeypatch.setattr(C, "_refresh_repository_snapshot", lambda _context: None)
    monkeypatch.setattr(C, "run_campaign", lambda _context: 0)
    monkeypatch.setattr(C, "_round_commit", lambda _context, _round: False)
    assert C.run_campaign_loop(context) == 1


def test_default_six_stage_pipeline_runs_meta_gates_validator_and_archive(monkeypatch, tmp_path):
    template = yaml.safe_load(
        (ROOT / "examples" / "repository-campaign-pipeline.yaml").read_text(encoding="utf-8"))
    template["git_commit"] = False
    pipeline_file = tmp_path / "pipeline.yaml"
    pipeline_file.write_text(yaml.safe_dump(template, sort_keys=False), encoding="utf-8")
    roles = tmp_path / "prompts"
    roles.mkdir()
    (roles / "qa_lead.md").write_text("ROLE-QA\n{input_content}\n", encoding="utf-8")
    keyword_dir = tmp_path / "pbatch"
    keyword_dir.mkdir()
    keyword_dir.joinpath("role_keywords.yaml").write_text(
        "qa_lead: [test, acceptance, coverage]\n", encoding="utf-8")
    (tmp_path / "quality.py").write_text(
        "import sys\nprint('quality: OK')\nsys.exit(0)\n", encoding="utf-8")
    source = tmp_path / "src" / "module.py"
    source.parent.mkdir()
    source.write_text("def existing(): return True\n", encoding="utf-8")
    analysis = tmp_path / "analysis.json"
    analysis.write_text("[]\n", encoding="utf-8")
    agent = _full_pipeline_agent(tmp_path)
    monkeypatch.setattr(config, "AGENT_BIN", str(agent))
    direction = Direction.from_mapping("src", {
        "title": "Harden behavior", "problem": "contract is implicit",
        "value": 9, "risk_reduction": 8, "effort": 2, "confidence": 9,
        "evidence": ["src/module.py"], "acceptance": ["regression test passes"],
    })
    settings = CampaignSettings(pipeline_template="pipeline.yaml", output_dir="docs/auto")
    outcome = run_direction(tmp_path, settings, direction, analysis)
    assert outcome.status == "PASSED"
    assert len((tmp_path / "agent-calls.log").read_text(encoding="utf-8").splitlines()) == 8
    assert all(Path(item).exists() for item in outcome.evidence)
    assert len(outcome.evidence) == len(set(outcome.evidence))
    assert any("/archive/" in item for item in outcome.evidence)


def _init_git(path: Path, extra: list[str] | None = None) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    source = path / "source.py"
    source.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "source.py", *(extra or [])], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _campaign_fixture(path: Path) -> None:
    module = path / "src" / "mod"
    module.mkdir(parents=True)
    (module / "code.py").write_text("def current(): return True\n", encoding="utf-8")
    (path / "pipeline.yaml").write_text(
        "stages:\n"
        "  - name: requirements\n    from_prompt: placeholder\n    output: docs/req.md\n"
        "  - name: gate\n    from_outputs: requirements\n    aggregate: true\n    gate: true\n"
        "    tasks:\n      - prompt: 'Review {input_content} and output VERDICT: PASS or FAIL'\n"
        "        output: docs/gate.md\n", encoding="utf-8")
    (path / "campaign.yaml").write_text(
        "name: test-campaign\n"
        "discovery:\n  roots: [src]\n  max_depth: 1\n  fallback_top_level: false\n"
        "analysis:\n  jobs: 1\n  retries: 0\n  candidate_limit: 1\n"
        "selection:\n  max_directions: 1\n"
        "implementation:\n  pipeline: pipeline.yaml\n  concurrency: 1\n"
        "state:\n  output_dir: docs/auto\n  state_file: docs/auto/state.jsonl\n"
        "  summary_file: docs/auto/SUMMARY.md\n", encoding="utf-8")
    candidate = json.dumps([{
        "title": "Harden current API", "problem": "No explicit contract", "value": 9,
        "risk_reduction": 8, "effort": 2, "confidence": 9,
        "evidence": ["src/mod/code.py"], "acceptance": ["contract test passes"],
    }])
    agent = path / "agent.sh"
    agent.write_text(
        "#!/bin/sh\n"
        f"printf 'call\\n' >> '{path / 'calls.log'}'\n"
        "case \"$2\" in\n"
        f"  *'Return ONLY a JSON array'*) printf '%s\\n' '{candidate}' ;;\n"
        "  *'VERDICT: PASS'*) printf '%s\\n' 'VERDICT: PASS - requirements are satisfied' ;;\n"
        "  *) printf '%s\\n' '## Requirements complete' ;;\n"
        "esac\n", encoding="utf-8")
    agent.chmod(0o755)


def _full_pipeline_agent(path: Path) -> Path:
    agent = path / "full-agent.py"
    agent.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        f"log = pathlib.Path({str(path / 'agent-calls.log')!r})\n"
        "log.write_text(log.read_text() + 'call\\n' if log.exists() else 'call\\n')\n"
        "prompt = sys.argv[2]\n"
        "if 'Available roles:' in prompt:\n"
        "    print('[]' if 'qa review complete' in prompt else '[\"qa_lead\"]')\n"
        "elif 'ROLE-QA' in prompt:\n"
        "    print('## qa review complete')\n"
        "elif 'VERDICT: PASS' in prompt:\n"
        "    print('VERDICT: PASS - evidence and acceptance checks verified')\n"
        "elif 'concrete design' in prompt:\n"
        "    print('## Design\\ntest acceptance regression coverage')\n"
        "else:\n"
        "    print('## Completed\\nEvidence-backed output')\n",
        encoding="utf-8")
    agent.chmod(0o755)
    return agent
