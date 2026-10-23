"""Bounded isolated dispatch; only the caller invokes lifecycle observers.

Workers signal attempt entry, never write campaign state. Completion is measured
before coordinator/observer I/O. Existing attempt supervision remains authoritative.
"""
from __future__ import annotations

import math
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from queue import Empty, Queue


def _interval(start, end):
    if start is None or end is None:
        return None
    value = end - start
    return value if math.isfinite(value) and 0 <= value <= 1e15 else None


def scheduling(queued_at, started_at=None, finished_at=None):
    """Unknown/unstarted intervals stay null, including refused admissions."""
    return {"queue_wait_seconds": _interval(queued_at, started_at),
            "execution_wall_seconds": _interval(started_at, finished_at)}


def publish_outcome(outcome, timing, on_outcome=None):
    outcome.scheduling = {**timing, "execution_status": outcome.status}
    if on_outcome is not None:
        on_outcome(outcome)
    return outcome


def _worker(slot, item, run, signals, allowed):
    def signal(started_at):
        signals.put_nowait((slot, item[0], started_at))
    outcome = run(item, signal, allowed)
    return outcome, time.monotonic()


def _drain_starts(signals, starts, queued_at, on_start):
    while True:
        try:
            slot, direction, started_at = signals.get_nowait()
        except Empty:
            return
        starts[slot] = started_at
        if on_start is not None:
            on_start(direction, scheduling(queued_at, started_at))


def _refill(pool, pending, active, jobs, run, signals, allowed, refuse, publish):
    while len(active) < jobs:
        try:
            slot, item = next(pending)
        except StopIteration:
            return
        if not allowed():
            publish(refuse(item), None, None)
            continue
        future = pool.submit(_worker, slot, item, run, signals, allowed)
        active[future] = slot


def run_dispatch(prepared, jobs, run, should_run, refuse, *, queued_at,
                 on_start=None, on_outcome=None):
    """Keep <= jobs submitted attempts, recheck admission before every submit.

    Starts are drained at most one polling interval after arrival (excluding
    coordinator I/O). An observer failure cancels unstarted futures, stops refill
    and propagates after existing in-flight supervision finishes. No replay/merge.
    """
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise ValueError("campaign dispatch jobs must be a positive integer")
    signals, starts, active = Queue(maxsize=jobs), {}, {}
    stopped, pending, outcomes = threading.Event(), iter(enumerate(prepared)), []
    def allowed():
        return not stopped.is_set() and should_run()
    def publish(outcome, started_at, finished_at):
        timing = scheduling(queued_at, started_at, finished_at)
        outcomes.append(publish_outcome(outcome, timing, on_outcome))
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        try:
            _refill(pool, pending, active, jobs, run, signals, allowed, refuse, publish)
            while active:
                _drain_starts(signals, starts, queued_at, on_start)
                done, _ = wait(active, timeout=0.05, return_when=FIRST_COMPLETED)
                _drain_starts(signals, starts, queued_at, on_start)
                for future in done:
                    slot = active.pop(future)
                    outcome, finished_at = future.result()
                    publish(outcome, starts.pop(slot, None), finished_at)
                _refill(pool, pending, active, jobs, run, signals, allowed, refuse, publish)
        except BaseException:
            stopped.set()
            for future in active:
                future.cancel()
            raise
    return outcomes
