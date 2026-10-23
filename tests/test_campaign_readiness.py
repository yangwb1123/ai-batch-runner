"""Campaign admission must not spend provider budget on a not_ready agent.

The operator declares readiness per agent; a not_ready probe refuses the
campaign before module analysis, model calls or pipeline spawns.  Unknown
readiness (unconfigured, invalid, unmeasurable) can only warn -- it never
becomes an implicit "ready" and it adds no bypass flag.  Provider-breaker and
budget admission stay in front of every spend decision.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import pytest

from pbatch import agent_readiness, campaign_exec, config
from pbatch.campaign_models import CampaignSettings
from pbatch.campaign_state import StateStore
from pbatch.pipeline_status import PipelineStatus


def _context(tmp_path, agent="probeagent"):
    return SimpleNamespace(
        root=tmp_path,
        settings=CampaignSettings(summary_file="summary.md"),
        args=SimpleNamespace(dry_run=False, modules="", agent_bin=agent),
        store=StateStore(tmp_path / "state.jsonl"),
        snapshot={},
    )


def _configure(monkeypatch, value):
    monkeypatch.setattr(config, "_AGENT_CFG", {})
    monkeypatch.setattr(config, "_AGENTS_CFG",
                        {"probeagent": {"readiness_cmd": value}})


def _capture(monkeypatch, campaign=None):
    campaign = campaign or campaign_exec
    events, summaries = [], []
    monkeypatch.setattr(campaign, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(campaign, "discover_modules", lambda *_a: ["src"])
    monkeypatch.setattr(campaign, "_provider_breaker_admission",
                        lambda *_a: ("", []))
    monkeypatch.setattr(campaign, "_event",
                        lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(campaign, "write_summary",
                        lambda *args: summaries.append(args))
    return events, summaries


def _forbid_spend(monkeypatch, campaign=None):
    campaign = campaign or campaign_exec
    monkeypatch.setattr(campaign, "run_directions_isolated",
                        lambda *a, **k: pytest.fail("pipeline spawn"))
    monkeypatch.setattr(campaign, "run_direction",
                        lambda *a, **k: pytest.fail("pipeline spawn"))


def _allow_empty_round(monkeypatch, campaign=None):
    campaign = campaign or campaign_exec
    monkeypatch.setattr(campaign, "_preflight_validators", lambda *_a: None)
    monkeypatch.setattr(campaign, "_analyze_modules", lambda *_a: ([], 0))
    monkeypatch.setattr(campaign, "_select", lambda *_a: ([], 0))
    monkeypatch.setattr(campaign, "_implement", lambda *_a: [])
    monkeypatch.setattr(campaign, "_report_working_tree_residue", lambda *_a: None)


def test_campaign_refuses_not_ready_before_analysis_and_records_event(
        monkeypatch, tmp_path):
    _configure(monkeypatch, [
        sys.executable, "-c", "print('Not logged in'); raise SystemExit(1)"])
    events, summaries = _capture(monkeypatch)
    _forbid_spend(monkeypatch)
    monkeypatch.setattr(campaign_exec, "_analyze_modules",
                        lambda *_a: pytest.fail("analysis/model call must not start"))
    context = _context(tmp_path)

    assert campaign_exec.run_campaign(context) == 1
    assert summaries
    assert len(events) == 1
    args, kwargs = events[0]
    assert args[4] == "AGENT_NOT_READY"
    assert args[1] == "__readiness__"
    assert "probeagent" in kwargs["reason"]
    assert kwargs["reason"].startswith("agent readiness not_ready")
    evidence = args[6]
    assert any("agent=probeagent" in item for item in evidence)
    assert any("command=" in item for item in evidence)
    assert any("exit_code=1" in item for item in evidence)
    assert all(len(item) <= 240 for item in evidence)
    assert len(kwargs["reason"]) <= 300


def test_refusal_evidence_is_redacted(monkeypatch, tmp_path):
    _configure(monkeypatch, [
        sys.executable, "-c",
        "print('token=supersecret123'); raise SystemExit(2)"])
    events, _summaries = _capture(monkeypatch)
    _forbid_spend(monkeypatch)
    monkeypatch.setattr(campaign_exec, "_analyze_modules",
                        lambda *_a: pytest.fail("analysis must not start"))
    assert campaign_exec.run_campaign(_context(tmp_path)) == 1
    rendered = repr(events)
    assert "supersecret123" not in rendered
    assert "<redacted>" in rendered


def test_campaign_unconfigured_readiness_warns_once_and_runs(
        monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(config, "_AGENT_CFG", {})
    monkeypatch.setattr(config, "_AGENTS_CFG", {})
    events, summaries = _capture(monkeypatch)
    _forbid_spend(monkeypatch)
    _allow_empty_round(monkeypatch)
    monkeypatch.setattr(agent_readiness, "run_bounded",
                        lambda *a, **k: pytest.fail("no probe is configured"))
    caplog.set_level(logging.WARNING, logger="pi-batch")

    assert campaign_exec.run_campaign(_context(tmp_path)) == 0
    warnings = [record for record in caplog.records
                if "readiness unknown" in record.getMessage()]
    assert len(warnings) == 1
    assert "no readiness_cmd configured" in warnings[0].getMessage()
    assert summaries
    assert not [event for event in events if event[0][4] == "AGENT_NOT_READY"]


def test_invalid_readiness_config_is_unknown_not_a_refusal(
        monkeypatch, tmp_path, caplog):
    _configure(monkeypatch, [])
    events, _summaries = _capture(monkeypatch)
    _forbid_spend(monkeypatch)
    _allow_empty_round(monkeypatch)
    caplog.set_level(logging.WARNING, logger="pi-batch")

    assert campaign_exec.run_campaign(_context(tmp_path)) == 0
    warnings = [record.getMessage() for record in caplog.records
                if "readiness unknown" in record.getMessage()]
    assert len(warnings) == 1
    assert "invalid readiness configuration" in warnings[0]
    assert not [event for event in events if event[0][4] == "AGENT_NOT_READY"]


def test_provider_breaker_admission_still_wins(monkeypatch, tmp_path):
    _configure(monkeypatch, [
        sys.executable, "-c", "raise SystemExit(1)"])
    events, summaries = _capture(monkeypatch)
    monkeypatch.setattr(
        campaign_exec, "_provider_breaker_admission",
        lambda *_a: ("provider breaker active; cooldown 30.0s", ["state.json"]))
    monkeypatch.setattr(campaign_exec, "_analyze_modules",
                        lambda *_a: pytest.fail("analysis must not start"))

    assert campaign_exec.run_campaign(_context(tmp_path)) == 1
    assert summaries
    statuses = [event[0][4] for event in events]
    assert PipelineStatus.PROVIDER_BREAKER_PAUSED in statuses
    assert "AGENT_NOT_READY" not in statuses


def test_ready_probe_does_not_add_an_event_or_block(monkeypatch, tmp_path):
    _configure(monkeypatch, [sys.executable, "-c", "raise SystemExit(0)"])
    events, summaries = _capture(monkeypatch)
    _forbid_spend(monkeypatch)
    _allow_empty_round(monkeypatch)

    assert campaign_exec.run_campaign(_context(tmp_path)) == 0
    assert summaries
    assert not [event for event in events
                if event[0][4] in {"AGENT_NOT_READY", "PROVIDER_BREAKER_PAUSED"}]


def test_refusal_writes_bounded_summary_and_event(monkeypatch, tmp_path):
    _configure(monkeypatch, [
        sys.executable, "-c", "print('Not logged in'); raise SystemExit(1)"])
    monkeypatch.setattr(campaign_exec, "_start_heartbeat", lambda: None)
    monkeypatch.setattr(campaign_exec, "discover_modules", lambda *_a: ["src"])
    monkeypatch.setattr(campaign_exec, "_provider_breaker_admission",
                        lambda *_a: ("", []))
    monkeypatch.setattr(campaign_exec, "_analyze_modules",
                        lambda *_a: pytest.fail("analysis must not start"))
    context = _context(tmp_path)

    assert campaign_exec.run_campaign(context) == 1
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "AGENT_NOT_READY" in summary and "probeagent" in summary
    lines = (tmp_path / "state.jsonl").read_text(encoding="utf-8").splitlines()
    assert any("AGENT_NOT_READY" in line for line in lines)
    assert all(len(line.encode("utf-8")) < 4096 for line in lines)


def test_budget_exhaustion_still_stops_before_readiness(monkeypatch, tmp_path):
    _configure(monkeypatch, [sys.executable, "-c", "raise SystemExit(1)"])
    events, summaries = _capture(monkeypatch)
    monkeypatch.setattr(campaign_exec, "_budget_exhausted", lambda *_a: True)
    monkeypatch.setattr(campaign_exec, "probe_readiness",
                        lambda *_a, **k: pytest.fail("budget gate must win"))

    assert campaign_exec.run_campaign(_context(tmp_path)) == 3
    assert not [event for event in events if event[0][4] == "AGENT_NOT_READY"]
