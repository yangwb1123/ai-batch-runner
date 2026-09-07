"""Tests for the canonical Pi CLI passthrough boundary."""

from __future__ import annotations

import sys

from pbatch import config, pi_cli


def _capture_runner(monkeypatch, returncode=0):
    calls = []

    def fake_run(command, env):
        calls.append((list(command), dict(env)))
        return returncode

    monkeypatch.setattr(pi_cli, "_run_pi", fake_run)
    return calls


def test_explicit_binary_and_argv_are_forwarded_verbatim(monkeypatch):
    calls = _capture_runner(monkeypatch)
    monkeypatch.setenv("PBATCH_PI_BIN", "/opt/pi custom/bin/pi-edge")

    assert pi_cli.pi_main(["--mode", "rpc", "", "@a file.md"]) == 0
    assert calls[0][0] == [
        "/opt/pi custom/bin/pi-edge", "--mode", "rpc", "", "@a file.md"]


def test_leading_separator_is_also_forwarded_verbatim(monkeypatch):
    calls = _capture_runner(monkeypatch)
    monkeypatch.setattr(config, "AGENT_BIN", "pi")

    assert pi_cli.pi_main(["--", "--", "--mode", "json"]) == 0
    assert calls[0][0] == ["pi", "--", "--", "--mode", "json"]


def test_configured_pi_path_is_used(monkeypatch):
    calls = _capture_runner(monkeypatch)
    monkeypatch.delenv("PBATCH_PI_BIN", raising=False)
    monkeypatch.setattr(config, "AGENT_BIN", "/tools/pi")

    assert pi_cli.pi_main(["auth", "login"]) == 0
    assert calls[0][0] == ["/tools/pi", "auth", "login"]


def test_non_pi_agent_config_is_never_given_pi_arguments(monkeypatch):
    calls = _capture_runner(monkeypatch)
    monkeypatch.delenv("PBATCH_PI_BIN", raising=False)
    monkeypatch.setattr(config, "AGENT_BIN", "/tools/codex")

    assert pi_cli.pi_main(["--mode", "rpc"]) == 0
    assert calls[0][0] == ["pi", "--mode", "rpc"]


def test_environment_is_inherited_and_attribution_is_non_destructive(monkeypatch):
    calls = _capture_runner(monkeypatch)
    monkeypatch.setenv("PI_TEST_PARENT", "present")
    monkeypatch.setenv("PBATCH_PI_LAUNCHER", "outer-launcher")

    assert pi_cli.pi_main([]) == 0
    child_env = calls[0][1]
    assert child_env["PI_TEST_PARENT"] == "present"
    assert child_env["PBATCH_PI_LAUNCHER"] == "outer-launcher"


def test_default_attribution_is_namespaced(monkeypatch):
    calls = _capture_runner(monkeypatch)
    monkeypatch.delenv("PBATCH_PI_LAUNCHER", raising=False)

    assert pi_cli.pi_main([]) == 0
    assert calls[0][1]["PBATCH_PI_LAUNCHER"] == "pi-batch"


def test_process_exit_codes_are_preserved_or_signal_normalized(monkeypatch):
    _capture_runner(monkeypatch, returncode=7)
    assert pi_cli.pi_main([]) == 7

    _capture_runner(monkeypatch, returncode=-9)
    assert pi_cli.pi_main([]) == 137


def test_missing_binary_is_a_clear_usage_failure(monkeypatch, capsys):
    def missing(_command, _env):
        raise FileNotFoundError

    monkeypatch.setattr(pi_cli, "_run_pi", missing)
    monkeypatch.setenv("PBATCH_PI_BIN", "/missing/pi")

    assert pi_cli.pi_main([]) == 2
    error = capsys.readouterr().err
    assert "Pi executable not found" in error
    assert "PBATCH_PI_BIN" in error


def test_keyboard_interrupt_maps_to_shell_sigint_status(monkeypatch):
    def interrupted(_command, _env):
        raise KeyboardInterrupt

    monkeypatch.setattr(pi_cli, "_run_pi", interrupted)
    assert pi_cli.pi_main([]) == 130


def test_none_argv_uses_process_arguments(monkeypatch):
    calls = _capture_runner(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["pi-batch", "--print", "hello"])

    assert pi_cli.pi_main() == 0
    assert calls[0][0][-2:] == ["--print", "hello"]
