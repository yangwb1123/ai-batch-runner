"""The Agent Hub control-plane environment stays outside provider processes."""

from __future__ import annotations

import io
import os
import subprocess

import pytest

from pbatch.provider_env import provider_child_env


class _FakeProcess:
    def __init__(self):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def test_provider_environment_copies_and_only_removes_hub_namespace():
    source = {
        "PBATCH_HUB_TOKEN": "hub-secret",
        "PBATCH_HUB_TOKEN_FILE": "/private/hub.token",
        "PBATCH_HUB_URL": "https://hub.example.test",
        "PBATCH_HUB_OAUTH_CONFIG": "/private/gateway-oauth.json",
        "PBATCH_HUB_USER_CONFIG": "/private/user-auth.json",
        "OPENAI_API_KEY": "provider-secret",
        "ANTHROPIC_API_KEY": "another-provider-secret",
        "PATH": "/usr/bin",
    }

    result = provider_child_env(source)

    assert result == {
        "OPENAI_API_KEY": "provider-secret",
        "ANTHROPIC_API_KEY": "another-provider-secret",
        "PATH": "/usr/bin",
    }
    assert source["PBATCH_HUB_TOKEN"] == "hub-secret"


@pytest.mark.parametrize("provider", [
    "legacy", "pi-rpc", "pi-version", "codex-exec", "codex-appserver",
])
def test_agent_session_provider_spawns_receive_filtered_env(monkeypatch, tmp_path, provider):
    from pbatch import runner, pi_version, tui_appserver_support
    from pbatch import tui_codex_support, tui_pi_rpc_support

    monkeypatch.setenv("PBATCH_HUB_TOKEN", "hub-secret")
    monkeypatch.setenv("PBATCH_HUB_TOKEN_FILE", "/private/hub.token")
    monkeypatch.setenv("PBATCH_HUB_URL", "https://hub.example.test")
    monkeypatch.setenv("PBATCH_HUB_OAUTH_CONFIG", "/private/gateway-oauth.json")
    monkeypatch.setenv("PBATCH_HUB_USER_CONFIG", "/private/user-auth.json")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "another-provider-secret")
    captured = []

    def fake_popen(argv, **kwargs):
        captured.append((list(argv), kwargs.get("env")))
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    if provider == "legacy":
        child_env = dict(os.environ)
        runner._spawn_agent(["agent"], str(tmp_path), child_env)
    elif provider == "pi-rpc":
        tui_pi_rpc_support._default_spawn(["pi", "--mode", "rpc"], str(tmp_path))
    elif provider == "pi-version":
        pi_version._default_spawn(["pi", "--version"], str(tmp_path))
    elif provider == "codex-exec":
        tui_codex_support._default_spawn(["codex", "exec", "--json"], str(tmp_path))
    else:
        transport = tui_appserver_support.DefaultTransport(cwd=str(tmp_path))
        transport.close()

    assert len(captured) == 1
    child_env = captured[0][1]
    assert child_env is not None
    assert "PBATCH_HUB_TOKEN" not in child_env
    assert "PBATCH_HUB_TOKEN_FILE" not in child_env
    assert "PBATCH_HUB_URL" not in child_env
    assert "PBATCH_HUB_OAUTH_CONFIG" not in child_env
    assert "PBATCH_HUB_USER_CONFIG" not in child_env
    assert child_env["OPENAI_API_KEY"] == "provider-secret"
    assert child_env["ANTHROPIC_API_KEY"] == "another-provider-secret"
    assert os.environ["PBATCH_HUB_TOKEN"] == "hub-secret"
