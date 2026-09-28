"""Coordinate CLI shutdown with child publication and queued command work."""

from __future__ import annotations

import threading


_LOCK = threading.RLock()
_STOPPING = False
_ACTIVE: dict = {}
_SPAWNING_THREAD = None
_DEFERRED_INTERRUPT = False


def shutdown_requested() -> bool:
    return _STOPPING


def spawn_registered(spawn, stop):
    """Publish each child before shutdown can finish, including spawn races."""
    global _SPAWNING_THREAD
    with _LOCK:
        if shutdown_requested():
            raise RuntimeError("process cancelled by CLI shutdown")
        _SPAWNING_THREAD = threading.get_ident()
        try:
            process = spawn()
            _ACTIVE[id(process)] = (process, stop)
        except BaseException:
            interrupt_after_spawn()
            raise
        finally:
            _SPAWNING_THREAD = None
        return process


def forget_process(process) -> None:
    with _LOCK:
        _ACTIVE.pop(id(process), None)


def interrupt_after_spawn() -> None:
    """Re-raise a main-thread signal only after its new child is owned."""
    global _DEFERRED_INTERRUPT
    if _DEFERRED_INTERRUPT and threading.current_thread() is threading.main_thread():
        _DEFERRED_INTERRUPT = False
        raise KeyboardInterrupt()


def shutdown_processes() -> bool:
    """Stop admission and reap registered groups before the CLI releases locks.

    Repeated signals cannot interrupt teardown. If a signal interrupts Popen on
    the main thread, defer its exception until the child has been registered;
    the caller's finally block then owns cleanup even in that narrow window.
    """
    global _DEFERRED_INTERRUPT, _STOPPING
    if shutdown_requested():
        return False
    _STOPPING = True
    with _LOCK:
        deferred = _SPAWNING_THREAD == threading.get_ident()
        _DEFERRED_INTERRUPT = deferred
        processes = tuple(_ACTIVE.values())
    for process, stop in processes:
        stop(process)
    return not deferred
