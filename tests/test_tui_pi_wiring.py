"""TUI routing contracts for the canonical Pi RPC runtime."""

from __future__ import annotations

import asyncio

from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_runtime import RuntimeSession


class _FakePiRpc:
    def __init__(self) -> None:
        self.initialized = 0
        self.started = []
        self.closed = False

    def initialize(self, _request):
        self.initialized += 1

    def start_session(self, request):
        self.started.append(dict(request))
        return RuntimeSession(
            "ses_pi_rpc", "/tmp/pi-session.jsonl",
            str(request.get("workspace_id", "")), "2026-08-26T00:00:00Z",
            "ACTIVE")

    def close(self):
        self.closed = True


def _run(coro) -> None:
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(
                *pending, return_exceptions=True))
        loop.close()


def test_auto_uses_pi_rpc_for_managed_pi_backend(tmp_path):
    async def scenario():
        adapter = _FakePiRpc()
        calls = []

        def factory(cwd, **kwargs):
            calls.append((cwd, kwargs))
            return adapter

        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path), agent="pi", session_name="work"),
            pi_rpc_factory=factory)
        async with app.run_test(size=(100, 30)):
            assert app._adapter is adapter
            assert app._strict_adapter is True
            assert adapter.initialized == 1
            assert adapter.started[0]["name"] == "work"
            assert calls[0][0] == str(tmp_path.resolve())
            assert calls[0][1]["agent"] == "pi"
        assert adapter.closed is True

    _run(scenario())


def test_explicit_pi_rpc_works_with_injected_backend(tmp_path):
    async def scenario():
        adapter = _FakePiRpc()

        class Backend:
            agent = "pi"
            provider = ""
            model = ""

            def set_emit(self, _emit):
                return None

            def run_turn(self, _prompt):
                return None

            def snapshot(self):
                return {}

        app = PiBatchTUI(
            TuiOptions(cwd=str(tmp_path), agent="pi", runtime="pi-rpc"),
            backend=Backend(), pi_rpc_factory=lambda _cwd, **_kw: adapter)
        async with app.run_test(size=(100, 30)):
            assert app._adapter is adapter
            assert not app._runtime_blocked_reason

    _run(scenario())


def test_explicit_pi_rpc_rejects_non_pi_agent(tmp_path):
    async def scenario():
        app = PiBatchTUI(TuiOptions(
            cwd=str(tmp_path), agent="codex", runtime="pi-rpc"))
        async with app.run_test(size=(100, 30)):
            assert app._adapter is None
            assert "canonical Pi executable" in app._runtime_blocked_reason
            assert app._start_turn("must not fall back") is False

    _run(scenario())
