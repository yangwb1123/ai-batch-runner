"""Regression tests for AADM formula and evidence-boundary helpers."""

from __future__ import annotations

import pytest

from pbatch import atomize, clock, formulas, temporal
from pbatch.waves import aging_boost, fair_priority_score, rank_nodes
from pbatch.fabric_sched import TaskNode


def test_uncertainty_is_criticality_weighted():
    items = [{"criticality": 1, "confidence": 1},
             {"criticality": 3, "confidence": 0}]
    assert formulas.uncertainty_score(items) == 0.75
    assert formulas.weighted_uncertainty(items) == 0.75
    assert formulas.uncertainty_score([]) == 0.0


def test_risk_keeps_worst_case_visible():
    worst = formulas.risk_item(1, 1, 1, 1, 0)
    assert worst == 1.0
    assert formulas.aggregate_risk([worst, 0.0]) == 0.85
    assert formulas.aggregate_risk([{"risk": 1, "criticality": 3},
                                    {"risk": 0, "criticality": 1}]) == 0.925


def test_complexity_and_confidence_weighted_formulas():
    assert formulas.complexity_score(1, 0, 0, 0, 0, 0) == 0.2
    assert formulas.confidence_score(1, 1, 1, 1, 1) == 1.0
    assert formulas.confidence_score(0, 0, 0, 0, 0) == 0.0
    with pytest.raises(ValueError):
        formulas.confidence_score(2, 0, 0, 0, 0)
    assert formulas.probe_value(0.8, 0.5, 0.1, 0.1) == 0.2


def test_atom_metadata_has_types_source_and_time():
    report = atomize.atomize(
        "现有审批接口，优先记录审计；测试结果显示通过，决定上线",
        time={"event_time": "2026-08-20T00:00:00Z", "version": 2})
    assert report["count"] >= 5
    for item in report["atoms"]:
        assert item["id"]
        assert item["source"] == "text"
        assert item["source_type"] == "user"
        assert item["time"]["event_time"].endswith("Z")
        assert item["time"]["version"] == 2
    types = {item["type"] for item in report["atoms"]}
    assert {"fact", "preference", "evidence", "decision"} <= types


def test_temporal_four_times_and_validity():
    record = temporal.temporal_record(
        event_time="2026-08-19T00:00:00Z",
        processing_time="2026-08-19T00:01:00Z",
        valid_from="2026-08-19T00:00:00Z",
        valid_until="2026-08-21T00:00:00Z",
        transaction_time="2026-08-19T00:02:00Z", version=3)
    assert record["version"] == 3
    assert temporal.is_valid_at(record, "2026-08-20T00:00:00Z") is True
    assert temporal.is_valid_at(record, "2026-08-22T00:00:00Z") is False
    assert temporal.age_seconds(record, "2026-08-20T00:00:00Z") == 86400
    assert temporal.effective_confidence(
        1.0, "slow_changing", record, "2026-08-26T00:00:00Z") == 0.5
    with pytest.raises(ValueError, match="timezone"):
        temporal.temporal_record(event_time="2026-08-19T00:00:00")


def test_clock_uses_monotonic_deadline_with_wall_fallback():
    window = clock.lease_window(10, wall_now=100, monotonic_now=50)
    lease = {**window, "clock_id": "epoch-1"}
    assert clock.lease_expired(lease, clock_id="epoch-1",
                               monotonic_now=59, wall_now=10000) is False
    assert clock.lease_expired(lease, clock_id="epoch-1",
                               monotonic_now=60, wall_now=100) is True
    # A lease from another clock epoch uses the conservative skew-aware wall
    # fallback rather than trusting an incomparable monotonic value.
    assert clock.lease_expired(lease, clock_id="epoch-2",
                               wall_now=105) is True


def test_wave_rank_uses_aging_to_prevent_starvation():
    first = TaskNode(id="first", goal="same", argv=[])
    waiting = TaskNode(id="waiting", goal="same", argv=[])
    nodes = [first, waiting]
    assert fair_priority_score(waiting, nodes, wait_cycles=10) > \
        fair_priority_score(first, nodes, wait_cycles=0)
    ordered = rank_nodes([first, waiting], nodes,
                         wait_cycles={"waiting": 10})
    assert ordered[0].id == "waiting"
    assert aging_boost(5, 0.2) == 1.0


def test_formulas_cli_json(capsys):
    with pytest.raises(SystemExit) as exc:
        formulas.formulas_main(["uncertainty", "--items",
                                '[{"criticality": 1, "confidence": 0}]',
                                "--json"])
    assert exc.value.code == 0
    assert '"score": 1.0' in capsys.readouterr().out
