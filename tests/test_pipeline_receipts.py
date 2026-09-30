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
