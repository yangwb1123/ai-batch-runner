"""Verified stage receipts: bounded, versioned, and never able to fail a run."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pbatch import config, pipeline, pipeline_receipts as receipts
from pbatch.models import Pipeline, Stage, Task, TaskResult


def _agent(tmp_path: Path, body: str = "echo deliverable\n") -> Path:
    path = tmp_path / "agent.sh"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _result(output: str, cwd: Path, *, success: bool = True) -> TaskResult:
    return TaskResult(Task(prompt="p", output=output, cwd=str(cwd)),
                      success, stdout="done")


def _run_pipeline(tmp_path: Path, decision_log: bool) -> Pipeline:
    """One real stage through the real executor."""
    agent = _agent(tmp_path)
    output = "docs/out.md"
    stage = Stage(name="design", from_prompt="write it", output=output,
                  cwd=str(tmp_path), agent=str(agent),
                  validate_cmd="")
    spec = Pipeline(stages=[stage],
                    decision_log=str(tmp_path / "DECISIONS.md") if decision_log else "")
    previous = (config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT)
    config.MEMORY_MODE = "off"
    config.STREAM_OUTPUT = "none"
    try:
        pipeline.run_pipeline(spec, timeout_override=30)
    finally:
        config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT = previous
    return spec


def test_receipt_records_artifact_digest_after_a_verified_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    _run_pipeline(tmp_path, decision_log=False)

    records = receipts.read_receipts(str(tmp_path))
    assert len(records) == 1
    record = records[0]
    assert record["schema_version"] == receipts.RECEIPT_SCHEMA_VERSION
    assert record["stage"] == "design" and record["ok"] is True
    assert record["verdict"] == ""
    task = record["tasks"][0]
    assert task["output"] == "docs/out.md"
    assert task["ok"] is True and task["bytes"] > 0 and task["digest"]


def test_receipt_is_written_without_a_decision_log_and_outside_the_tree(tmp_path, monkeypatch):
    """The verified record must not depend on an opt-in project document."""
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    _run_pipeline(tmp_path, decision_log=False)

    assert not (tmp_path / "DECISIONS.md").exists()
    assert not (tmp_path / "pipeline").exists()
    ledger = receipts.receipts_path(str(tmp_path))
    assert ledger.is_file() and ledger.is_relative_to(tmp_path / "runtime")


def test_digest_notices_a_changed_artifact_and_ignores_missing_ones(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    artifact = tmp_path / "out.md"
    artifact.write_text("first\n", encoding="utf-8")
    before = receipts._identity(artifact)[1]
    artifact.write_text("second\n", encoding="utf-8")
    after = receipts._identity(artifact)[1]
    assert before and after and before != after
    assert receipts._identity(tmp_path / "absent.md") == (0, "")
    directory = receipts._identity(tmp_path)
    assert directory == (0, ""), "only regular files carry an identity"


def test_unwritable_runtime_degrades_instead_of_failing_the_pipeline(tmp_path, monkeypatch):
    """A lost observation must never become a failed stage."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.setenv("PBATCH_DATA_DIR", str(blocker / "runtime"))
    assert receipts.record_stage(str(tmp_path), "design",
                                 [_result("out.md", tmp_path)], True, None) is False
    assert receipts.read_receipts(str(tmp_path)) == []


def test_corrupt_and_foreign_records_are_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    receipts.record_stage(str(tmp_path), "good", [_result("a.md", tmp_path)], True, "PASS")
    ledger = receipts.receipts_path(str(tmp_path))
    with ledger.open("ab") as handle:
        handle.write(b"{not json\n")
        handle.write(json.dumps({"schema_version": 999, "stage": "future"}).encode() + b"\n")
    records = receipts.read_receipts(str(tmp_path))
    assert [item["stage"] for item in records] == ["good"]


def test_ledger_stays_inside_its_byte_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    for index in range(400):
        receipts.record_stage(str(tmp_path), f"stage-{index}",
                              [_result(f"out-{index}.md", tmp_path)], True, None)
    ledger = receipts.receipts_path(str(tmp_path))
    assert ledger.stat().st_size <= receipts.RECEIPTS_MAX_BYTES
    assert receipts.read_receipts(str(tmp_path), limit=1)[0]["stage"] == "stage-399"
    assert len(receipts.read_receipts(str(tmp_path), limit=10_000)) <= receipts.READ_MAX_RECORDS


