"""Explicit prompt-overflow policy: fail closed by default, bounded opt-in."""

from types import SimpleNamespace

from pbatch import campaign_cli, cli, config
from pbatch.pipeline import _task_prompt
from pbatch.prompt_budget import EVIDENCE_TRUNCATION_MARKER, fit_prompt_evidence


def test_within_budget_prompt_is_unchanged(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 4096)
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "truncate")
    rendered = "INSTRUCTIONS evidence INSTRUCTIONS-END"
    assert fit_prompt_evidence("evidence", rendered) == rendered


def test_default_policy_stays_fail_closed(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 256)
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "fail")
    evidence = "x" * 4096
    rendered = f"INSTRUCTIONS {evidence} INSTRUCTIONS-END"
    assert fit_prompt_evidence(evidence, rendered) == rendered
    assert len(rendered.encode()) > 256


def test_truncate_bounds_and_marks_evidence(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 4096)
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "truncate")
    evidence = "EVIDENCE-HEAD " + ("x" * 8000) + " EVIDENCE-TAIL"
    rendered = f"INSTRUCTIONS {evidence} INSTRUCTIONS-END"
    bounded = fit_prompt_evidence(evidence, rendered)
    assert len(bounded.encode()) <= 4096
    assert bounded.startswith("INSTRUCTIONS EVIDENCE-HEAD ")
    assert bounded.endswith(" EVIDENCE-TAIL INSTRUCTIONS-END")
    assert EVIDENCE_TRUNCATION_MARKER in bounded


def test_truncate_respects_utf8_boundaries(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 300)
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "truncate")
    evidence = "汉字" * 400
    rendered = f"指令 {evidence} 结尾"
    bounded = fit_prompt_evidence(evidence, rendered)
    assert len(bounded.encode()) <= 300
    bounded.encode("utf-8").decode("utf-8")
    assert EVIDENCE_TRUNCATION_MARKER in bounded


def test_overhead_over_limit_is_left_for_the_runner(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 64)
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "truncate")
    evidence = "x" * 512
    rendered = "I" * 128 + evidence
    assert fit_prompt_evidence(evidence, rendered) == rendered


def test_missing_evidence_placeholder_is_unchanged(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 64)
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "truncate")
    rendered = "INSTRUCTIONS WITHOUT PLACEHOLDER"
    assert fit_prompt_evidence("", rendered) == rendered


def test_task_prompt_applies_both_policies(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 4096)
    evidence = "EVIDENCE-HEAD " + ("x" * 8000) + " EVIDENCE-TAIL"
    task = {"prompt": "INSTRUCTIONS {input_content} INSTRUCTIONS-END"}
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "fail")
    oversized = _task_prompt(task, evidence, "in", "in.md")
    assert oversized == f"INSTRUCTIONS {evidence} INSTRUCTIONS-END"
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "truncate")
    bounded = _task_prompt(task, evidence, "in", "in.md")
    assert len(bounded.encode()) <= 4096
    assert bounded.startswith("INSTRUCTIONS EVIDENCE-HEAD ")
    assert bounded.endswith(" INSTRUCTIONS-END")
    assert EVIDENCE_TRUNCATION_MARKER in bounded


def test_batch_cli_wiring_applies_prompt_overflow(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "fail")
    args = SimpleNamespace(agent_bin="pi", thinking="max", memory_mode="auto",
                           stream_output="auto", prompt_overflow="truncate")
    cli._wire_agent(args)
    assert config.PROMPT_OVERFLOW == "truncate"
    args.prompt_overflow = ""
    cli._wire_agent(args)
    assert config.PROMPT_OVERFLOW == "truncate"  # empty keeps the current value


def test_campaign_cli_wiring_applies_prompt_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)
    monkeypatch.setenv("PBATCH_RATE_LIMIT_FILE", str(tmp_path / "rate.json"))
    monkeypatch.setenv("PBATCH_PROVIDER_BREAKER_FILE", str(tmp_path / "breaker.json"))
    args = SimpleNamespace(
        agent_bin="pi", thinking="max", memory_mode="auto", stream_output="auto",
        prompt_overflow="truncate", events_file="", webhook="", budget_max=0,
        daily_budget=0, daily_state=str(tmp_path / "daily.json"),
        rate_limit=0, rate_burst=0)
    campaign_cli._wire_runtime(args)
    assert config.PROMPT_OVERFLOW == "truncate"
