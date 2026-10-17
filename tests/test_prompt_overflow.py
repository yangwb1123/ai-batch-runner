"""Explicit prompt-overflow policy: fail closed by default, bounded opt-in."""

from types import SimpleNamespace

from pbatch import campaign_cli, campaign_runtime, cli, config
from pbatch.meta import _role_prompt
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


def test_truncate_reserves_space_for_the_prepended_note(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 4096)
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "truncate")
    evidence = "HEAD " + ("z" * 8192) + " TAIL"
    rendered = f"INSTRUCTIONS {evidence} END"
    note = "N" * 1024 + "\n"
    bounded = fit_prompt_evidence(evidence, rendered, reserve_bytes=len(note.encode()))
    assert len((note + bounded).encode()) <= 4096
    assert EVIDENCE_TRUNCATION_MARKER in bounded


def test_task_prompt_reserve_keeps_final_prompt_within_budget(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 4096)
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "truncate")
    evidence = "HEAD " + ("z" * 8192) + " TAIL"
    note = "N" * 1024 + "\n"
    task = {"prompt": "INSTRUCTIONS {input_content} END"}
    bounded = _task_prompt(task, evidence, "in", "in.md", "", reserve_bytes=len(note.encode()))
    assert len((note + bounded).encode()) <= 4096


def test_bound_prompt_is_the_final_argv_guard(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 2048)
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "fail")
    prompt = "HEAD " + ("q" * 8192) + " TAIL"
    assert config.bound_prompt(prompt) == prompt
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "truncate")
    bounded = config.bound_prompt(prompt)
    assert len(bounded.encode()) <= 2048
    assert bounded.startswith("HEAD ") and bounded.endswith(" TAIL")
    assert EVIDENCE_TRUNCATION_MARKER in bounded


def test_build_agent_argv_bounds_the_prompt(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 2048)
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "truncate")
    prompt = "HEAD " + ("q" * 8192) + " TAIL"
    cmd = config.build_agent_argv("pi", prompt)
    index = config.agent_prompt_index("pi")
    assert len(cmd[index].encode()) <= 2048
    assert max(len(value.encode()) for value in cmd[1:]) <= 2048
    assert EVIDENCE_TRUNCATION_MARKER in cmd[index]


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


def test_isolated_command_propagates_prompt_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "truncate")
    command = campaign_runtime.isolated_command(
        tmp_path, tmp_path / "pipeline.yaml", tmp_path / "run.log", ["--model", "m"])
    assert "--prompt-overflow" in command
    assert command[command.index("--prompt-overflow") + 1] == "truncate"
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "fail")
    default = campaign_runtime.isolated_command(
        tmp_path, tmp_path / "pipeline.yaml", tmp_path / "run.log", ["--model", "m"])
    assert "--prompt-overflow" not in default


def test_role_prompt_is_bounded_under_truncate(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 2048)
    combined = "CONTEXT-HEAD " + ("y" * 8192) + " CONTEXT-TAIL"
    item = {"task": "Review the context", "role": "reviewer"}
    stage = SimpleNamespace(role_dir="")
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "fail")
    oversized = _role_prompt(stage, item, combined)
    assert len(oversized.encode()) > 2048
    monkeypatch.setattr(config, "PROMPT_OVERFLOW", "truncate")
    bounded = _role_prompt(stage, item, combined)
    assert len(bounded.encode()) <= 2048
    assert bounded.startswith("Review the context")
    assert EVIDENCE_TRUNCATION_MARKER in bounded
