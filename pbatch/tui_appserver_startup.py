"""Finite App Server startup budgets, including transport writes and replies."""
from __future__ import annotations

import math
import queue
import threading
import time
from typing import Any, Callable, Optional

DEFAULT_TIMEOUT = 5.0
DEFAULT_STARTUP_TIMEOUT = 15.0


def _positive_timeout(value: Any, name: str) -> float:
    message = f"{name} must be a finite positive number <= {threading.TIMEOUT_MAX:g}"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(message)
    try:
        seconds = float(value)
    except OverflowError as exc:
        raise ValueError(message) from exc
    if not math.isfinite(seconds) or not 0 < seconds <= threading.TIMEOUT_MAX:
        raise ValueError(message)
    return seconds


def startup_timeouts(request_timeout: Optional[float], startup_timeout: Optional[float],
                     approval_ttl: float) -> tuple[float, float, float]:
    """Preserve an explicit request budget unless startup is separately configured."""
    request = DEFAULT_TIMEOUT if request_timeout is None else request_timeout
    startup = (DEFAULT_STARTUP_TIMEOUT if request_timeout is None else request)
    if startup_timeout is not None:
        startup = startup_timeout
    return (_positive_timeout(request, "request_timeout"),
            _positive_timeout(startup, "startup_timeout"),
            _positive_timeout(approval_ttl, "approval_ttl"))


def run_startup(operation: Callable[[], Any], close: Callable[[], None],
                timeout: float) -> Any:
    """Bound the entire handshake; closing owned pipes releases blocked writes."""
    results: queue.Queue[tuple[float, Any, Optional[Exception]]] = queue.Queue(maxsize=1)
    deadline = time.monotonic() + timeout

    def execute() -> None:
        try:
            value, error = operation(), None
        except Exception as exc:
            value, error = None, exc
        results.put((time.monotonic(), value, error))

    worker = threading.Thread(target=execute, name="codex-app-server-initialize", daemon=True)
    worker.start()
    try:
        completed, value, error = results.get(timeout=max(0, deadline - time.monotonic()))
        if completed > deadline:
            raise queue.Empty
    except queue.Empty as exc:
        close()
        worker.join(timeout=1.0)
        raise RuntimeError(f"initialize timed out after {timeout:g}s") from exc
    worker.join(timeout=1.0)
    if error is not None:
        raise error
    return value