def test_knowledge_cites_only_artifacts_that_still_match_their_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    kept = tmp_path / "kept.md"
    kept.write_text("stable\n", encoding="utf-8")
    stale = tmp_path / "stale.md"
    stale.write_text("first\n", encoding="utf-8")
    receipts.record_stage(str(tmp_path), "design",
                          [_result("kept.md", tmp_path), _result("stale.md", tmp_path)],
                          True, "PASS")
    stale.write_text("edited since verification\n", encoding="utf-8")

    block = receipts.verified_knowledge(str(tmp_path),
                                    [str(kept), str(stale)])
    assert "kept.md" in block and "stale.md" not in block


def test_knowledge_refuses_failed_and_rejected_receipts(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    (tmp_path / "good.md").write_text("g\n", encoding="utf-8")
    (tmp_path / "failed.md").write_text("f\n", encoding="utf-8")
    (tmp_path / "rejected.md").write_text("r\n", encoding="utf-8")
    receipts.record_stage(str(tmp_path), "design", [_result("good.md", tmp_path)], True, "PASS")
    receipts.record_stage(str(tmp_path), "design", [_result("failed.md", tmp_path)], False, None)
    receipts.record_stage(str(tmp_path), "gate", [_result("rejected.md", tmp_path)], True, "FAIL")

    block = receipts.verified_knowledge(
        str(tmp_path), [str(tmp_path / "good.md"), str(tmp_path / "failed.md"),
                        str(tmp_path / "rejected.md")])
    assert "good.md" in block
    assert "failed.md" not in block and "rejected.md" not in block


def test_knowledge_is_stable_while_nothing_changed_and_silent_when_empty(tmp_path, monkeypatch):
    """Byte-stability is what keeps a downstream reuse fingerprint from
    self-invalidating: a stage re-verified under --reuse writes a receipt with
    the same content, so the block must not change."""
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    (tmp_path / "up.md").write_text("body\n", encoding="utf-8")
    assert receipts.verified_knowledge(str(tmp_path), [str(tmp_path / "up.md")]) == ""

    receipts.record_stage(str(tmp_path), "design", [_result("up.md", tmp_path)], True, None)
    first = receipts.verified_knowledge(str(tmp_path), [str(tmp_path / "up.md")])
    receipts.record_stage(str(tmp_path), "design", [_result("up.md", tmp_path)], True, None)
    assert receipts.verified_knowledge(str(tmp_path), [str(tmp_path / "up.md")]) == first

    assert receipts.verified_knowledge(str(tmp_path), []) == ""
    assert receipts.verified_knowledge(str(tmp_path),
                                    [str(tmp_path / "absent.md")]) == ""

def _two_stage_agent(tmp_path: Path) -> Path:
    """Agent that records each prompt it received, one file per call."""
    seen = tmp_path / "prompts.log"
    agent = tmp_path / "agent.sh"
    agent.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$2" >> {seen}\n'
        'case "$2" in *"design it"*) echo "# design" ;; *) echo "# implement" ;; esac\n',
        encoding="utf-8")
    agent.chmod(0o755)
    return agent


def _two_stage_pipeline(tmp_path: Path) -> Pipeline:
    design = Stage(name="design", from_prompt="design it", output="docs/design.md",
                   cwd=str(tmp_path))
    implement = Stage(name="implement", from_outputs="design", aggregate=True,
                      tasks=[{"prompt": "implement {input_stem}", "output": "docs/impl.md"}],
                      cwd=str(tmp_path))
    return Pipeline(stages=[design, implement])


def test_rebuild_cites_verified_upstream_and_a_fully_reused_run_cites_nothing(
        tmp_path, monkeypatch):
    """The scenario the feature targets: upstream still verifies, downstream
    has to be rebuilt, and the rebuild is told what upstream already proved.

    Also pins the negative case. When everything is reusable there is nothing
    to rebuild and nothing to say, so no prompt bytes are spent on a heading.
    """
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    agent = _two_stage_agent(tmp_path)
    previous = (config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT)
    config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT = str(agent), "off", "none"
    try:
        pipeline.run_pipeline(_two_stage_pipeline(tmp_path), timeout_override=30)
        first = (tmp_path / "prompts.log").read_text(encoding="utf-8")
        assert "Verified upstream artifacts" not in first, "a first run has no receipts"

        (tmp_path / "prompts.log").write_text("", encoding="utf-8")
        pipeline.run_pipeline(_two_stage_pipeline(tmp_path), timeout_override=30, reuse=True)
        reused = (tmp_path / "prompts.log").read_text(encoding="utf-8")
        assert reused == "", "a fully reused run must not re-prompt anything"

        (tmp_path / "docs" / "impl.md").unlink()
        (tmp_path / "prompts.log").write_text("", encoding="utf-8")
        pipeline.run_pipeline(_two_stage_pipeline(tmp_path), timeout_override=30, reuse=True)
        rebuild = (tmp_path / "prompts.log").read_text(encoding="utf-8")
    finally:
        config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT = previous

    assert rebuild.count("Verified upstream artifacts") == 1
    assert "docs/design.md" in rebuild, "the cited artifact is named"
    assert "implement combined" in rebuild, "the real task prompt is still there"


