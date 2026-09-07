"""Capability- and identity-bound child-actor controls for the TUI."""

from __future__ import annotations

from typing import Any, Optional

from .tui_actor_tree import dispatch_actor_control
from .tui_view_support import clean_text

_ACTOR_ID_BYTES = 256
_ACTOR_TEXT_BYTES = 4096


class TUIActorControlMixin:
    """Expose controls only for the current typed actor tree."""

    def _init_actor_controls(self) -> None:
        self._actor_control_pending = False

    def _slash_actor_stop(self, args: tuple[str, ...]) -> None:
        if len(args) != 1:
            self._usage("actor-stop", "<actor-id>")
            return
        self._start_actor_control("stop", args[0])

    def _slash_actor_steer(self, args: tuple[str, ...]) -> None:
        if len(args) < 2:
            self._usage("actor-steer", "<actor-id> <text>")
            return
        self._start_actor_control("steer", args[0], " ".join(args[1:]))

    def _start_actor_control(self, action: str, actor_id: str,
                             text: str = "") -> None:
        if not self._busy or getattr(self, "_review_busy", False):
            self.notify("Actor control requires an active runtime turn.",
                        severity="warning")
            return
        runtime, native_control = self._actor_control_runtime(action)
        if runtime is False:
            return
        if self._actor_control_pending:
            self.notify("An actor control request is already pending.",
                        severity="warning")
            return
        target, turn_id, actor_tree = self._actor_control_target(actor_id)
        if target is None:
            self.notify("Actor control target is not an active typed actor.",
                        severity="warning")
            return
        value = _bounded_text(text)
        if action == "steer" and not value:
            self.notify("Actor steer text must not be empty.", severity="warning")
            return
        if runtime is None:
            self.notify("The active runtime is unavailable.", severity="warning")
            return
        view = getattr(self, "_runtime_view", None)
        scope = {name: getattr(view, name, "") for name in (
            "workspace_id", "session_id", "run_id", "turn_id", "actor_id")}
        self._actor_control_pending = True
        self._last_status = f"Sending actor {action} request"
        self._refresh_status()
        try:
            self.run_worker(
                lambda: self._actor_control_worker(
                    runtime, actor_tree, target, turn_id, action, value,
                    scope, bool(native_control)),
                name=f"actor-{action}", group="actor-control",
                exit_on_error=False, exclusive=True, thread=True)
        except Exception as exc:
            self._actor_control_pending = False
            self._last_status = f"Actor {action} failed"
            self.notify(f"Actor {action} failed: {exc}", severity="error")
            self._refresh_status()

    def _actor_control_runtime(self, action: str) -> tuple[Any, Any]:
        if not getattr(self, "_pipeline_busy", False):
            return getattr(self, "_adapter", None), False
        if action != "stop":
            self.notify("Native Pipeline actors do not support steering.",
                        severity="warning")
            return False, False
        channel = getattr(self, "_native_pipeline_approval", None)
        if not callable(getattr(channel, "request_actor_cancel", None)):
            self.notify("Native Pipeline actor control is unavailable.",
                        severity="warning")
            return False, False
        return channel, True

    def _actor_control_target(self, actor_id: str) -> tuple[Any, str, Any]:
        value = _bounded_text(actor_id, _ACTOR_ID_BYTES)
        view = getattr(self, "_runtime_view", None)
        actors = getattr(view, "actors", None)
        turn_id = str(getattr(view, "turn_id", "") or "")
        if not value or not turn_id or actors is None:
            return None, turn_id, actors
        target = next((node for node in actors.nodes
                       if node.actor_id == value and node.current
                       and not node.terminal), None)
        return target, turn_id, actors

    def _actor_control_worker(self, runtime: Any, actor_tree: Any,
                              snapshot: Any, turn_id: str, action: str,
                              text: str, scope: dict[str, Any],
                              native_control: bool) -> None:
        error: Optional[Exception] = None
        try:
            if native_control:
                if not runtime.request_actor_cancel(
                        scope, actor_id=snapshot.actor_id):
                    raise RuntimeError("native actor is no longer active")
            else:
                dispatch_actor_control(
                    runtime, actor_tree, turn_id, snapshot.actor_id, action, text)
        except Exception as exc:
            error = exc
        try:
            self.call_from_thread(self._finish_actor_control, action, error)
        except RuntimeError:
            pass

    def _finish_actor_control(self, action: str,
                              error: Optional[Exception]) -> None:
        self._actor_control_pending = False
        if error is None:
            self._last_status = f"Actor {action} accepted"
            self.notify(f"Actor {action} request accepted by the runtime.")
        else:
            self._last_status = f"Actor {action} failed"
            self.notify(f"Actor {action} failed: {error}", severity="error")
        self._refresh_status()


def _bounded_text(value: object, maximum: int = _ACTOR_TEXT_BYTES) -> str:
    text = " ".join(clean_text(value).split())
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text
    return encoded[:maximum - 3].decode("utf-8", "ignore") + "..."


__all__ = ["TUIActorControlMixin"]
