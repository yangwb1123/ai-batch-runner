"""Runtime state paths must honor PBATCH_DATA_DIR without breaking overrides."""

from __future__ import annotations

import json

from pbatch import (causality, config, events, fabric_devices, metrics,
                    pinned, recovery, truth)
from pbatch.memory_policy import memory_scope


def test_truth_and_causality_follow_external_data_dir(tmp_path, monkeypatch):
    external = tmp_path / "runtime"
    monkeypatch.setenv("PBATCH_DATA_DIR", str(external))
    monkeypatch.setattr(config, "_DATA_DIR_OVERRIDE", "")

    truth._record_to_log(
        truth.TruthStore(), "claim",
        {"id": "claim-1", "statement": "事实", "premises": [],
         "confidence": 0.8},
    )
    causality._append({
        "action": "new",
        "payload": {"id": "H-1", "symptom": "慢", "cause": "缓存",
                    "intervention": "测量", "expected": "变快"},
    })

    assert (external / "truth.jsonl").is_file()
    assert (external / "causality.jsonl").is_file()
    assert truth.load_from_log().status("claim-1")["statement"] == "事实"
    assert causality.load_hypotheses()["H-1"]["proposed_cause"] == "缓存"


def test_metrics_reads_external_runtime_sources(tmp_path, monkeypatch):
    external = tmp_path / "runtime"
    monkeypatch.setenv("PBATCH_DATA_DIR", str(external))
    monkeypatch.setattr(config, "_DATA_DIR_OVERRIDE", "")
    (external / "rejected").mkdir(parents=True)
    (external / "rejected" / "one.rejected.md").write_text("x", encoding="utf-8")
    (external / "memory").mkdir(parents=True)
    (external / "memory" / "sessions.index.jsonl").write_text(
        json.dumps({"observed_verdict": "PASS", "scope": memory_scope()})
        + "\n", encoding="utf-8")
    (external / "truth.jsonl").write_text(
        json.dumps({"action": "invalidate"}) + "\n", encoding="utf-8")

    report = metrics.metrics_report()
    assert report["result_quality"]["rejected_artifacts"] == 1
    assert report["resource_quality"]["sessions"] == 1
    assert report["decision_quality"]["truth_invalidations"] == 1


def test_device_state_and_event_defaults_follow_external_data_dir(tmp_path,
                                                                  monkeypatch):
    external = tmp_path / "runtime"
    monkeypatch.setenv("PBATCH_DATA_DIR", str(external))
    monkeypatch.setattr(config, "_DATA_DIR_OVERRIDE", "")
    expected = external / "devices"
    assert fabric_devices._effective_device_state_dir() == expected
    (expected).mkdir(parents=True)
    (expected / "events.jsonl").write_text(
        json.dumps({"verb": "submit"}) + "\n", encoding="utf-8")
    assert events.read_events(limit=1)[0]["verb"] == "submit"


def test_budget_and_pinned_defaults_follow_external_data_dir(tmp_path,
                                                            monkeypatch):
    external = tmp_path / "runtime"
    monkeypatch.setenv("PBATCH_DATA_DIR", str(external))
    monkeypatch.setattr(config, "_DATA_DIR_OVERRIDE", "")

    recovery.consume_budget("test", cost=2)
    status = recovery.budget_status(allowance=3)
    assert status["consumed"] == 2.0

    (external / "pinned.md").write_text("硬约束", encoding="utf-8")
    report = pinned.render_pinned(cwd=str(tmp_path))
    assert report["sources"] == [str(external / "pinned.md")]
    assert "硬约束" in report["content"]
