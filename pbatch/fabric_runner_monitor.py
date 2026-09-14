"""Heartbeat and server-cancel watcher for one active Fabric task."""

from __future__ import annotations

import threading
import time

from .fabric import local_probe


class TaskMonitor:
    """Keep a single active task visible and observe cancellation requests."""

    def __init__(self, client, task_id: str, cancel_event: threading.Event,
                 interval: float = 5.0):
        self.client = client
        self.task_id = task_id
        self.cancel_event = cancel_event
        self.interval = max(0.25, min(float(interval), 30.0))
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run,
                                       name=f"fabric-task-{task_id[:12]}",
                                       daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5.5)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            self._heartbeat()
            self._observe_cancel()
            self.stop_event.wait(self.interval)

    def _heartbeat(self) -> None:
        heartbeat = getattr(self.client, "heartbeat", None)
        if not callable(heartbeat):
            return
        state = local_probe()["dynamic_state"]
        state["running_tasks"] = 1
        try:
            heartbeat(state, [self.task_id])
        except Exception:
            # The execution result remains authoritative; the main runner loop
            # retries connection failures after this bounded task attempt.
            return

    def _observe_cancel(self) -> None:
        status = getattr(self.client, "task_status", None)
        if not callable(status):
            return
        try:
            result = status(self.task_id)
        except Exception:
            return
        task = result.get("task") if isinstance(result, dict) else None
        if isinstance(task, dict) and task.get("cancel_requested") is True:
            self.cancel_event.set()