def test_receipt_records_the_citation_count_and_effect_splits_by_it(tmp_path, monkeypatch):
    """The decision to keep or drop verified-upstream citation needs the split,
    so the receipt carries the count and the ledger can report the split."""
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    for index in range(3):
        (tmp_path / f"a{index}.md").write_text("a\n", encoding="utf-8")
        receipts.record_stage(str(tmp_path), f"cited-{index}",
                              [_result(f"a{index}.md", tmp_path)], True, None,
                              knowledge_cited=2)
    for index in range(2):
        (tmp_path / f"b{index}.md").write_text("b\n", encoding="utf-8")
        receipts.record_stage(str(tmp_path), f"plain-{index}",
                              [_result(f"b{index}.md", tmp_path)], index == 0, None)

    effect = receipts.knowledge_effect(str(tmp_path))
    assert effect["cited"] == {"stages": 3, "passed": 3, "pass_rate": 1.0}
    assert effect["not_cited"] == {"stages": 2, "passed": 1, "pass_rate": 0.5}


def test_knowledge_effect_reports_no_rate_rather_than_guessing_one(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    empty = receipts.knowledge_effect(str(tmp_path))
    assert empty["cited"] == {"stages": 0, "passed": 0, "pass_rate": None}
    assert empty["not_cited"]["pass_rate"] is None


def test_reuse_rerun_records_that_the_rebuild_was_given_verified_upstream(
        tmp_path, monkeypatch):
    """End to end: the count reaches the receipt through the real stage."""
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    agent = _two_stage_agent(tmp_path)
    previous = (config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT)
    config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT = str(agent), "off", "none"
    try:
        pipeline.run_pipeline(_two_stage_pipeline(tmp_path), timeout_override=30)
        assert receipts.knowledge_effect(str(tmp_path))["cited"]["stages"] == 0
        (tmp_path / "docs" / "impl.md").unlink()
        pipeline.run_pipeline(_two_stage_pipeline(tmp_path), timeout_override=30,
                              reuse=True)
    finally:
        config.AGENT_BIN, config.MEMORY_MODE, config.STREAM_OUTPUT = previous

    cited = [item for item in receipts.read_receipts(str(tmp_path))
             if item.get("knowledge_cited")]
    assert cited, "the rebuilt stage must be recorded as having been cited"
    assert cited[-1]["stage"] == "implement"
    assert cited[-1]["knowledge_cited"] == 1
    assert receipts.knowledge_effect(str(tmp_path))["cited"]["stages"] == 1


def test_metrics_report_exposes_the_citation_split_and_survives_no_receipts(tmp_path, monkeypatch):
    """The split has to be reachable without a new command, and a workspace
    with no receipts must still produce a report rather than an error."""
    from pbatch import metrics
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.chdir(tmp_path)

    empty = metrics.metrics_report()["result_quality"]
    assert empty["reuse_cited_stages"] == 0
    assert empty["reuse_cited_pass_rate"] is None, "no data is not a 0.0 rate"
    assert empty["reuse_uncited_pass_rate"] is None

    (tmp_path / "a.md").write_text("a\n", encoding="utf-8")
    receipts.record_stage(str(tmp_path), "implement", [_result("a.md", tmp_path)],
                          True, None, knowledge_cited=1)
    (tmp_path / "b.md").write_text("b\n", encoding="utf-8")
    receipts.record_stage(str(tmp_path), "design", [_result("b.md", tmp_path)],
                          False, None)
    filled = metrics.metrics_report()["result_quality"]
    assert (filled["reuse_cited_stages"], filled["reuse_cited_pass_rate"]) == (1, 1.0)
    assert (filled["reuse_uncited_stages"], filled["reuse_uncited_pass_rate"]) == (1, 0.0)
