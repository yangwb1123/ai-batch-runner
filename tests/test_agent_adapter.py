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
