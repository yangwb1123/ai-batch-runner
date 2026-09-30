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