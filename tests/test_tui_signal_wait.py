"""The original PTY exit deadline and shutdown evidence checks remain intact."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def smoke(monkeypatch):
    tools = Path(__file__).resolve().parents[1] / "tools"
    monkeypatch.syspath_prepend(str(tools))
    spec = importlib.util.spec_from_file_location("owned_signal_wait", tools / "smoke-tui-signals.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Process:
    def __init__(self, states):
        self.states = iter(states)
        self.returncode = None
        self.calls = 0

    def poll(self):
        self.calls += 1
        self.returncode = next(self.states, self.returncode)
        return self.returncode


def test_exit_between_two_polls_is_not_a_false_early_exit_failure(smoke):
    process = Process([None, 0])
    terminal = SimpleNamespace(process=process, pump=lambda: None)
    # No timing lottery: first poll sees active, second sees successful exit.
    smoke.wait_for(terminal, lambda: process.poll() is not None, "TUI exit", timeout=1)
    assert process.returncode == 0 and process.calls == 2


def test_exit_before_required_non_exit_fact_still_refuses(smoke):
    process = Process([0])
    terminal = SimpleNamespace(process=process, pump=lambda: None)
    with pytest.raises(smoke.SoakFailure, match="before durable active provider output"):
        smoke.wait_for(terminal, lambda: False, "durable active provider output", timeout=1)


def test_wait_deadline_is_not_restarted_or_extended(smoke, monkeypatch):
    ticks = iter([0, 0.1, 1.1])
    monkeypatch.setattr(smoke, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    calls = []
    terminal = SimpleNamespace(process=Process([None]), pump=lambda: calls.append("pump"))
    with pytest.raises(smoke.SoakFailure, match="deadline waiting for ready composer"):
        smoke.wait_for(terminal, lambda: False, "ready composer", timeout=1)
    assert calls == ["pump"]


@pytest.mark.parametrize("code", [1, -9, 143])
def test_exit_observation_never_substitutes_expected_code_or_owned_evidence(smoke, tmp_path, code):
    process = Process([code])
    terminal = SimpleNamespace(process=process, pump=lambda: None)
    smoke.wait_for(terminal, lambda: process.poll() is not None, "TUI exit", timeout=1)
    with pytest.raises(smoke.SoakFailure, match="expected 0"):
        smoke.validate_shutdown(tmp_path, "quit", code, [])


def test_exit_without_two_owned_process_identities_still_refuses(smoke, tmp_path):
    with pytest.raises(smoke.SoakFailure, match="process evidence missing"):
        smoke.validate_shutdown(tmp_path, "quit", 0, [])
