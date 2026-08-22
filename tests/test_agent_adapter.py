"""Agent argv/session adapter regression tests."""

from __future__ import annotations

import pytest

from pbatch import config


def _adapter_config(monkeypatch) -> None:
    monkeypatch.setattr(config, "_AGENT_CFG", {
        "argument_flags": {"prompt": "--prompt", "model": ["-m"]},
        "session_flags": {"start": ["--session", "{session}"]},
    })
    monkeypatch.setattr(config, "_AGENTS_CFG", {
        "codex": {
            "argument_flags": {"prompt": ""},
            "session_flags": {"start": ["resume", "{session}", "{name}"]},
        },
    })


def test_agent_argv_merges_global_and_per_agent_flags(monkeypatch):
    _adapter_config(monkeypatch)
    assert config.build_agent_argv(
        "pi", "do work", model="large", session_flags=["--fresh"],
    ) == ["pi", "--prompt", "do work", "-m", "large", "--fresh"]
    assert config.build_agent_argv("codex", "do work", model="large") == [
        "codex", "do work", "-m", "large",
    ]
    assert config.agent_prompt_index("pi") == 2
    assert config.agent_prompt_index("codex") == 1


def test_agent_extra_args_appended_after_session_flags(monkeypatch):
    _adapter_config(monkeypatch)
    monkeypatch.setattr(config, "_AGENTS_CFG", {
        "codex": {
            "argument_flags": {"prompt": ["exec"]},
            "extra_args": ["--approve-for-me", "-s", "workspace-write"],
        },
    })
    assert config.build_agent_argv(
        "codex", "do work", model="large",
    ) == ["codex", "exec", "do work", "-m", "large",
          "--approve-for-me", "-s", "workspace-write"]
    # Global fallback when the per-agent section omits extra_args.
    monkeypatch.setattr(config, "_AGENT_CFG", {"extra_args": ["--global"]})
    monkeypatch.setattr(config, "_AGENTS_CFG", {"codex": {}})
    assert config.build_agent_argv(
        "codex", "do work",
    ) == ["codex", "-p", "do work", "--global"]


def test_agent_extra_args_malformed_ignored(monkeypatch):
    monkeypatch.setattr(config, "_AGENTS_CFG", {
        "codex": {"extra_args": "not-a-list"},
    })
    monkeypatch.setattr(config, "_AGENT_CFG", {})
    assert config.agent_extra_args("codex") == ()


def test_stdout_banner_reason_line_anchored_only():
    from pbatch import runner
    # A LONG body that merely QUOTES failure-signature words is not rejected
    # (codex transcript self-match lesson)...
    long_body = [
        "The rejection table in AGENTS.md covers `insufficient_quota`, `rate_limit_error`",
        "and `quota_exceeded`; the runner's anti-misfire discipline keeps bodies intact.",
        "This paragraph is a doc quote embedded in a normal multi-line analysis:",
        "it explains how signatures are matched per stream and why short bodies may",
        "be treated as error reports. It must not be mistaken for a provider reply.",
        "The remaining paragraphs only exist to push this body well past the",
        "eight-line short-body threshold so the test exercises the long-body path.",
        "Line eight keeps the block readable while still staying under the boundary.",
        "Line nine is the first line beyond the threshold: long bodies are safe.",
    ]
    assert runner._stdout_banner_reason("\n".join(long_body) + "\n") == ""
    # ...but a line-anchored CLI banner stays rejected...
    assert runner._stdout_banner_reason("ERROR: insufficient_quota") != ""
    assert runner._stdout_banner_reason("fatal: something broke") != ""
    assert runner._stdout_banner_reason("a thoughtful review of errors") == ""
    # ...and a SHORT body that IS a provider error is rejected, not treated
    # as a normal answer (offline-agent product decision).
    assert runner._stdout_banner_reason(
        "requests.exceptions.ConnectionError: Max retries exceeded",
    ) != ""


def test_agent_argv_rejects_value_for_disabled_capability(monkeypatch):
    monkeypatch.setattr(config, "_AGENT_CFG", {
        "argument_flags": {"provider": ""},
    })
    monkeypatch.setattr(config, "_AGENTS_CFG", {})
    with pytest.raises(ValueError, match="does not support provider"):
        config.build_agent_argv("agent", "task", provider="cloud")


def test_agent_session_flags_use_selected_binary(monkeypatch):
    _adapter_config(monkeypatch)
    assert config.agent_session_flags("start", "sid", "display", "pi") == [
        "--session", "sid",
    ]
    assert config.agent_session_flags(
        "start", "sid", "display", "codex",
    ) == ["resume", "sid", "display"]


def test_agent_preflight_checks_selected_executable(monkeypatch):
    monkeypatch.setattr(config.shutil, "which", lambda name: "/bin/ok"
                        if name == "ok-agent" else None)
    assert config.agent_preflight_errors("ok-agent") == []
    assert config.agent_preflight_errors("missing") == [
        "agent executable not found or not executable: missing",
    ]
