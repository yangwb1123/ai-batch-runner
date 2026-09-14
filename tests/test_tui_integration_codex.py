"""Real-codex TUI integration scenario: full core loop through the actual UI.

This is the "can I complete one core function in the TUI?" proof: the
Textual app is driven like a user (type a task, press enter), a REAL
codex process executes it through the structured backend path, and we
assert on the visible conversation, the timeline entries and the
multi-turn continuation (provider thread resume).

Skipped when codex is not installed (CI without it); slow by nature.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from textual.widgets import Input, Markdown

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_backend import TUIBackend
from pbatch import config

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None, reason="codex CLI not installed")


@pytest.fixture(autouse=True)
def _use_codex_account_defaults(monkeypatch):
    # This repository's default model is Pi-specific. A real Codex smoke test
    # should use the user's configured Codex model, without rewriting it.
    monkeypatch.setattr(config, "AGENT_DEFAULT_MODEL", "")


def _run(coro) -> None:
    import asyncio
    asyncio.run(coro)


async def _wait_until(predicate, pilot, timeout: float = 180.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached within "
                                 f"{timeout:.0f}s")
        await pilot.pause(0.25)


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                   check=True)
    (repo / "file.txt").write_text("content\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"],
                   check=True)
    return repo


def test_tui_core_loop_with_real_codex_two_turns(tmp_path: Path):
    repo = _git_repo(tmp_path)

    backend = TUIBackend(
        lambda kind, text: None, cwd=str(repo),
        agent="codex", memory_mode="off", session_id="ui-codex-e2e",
        timeout=150)

    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend=backend)
        async with app.run_test(size=(110, 32)) as pilot:
            # ── turn 1: user sends a real task ──────────────────────────
            composer = app.query_one("#composer", Input)
            composer.value = ("Remember the secret marker: KAPPA_7. "
                              "Reply with exactly: GOT_IT")
            await pilot.press("enter")
            await _wait_until(lambda: not app._busy, pilot)
            assert backend.snapshot()["status"] == "succeeded", backend.snapshot()
            source = app.query_one("#conversation", Markdown).source
            assert "GOT_IT" in app._runtime_view.assistant_text, source[-400:]
            # structured path engaged: the immutable runtime snapshot holds
            # typed command/file/usage cards from the real codex run
            timeline_kinds = [
                entry.kind for entry in app._runtime_view.timeline.entries]
            assert any(kind in timeline_kinds
                       for kind in ("command", "file", "usage")), \
                f"no structured timeline cards from the codex run: {timeline_kinds}"
            # status must be Ready (succeeded) after the turn
            snapshot = backend.snapshot()
            assert snapshot["status"] == "succeeded", snapshot

            # ── turn 2: continuation must remember the marker ───────────
            composer = app.query_one("#composer", Input)
            composer.value = "What marker did I ask you to remember? " \
                             "Reply with it."
            await pilot.press("enter")
            await _wait_until(lambda: not app._busy, pilot)
            source2 = app.query_one("#conversation", Markdown).source
            assert "KAPPA_7" in app._runtime_view.assistant_text, \
                f"turn2 recall failed; source={source2[-400:]}"
            assert backend.snapshot()["turn"] == 2

    _run(scenario())


def test_tui_timeline_view_is_toggleable_with_real_codex(tmp_path: Path):
    """Ctrl+T toggles the timeline; with the structured backend the
    timeline block contains command/failure cards after a real turn."""
    repo = _git_repo(tmp_path)
    backend = TUIBackend(lambda k, t: None, cwd=str(repo), agent="codex",
                         memory_mode="off", session_id="ui-codex-tl",
                         timeout=150)

    async def scenario():
        app = PiBatchTUI(TuiOptions(), backend=backend)
        async with app.run_test(size=(110, 32)) as pilot:
            composer = app.query_one("#composer", Input)
            composer.value = "List the files in this repo and reply: LISTED"
            await pilot.press("enter")
            await _wait_until(lambda: not app._busy, pilot)
            source = app.query_one("#conversation", Markdown).source
            assert "LISTED" in source, source[-300:]
            # /timeline visible source contains the timeline section
            await pilot.press("ctrl+t")
            await pilot.pause(0.3)
            toggled = app.query_one("#conversation", Markdown).source
            assert ("timeline" in toggled.lower()
                    or "command" in toggled.lower()), toggled[-400:]

    _run(scenario())
