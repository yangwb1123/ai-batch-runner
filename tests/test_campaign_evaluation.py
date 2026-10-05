"""Evaluation scores campaign windows, not individual tasks."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from pbatch.campaign_evaluation import (BREACH, DIMENSIONS, OK, WATCH,
                                         GitFacts, Scorecard, SupervisorFacts,
                                         classify_analysis_failure,
                                         classify_rejection,
                                         collect_supervisor_facts, compare,
                                         evaluate, read_rows, render_markdown)

NOW = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)


def _row(status: str, **extra) -> dict:
    row = {"status": status, "updated_at": "2026-10-01T00:00:00+00:00"}
    row.update(extra)
    return row


def _metrics(utilization: float, tokens=None, telemetry: str = "unknown") -> dict:
    return _row("CAMPAIGN_METRICS", metrics={
        "concurrency_slot_utilization": utilization,
        "analysis_token_telemetry": telemetry,
        "analysis_tokens": tokens,
        "analysis_wall_seconds": 600.0,
        "retry_ratio": 0.05,
    })


def _healthy_rows(merges: int = 12) -> list[dict]:
    rows = [_metrics(0.8), _metrics(0.75)]
    for index in range(merges):
        rows.append(_row("PASSED", elapsed=3600.0))
        rows.append(_row("ANALYZED"))
    return rows


def _git(merges: int = 12, *, ahead: int = 12, behind: int = 3,
         age: float = 1.0, violations: int = 0, forbidden: int = 0) -> GitFacts:
    dates = tuple(f"2026-10-0{min(9, 1 + index // 6)}" for index in range(merges))
    return GitFacts(
        merges=merges, merge_dates=dates, files_changed=merges * 3,
        insertions=merges * 40, deletions=merges, ahead=ahead, behind=behind,
        merge_base_age_days=age,
        framework_free_violations=tuple(f"packages/core/src/x{index}.ts"
                                        for index in range(violations)),
        forbidden_paths=tuple(f"pnpm-lock.yaml" for _ in range(forbidden)))


def _supervisor(segments: int = 10, stalls: int = 1) -> SupervisorFacts:
    return SupervisorFacts(segments=segments, stall_breaks=stalls,
                           preflight_failures=0, status_lines=4)


def test_healthy_window_scores_ok() -> None:
    rows = [_metrics(0.8, tokens=4_000_000, telemetry="observed")]
    rows += _healthy_rows()[1:]
    card = evaluate(rows, _supervisor(), _git(), window_hours=24,
                    upstream="main", now=NOW)

    assert set(card.dimensions) == set(DIMENSIONS)
    assert card.status == OK, [flag.as_dict() for flag in card.flags
                               if flag.status == BREACH]
    assert card.dimensions["yield"]["merges_per_day"] == 12.0
    assert card.dimensions["yield"]["pass_rate"] == 1.0


def test_missing_token_telemetry_is_unmeasurable_not_zero() -> None:
    card = evaluate(_healthy_rows(), _supervisor(), _git(), window_hours=24,
                    upstream="main", now=NOW)

    assert card.dimensions["efficiency"]["tokens_per_merge"] is None
    assert "tokens_per_merge" in card.dimensions["observability"]["unmeasurable"]
    flag = next(f for f in card.flags if f.name == "token_telemetry")
    assert flag.status == WATCH
    assert "unmeasurable" in flag.detail


def test_observed_telemetry_yields_tokens_per_merge() -> None:
    rows = [_metrics(0.8, tokens=1_000_000, telemetry="observed")]
    rows += _healthy_rows()[1:]
    card = evaluate(rows, _supervisor(), _git(), window_hours=24, upstream="main",
                    now=NOW)

    assert card.dimensions["efficiency"]["tokens_per_merge"] > 0
    assert not [f for f in card.flags if f.name == "token_telemetry"]


def test_framework_free_violation_is_a_correctness_breach() -> None:
    card = evaluate(_healthy_rows(), _supervisor(), _git(violations=1),
                    window_hours=24, upstream="main", now=NOW)

    assert card.status == BREACH
    assert card.dimensions["correctness"]["framework_free_violations"] == 1
    assert any(f.name == "framework_free_violations" for f in card.breaches)


def test_stall_churn_and_zero_yield_are_reported_per_dimension() -> None:
    rows = [_metrics(0.2)] + [_row("DIRECTION_REJECTED",
                                  reason="selection quota already filled")
                             for _ in range(20)]
    card = evaluate(rows, _supervisor(segments=10, stalls=9), _git(merges=0),
                    window_hours=24, upstream="main", now=NOW)

    assert card.status == BREACH
    names = {flag.name for flag in card.breaches}
    assert "stall_segment_ratio" in names
    assert "merges_per_day" in names
    assert "pass_rate" in names
    assert "concurrency_slot_utilization" in names


def test_probe_file_left_behind_fails_hygiene() -> None:
    card = evaluate(_healthy_rows(), _supervisor(), _git(), window_hours=24,
                    probe_files=1, now=NOW)

    flag = next(f for f in card.flags if f.name == "probe_files_remaining")
    assert flag.status == BREACH
    assert card.status == BREACH


def test_upstream_divergence_and_stale_base_are_integration_signals() -> None:
    card = evaluate(_healthy_rows(), _supervisor(),
                    _git(ahead=94, behind=20, age=8.5), window_hours=120,
                    upstream="main", now=NOW)

    integration = card.dimensions["integration"]
    assert integration["commits_ahead"] == 94
    assert integration["commits_behind"] == 20
    flag = next(f for f in card.flags if f.name == "merge_base_age_days")
    assert flag.status == BREACH


def test_yield_halving_is_sustainability_signal() -> None:
    dates = ("2026-10-01",) * 8 + ("2026-10-05",) * 2
    git = _git(merges=10)
    git.merge_dates = dates
    card = evaluate(_healthy_rows(merges=10), _supervisor(), git,
                    window_hours=96, upstream="main", now=NOW)

    assert card.dimensions["sustainability"]["yield_second_half_ratio"] == 0.25
    flag = next(f for f in card.flags if f.name == "yield_second_half_ratio")
    assert flag.status in (WATCH, BREACH)


def test_supervisor_log_parsing_counts_stalls_preflight_and_sweeps() -> None:
    text = "\n".join([
        "[SUP] segment #1 ended (no provider hits, merges=0) -> cooling 600s",
        "[SUP] segment #2 STALL BREAKER: 6 consecutive empty rounds -> terminating",
        "[SUP] preflight: unexpected probe reply -> cooling 600s: timeout",
        "[SUP] sweep: removed leaked agent probe packages/core/src/__probe_a.test.ts",
        "[SUP] sweep: preserved tracked probe candidate packages/core/src/__scratch_b.ts",
        "[SUP] status left=100s merges_this_segment=0 head=abc1234 merged_total=91",
    ])
    facts = collect_supervisor_facts(text)

    assert facts.segments == 1
    assert facts.stall_breaks == 1
    assert facts.preflight_failures == 1
    assert facts.sweeps_removed == 1
    assert facts.preserved_tracked == 1
    assert facts.status_lines == 1


def test_read_rows_skips_torn_lines(tmp_path: Path) -> None:
    path = tmp_path / "state.jsonl"
    path.write_text('{"status": "PASSED"}\n{"status": \n{"status": "MERGED"}\n',
                    encoding="utf-8")

    assert [row["status"] for row in read_rows(path)] == ["PASSED", "MERGED"]
    assert read_rows(tmp_path / "absent.jsonl") == []


def test_markdown_renders_every_dimension_and_flag() -> None:
    card = evaluate(_healthy_rows(), _supervisor(), _git(), window_hours=24,
                    upstream="main", now=NOW)
    markdown = render_markdown(card)

    for dimension in DIMENSIONS:
        assert f"## {dimension}" in markdown
    assert "## flags" in markdown
    assert "Campaign evaluation" in markdown
    assert isinstance(card, Scorecard)

def test_quota_drops_are_not_quality_failures() -> None:
    """A direction dropped because the per-module quota is full is not a
    quality signal; only quality rejections may lower the quality pass rate."""
    rows = [_metrics(0.8, tokens=1, telemetry="observed")]
    for _ in range(9):
        rows.append(_row("DIRECTION_REJECTED",
                         reason="rejected because the selection quota was already filled"))
    for _ in range(6):
        rows.append(_row("PASSED", elapsed=100.0))
    card = evaluate(rows, _supervisor(), _git(merges=6), window_hours=24,
                    upstream="main", now=NOW)

    yield_metrics = card.dimensions["yield"]
    assert yield_metrics["rejections_by_class"]["quota"] == 9
    assert yield_metrics["quality_pass_rate"] == 1.0
    assert yield_metrics["pass_rate"] < 0.5
    assert not [flag for flag in card.breaches if flag.dimension == "yield"]


def test_infrastructure_failures_are_separated_from_model_failures() -> None:
    infra = [_row("ANALYSIS_FAILED", reason="agent binary not found")
             for _ in range(3)]
    content = [_row("ANALYSIS_FAILED", reason="agent produced no output")
               for _ in range(2)]
    rows = [_metrics(0.8, tokens=1, telemetry="observed")]
    rows += [_row("ANALYZED")] * 10 + infra + content
    rows += [_row("PASSED", elapsed=100.0)] * 6
    card = evaluate(rows, _supervisor(), _git(merges=6), window_hours=24,
                    upstream="main", now=NOW)

    reliability = card.dimensions["reliability"]
    assert reliability["analysis_failures_by_class"] == {"content": 2, "infra": 3}
    assert reliability["analysis_infra_failure_ratio"] == 0.2
    flag = next(f for f in card.flags if f.name == "analysis_infra_failure_ratio")
    assert flag.status == BREACH


def test_scorecard_trend_compares_two_windows() -> None:
    from pbatch.campaign_evaluation import compare

    first_rows = [_metrics(0.8, tokens=1, telemetry="observed")]
    first_rows += _healthy_rows(merges=10)[1:]
    first = evaluate(first_rows, _supervisor(), _git(merges=10),
                     window_hours=24, upstream="main", now=NOW)
    second = evaluate(_healthy_rows(merges=4),
                      _supervisor(segments=10, stalls=8), _git(merges=4),
                      window_hours=24, upstream="main", now=NOW)
    trend = compare(first, second)

    assert trend["yield/merges_per_day"]["delta"] == pytest_approx(-6.0)
    assert trend["status"]["from"] == OK
    assert trend["status"]["to"] in (WATCH, BREACH)


def pytest_approx(value: float):
    import pytest
    return pytest.approx(value, abs=0.001)
