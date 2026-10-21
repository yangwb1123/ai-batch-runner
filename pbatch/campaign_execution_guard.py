"""Deadline alarm independent of lease/artifact I/O in the coordinator thread."""

from __future__ import annotations

import threading
import time

from .campaign_budget import CampaignDeadlineExpired, CampaignGuardFailed


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
        self._done.set()
        if self._thread.ident is not None:
            try:
                self._thread.join(timeout=3)
            except RuntimeError:
                self.failed = True  # partial bootstrap; the done flag forbids monitor work
        return not self._thread.is_alive()

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
