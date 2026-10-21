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


class SpawnRefused(RuntimeError):
    """A caller-owned admission predicate refused the spawn under the lock."""

    def __init__(self, result):
        super().__init__("spawn refused before process creation")
        self.result = result


def spawn_registered(spawn, stop, admit=None):
    """Publish each child before shutdown can finish, including spawn races.

    ``admit`` runs under the same lock as the spawn, so a worker that waited
    for another worker's process creation re-evaluates its cutoff immediately
    before Popen instead of trusting a check made before the wait.
    """
    global _SPAWNING_THREAD
    with _LOCK:
        if shutdown_requested():
            raise RuntimeError("process cancelled by CLI shutdown")
        refused = admit() if admit is not None else None
        if refused is not None:
            raise SpawnRefused(refused)
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
