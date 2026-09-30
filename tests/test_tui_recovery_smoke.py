"""Recovery acceptance waits for the rejected draft before sending erase keys."""

from collections import deque
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
SPEC = importlib.util.spec_from_file_location("recovery_smoke", TOOLS / "smoke-tui-recovery.py")
SMOKE = importlib.util.module_from_spec(SPEC)
sys.path.insert(0, str(TOOLS))
try:
    SPEC.loader.exec_module(SMOKE)
    from tui_soak_terminal import ComposerObservation
finally:
    sys.path.remove(str(TOOLS))


DRAFT = "THIS_UNKNOWN_TURN_MUST_NOT_EXECUTE"


def _frame(text):
    return ("\x1b[35;4H" + text.ljust(100)).encode()


class ScriptedTerminal:
    def __init__(self, frames):
        self.frames, self.sent = deque(frames), []
        self.composer, self.tail = ComposerObservation(), bytearray()
        self.process = SimpleNamespace(poll=lambda: None)
        self.pumps = 0

    def pump(self):
        assert self.frames, "unexpected poll after scripted UI frames"
        self.pumps += 1
        self.composer.feed(self.frames.popleft())

    def send(self, value):
        self.sent.append((value, self.pumps))


def _run(tmp_path, frames):
    run = SMOKE.RecoveryRun(["unused"], tmp_path)
    run.terminal = ScriptedTerminal(frames)
    run.processes = SimpleNamespace(poll=lambda: None)
    run.sessions = {"UNKNOWN": {"session_id": "saved-unknown"}}
    run.summary = lambda _state: {"read_only": True, "turn_state": "UNKNOWN"}
    run.visible = lambda _text: True  # The earlier /resume notice is already visible.
    run.controls = lambda _kind: []
    return run


def test_resume_notice_does_not_allow_erasing_before_rejected_draft_is_painted(tmp_path):
    empty = _frame("Ask pi-batch…")
    run = _run(tmp_path, [empty, empty, empty, _frame(DRAFT), empty])
    run.check_unknown("UNKNOWN")
    assert run.retained_input and run.terminal.pumps == 4
    run.send("/new")
    assert run.terminal.sent == [
        ("/resume saved-unknown", 0), (DRAFT, 1), (b"\x05\x15", 4), ("/new", 5)]
    assert not run.retained_input


def test_clear_acknowledgement_requires_composer_to_still_be_empty(tmp_path):
    empty = _frame("Ask pi-batch…")
    run = _run(tmp_path, [empty + _frame(DRAFT), empty])
    run.terminal.composer.feed(_frame(DRAFT))
    run.retained_input = True
    run.send("/new")
    assert run.terminal.sent == [(b"\x05\x15", 0), ("/new", 2)]


def test_provider_prompt_while_waiting_for_retained_draft_still_fails(tmp_path):
    empty = _frame("Ask pi-batch…")
    run = _run(tmp_path, [empty, empty, empty])
    run.controls = lambda kind: [{"message": DRAFT}] if (
        kind == "prompt" and run.terminal.pumps == 3) else []
    with pytest.raises(SMOKE.SoakFailure, match="accepted a turn before /new"):
        run.check_unknown("UNKNOWN")
    assert not run.results and not run.retained_input
