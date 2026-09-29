"""TUI startup options stay separate from task and ordinary request budgets."""

from __future__ import annotations

import asyncio
import threading

import pytest

from pbatch.tui import PiBatchTUI, TuiOptions, _build_parser, _options_from_args
from pbatch.tui_adapter_appserver import CodexAppServerAdapter
from test_tui_appserver_startup import StartupNotificationTransport


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", str(threading.TIMEOUT_MAX * 2)])
def test_startup_option_rejects_invalid_budget_before_mount(value):
    parser = _build_parser()
    with pytest.raises(SystemExit) as error:
        _options_from_args(parser, parser.parse_args(
            ["--runtime", "app-server", "--startup-timeout", value]))
    assert error.value.code == 2


def test_orchestrator_only_requires_managed_pi_rpc_and_disallows_native_replay():
    parser = _build_parser()
    options = _options_from_args(parser, parser.parse_args([
        "--orchestrator-only", "--model", "controller-model"]))
    assert options.orchestrator_only is True
    assert options.model == "controller-model"
    for argv in (
            ["--orchestrator-only", "--runtime", "legacy"],
            ["--orchestrator-only", "--native-session", "session-1"]):
        with pytest.raises(SystemExit) as error:
            _options_from_args(parser, parser.parse_args(argv))
        assert error.value.code == 2


def test_persistent_pipelines_require_the_tool_free_controller():
    parser = _build_parser()
    options = _options_from_args(parser, parser.parse_args([
        "--orchestrator-only", "--persistent-pipelines"]))
    assert options.persistent_pipelines is True
    with pytest.raises(SystemExit) as error:
        _options_from_args(parser, parser.parse_args(["--persistent-pipelines"]))
    assert error.value.code == 2


def test_startup_option_requires_its_runtime_and_does_not_change_turn_timeout():
    parser = _build_parser()
    with pytest.raises(SystemExit):
        _options_from_args(parser, parser.parse_args(["--startup-timeout", "20"]))
    options = _options_from_args(parser, parser.parse_args(
        ["--runtime", "app-server", "--startup-timeout", "20", "--timeout", "7"]))
    assert options.startup_timeout == 20 and options.timeout == 7
    assert "startup_timeout" not in options.backend_kwargs()


@pytest.mark.parametrize("startup", [None, 0.5])
def test_mount_passes_only_explicit_startup_override(tmp_path, monkeypatch, startup):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    calls = []

    def factory(cwd, **kwargs):
        calls.append(kwargs)
        return CodexAppServerAdapter(cwd, transport=StartupNotificationTransport(), **kwargs)

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path), agent="codex", runtime="app-server",
                                    timeout=7, startup_timeout=startup), appserver_factory=factory)
        async with app.run_test(size=(100, 30)):
            assert app._adapter is not None and not app._runtime_blocked_reason
            assert calls == ([{}] if startup is None else [{"startup_timeout": startup}])

    asyncio.run(scenario())
