"""Deadline alarms independent of lease/artifact I/O in the coordinator thread."""

from __future__ import annotations

import threading
import time

from .campaign_budget import CampaignDeadlineExpired, CampaignGuardFailed


def _noop() -> None:
    return None


def _settle_monitor(thread, done, timeout: float = 3.0) -> bool:
    """Signal a monitor to finish and report whether it actually stopped."""
    done.set()
    if thread.ident is not None:
        try:
            thread.join(timeout=timeout)
        except RuntimeError:
            return False  # partial bootstrap; the done flag forbids monitor work
    return not thread.is_alive()


class SerialDeadlineGuard:
    """Campaign cutoff for in-process serial work: set a cancel event and stop children.

    The serial pipeline runs tasks in the coordinator process, so the deadline
    is enforced through the existing cancellation contract plus an owned
    kill of the process's registered agent children. It never grants a fresh
    window and must be joined before the caller publishes any outcome.
    """

    def __init__(self, deadline, event, stop=None, poll: float = 0.2):
        self.deadline, self.event = deadline, event
        self.stop_hook, self.poll = stop or _noop, max(0.01, poll)
        self.budget_exhausted = False
        self.failed = False
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._monitor, daemon=True,
                                        name="campaign-serial-deadline")

    @property
    def stop_requested(self) -> bool:
        return self.budget_exhausted

    def stop(self) -> None:
        self.event.set()
        try:
            self.stop_hook()
        except Exception:
            self.failed = True

    def _monitor(self):
        try:
            while not self._done.is_set():
                if self.deadline.expired(time.monotonic()):
                    self.budget_exhausted = True
                    self.stop()
                    self._done.wait(self.poll)  # catch a spawn that raced the event
                    continue
                self._done.wait(min(1.0, max(0.01, self.deadline.remaining(time.monotonic()))))
        except BaseException:
            self.failed = True
            try:
                self.stop()
            except BaseException:
                pass

    def _finish(self):
        joined = _settle_monitor(self._thread, self._done)
        self.failed = self.failed or not joined
        return joined

    def __enter__(self):
        try:
            self._thread.start()
        except BaseException as exc:
            try:
                self.stop()
            finally:
                self._finish()
            if isinstance(exc, Exception):
                raise CampaignGuardFailed("campaign serial monitor could not start") from exc
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc is not None:
                self.stop()
        except Exception:
            self.failed = True
        finally:
            joined = self._finish()
        failed = self.failed or not joined
        if failed and isinstance(exc, CampaignDeadlineExpired):
            exc.args = (str(exc) + "; serial deadline monitor did not settle",)
        elif (failed and exc is not None and not isinstance(exc, Exception)
              and self.event.is_set()):
            return False  # observed cancellation retains its original identity/args
        elif failed:
            raise CampaignGuardFailed("campaign serial monitor did not settle") from exc
        return False


class ExecutionGuard:
    def __init__(self, child, started: float, timeout: int, deadline):
        self.child, self.deadline = child, deadline
        self.pipeline_stop = started + timeout if timeout > 0 else None
        self.budget_exhausted = False
        self.timed_out = False
        self.failed = False
        self._done = threading.Event()
        self._stop_lock = threading.Lock()
        self._stop_requested = False
        self._thread = threading.Thread(target=self._monitor, daemon=True,
                                        name="campaign-deadline")

    def __enter__(self):
        try:
            self._thread.start()
        except BaseException as exc:
            try:
                self.stop()
            finally:
                self._finish()
            if isinstance(exc, Exception):
                raise CampaignGuardFailed("campaign deadline monitor could not start") from exc
            raise
        return self

    @property
    def stop_requested(self):
        with self._stop_lock:
            return self._stop_requested

    def stop(self):
        with self._stop_lock:
            if self._stop_requested:
                return
            self._stop_requested = True
        self.child.stop()

    def _monitor(self):
        try:
            while not self._done.is_set():
                now = time.monotonic()
                if self.deadline.expired(now):
                    self.budget_exhausted = True
                elif self.pipeline_stop is not None and now >= self.pipeline_stop:
                    self.timed_out = True
                else:
                    delay = min(1.0, self.deadline.remaining(now))
                    if self.pipeline_stop is not None:
                        delay = min(delay, max(0.0, self.pipeline_stop - now))
                    self._done.wait(delay)
                    continue
                self.stop()
                return
        except BaseException:
            self.failed = True
            try:
                self.stop()
            except BaseException:
                pass  # caller reports required guard failure, never a successful pipeline

    def _finish(self):
        joined = _settle_monitor(self._thread, self._done)
        if not joined:
            self.failed = True
        return joined

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc is not None:
                self.stop()
        except Exception:
            self.failed = True
        finally:
            joined = self._finish()
        failed = self.failed or not joined
        if failed and isinstance(exc, CampaignDeadlineExpired):
            exc.args = (str(exc) + "; deadline monitor did not settle",)
        elif (failed and exc is not None and not isinstance(exc, Exception)
              and self.child.returncode is not None):
            return False  # observed termination: cancellation retains its original identity/args
        elif failed:
            raise CampaignGuardFailed("campaign deadline monitor did not settle") from exc
        return False
