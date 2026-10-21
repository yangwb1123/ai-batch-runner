"""Owned isolated-session handle: serialize observation, signals and reaping."""

from __future__ import annotations

import os
import signal
import subprocess
import threading

from .campaign_watchdog import process_start_ticks


class OwnedPipeline:
    """Wrap only a newly spawned start_new_session=True child, never an adopted PID.

    All poll/wait/stop access stays behind this handle's lock. A deadline stop
    signals the group before reaping its leader, including an already-zombie
    leader, so another observer cannot release/reuse its PID between signals.
    This does not contain descendants that escape the session/process group.
    """

    def __init__(self, child: subprocess.Popen):
        self._child = child
        self._lock = threading.RLock()
        self.start_ticks = None

    def capture_identity(self):
        """Only probe after registration; retain ownership if setup/stop fails."""
        try:
            with self._lock:
                if self._child.returncode is not None:
                    return
                ticks = process_start_ticks(self.pid)
                if self._child.returncode is None:
                    self.start_ticks = ticks or None
        except BaseException:
            self.stop()
            raise

    @property
    def pid(self) -> int:
        return self._child.pid

    @property
    def returncode(self) -> int | None:
        with self._lock:
            return self._child.returncode

    def poll(self):
        with self._lock:
            return self._child.poll()

    def stop(self) -> None:
        """Zero-grace TERM/KILL, followed by bounded direct-child reaping.

        Do not poll before signalling: a zombie still reserves the owned PID.
        A previously reaped leader is not authority to signal that PID again.
        """
        with self._lock:
            if self._child.returncode is not None:
                return
            try:
                os.killpg(self.pid, signal.SIGTERM)
                os.killpg(self.pid, signal.SIGKILL)
            except OSError:
                try:
                    self._child.kill()
                except OSError:
                    pass
            try:
                self._child.wait(timeout=2)
            except (subprocess.TimeoutExpired, ChildProcessError):
                pass  # termination unknown; caller must preserve the live/unknown lease
