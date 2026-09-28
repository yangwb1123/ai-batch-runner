"""Scoped operating-system shutdown signals for the interactive TUI."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import signal
import threading
from typing import Any


@dataclass
class SignalExit:
    signum: int = 0

    @property
    def return_code(self) -> int:
        return 128 + self.signum if self.signum else 0


def _exit_after_signal(app: Any, signum: int) -> None:
    """Use normal teardown; absence of provider completion remains UNKNOWN."""
    app.action_quit()
    # A native attachment observes a separately owned run. Its termination is
    # not ours to assert, even though quitting detaches the local observer.
    if app._native_pipeline_attachment is not None or not app._projection_driven:
        return
    app._apply_runtime_snapshot(app._event_controller.snapshot())
    app._reconcile_runtime_failure(
        f"TUI received {signal.Signals(signum).name}; provider completion is unconfirmed")


@contextmanager
def tui_signal_handlers(app: Any):
    """Restore all inherited handlers after this app, including failed startup."""
    result = SignalExit()
    previous = {}

    def request_exit(signum, _frame):
        if result.signum:
            return  # Repeated shutdown requests cannot interrupt cleanup.
        result.signum = signum
        app.call_later(_exit_after_signal, app, signum)

    try:
        if threading.current_thread() is threading.main_thread():
            for name in ("SIGTERM", "SIGHUP"):
                signum = getattr(signal, name, None)
                if signum is not None:
                    previous[signum] = signal.signal(signum, request_exit)
        yield result
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
