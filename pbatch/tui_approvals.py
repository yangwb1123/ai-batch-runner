"""Typed, bounded approval projection and TUI controls.

Only trusted ``approval.*`` runtime events enter this module.  Assistant text,
stderr and restored non-terminal journals never become executable decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Static

from .tui_protocol import RuntimeEvent


APPROVAL_MAX_PENDING = 32
APPROVAL_FIELD_BYTES = 16 * 1024
_APPROVAL_KINDS = frozenset({
    "approval.requested", "approval.resolved", "approval.expired",
})
_DECISIONS = ("allow", "allow_session", "deny", "cancel")
_DECISION_ALIASES = {
    "accept": "allow",
    "approved": "allow",
    "acceptforsession": "allow_session",
    "approved_for_session": "allow_session",
    "decline": "deny",
    "denied": "deny",
    "abort": "cancel",
    "interrupt": "cancel",
}


@dataclass(frozen=True)
class ApprovalRequest:
    """One immutable approval request bound to an exact runtime turn."""

    request_id: str | int
    approval_type: str
    method: str
    target: str
    received_at: str
    expires_at: str
    available_decisions: tuple[str, ...]
    workspace_id: str
    session_id: str
    run_id: str
    turn_id: str
    actor_id: str
    runtime: str = ""

    @property
    def key(self) -> tuple[type, Any]:
        return type(self.request_id), self.request_id

    @property
    def scope_key(self) -> tuple[Any, ...]:
        return (
            self.workspace_id, self.session_id, self.run_id, self.turn_id,
            self.actor_id, type(self.request_id), self.request_id,
        )


@dataclass(frozen=True)
class ApprovalSnapshot:
    """Defensive current-turn approval view."""

    pending: tuple[ApprovalRequest, ...] = ()

    @property
    def waiting(self) -> bool:
        return bool(self.pending)

    @property
    def active(self) -> Optional[ApprovalRequest]:
        return self.pending[0] if self.pending else None


class ApprovalProjection:
    """Rebuild pending approvals from the append-only runtime event stream."""

    def __init__(self, max_pending: int = APPROVAL_MAX_PENDING) -> None:
        if (isinstance(max_pending, bool) or not isinstance(max_pending, int)
                or max_pending <= 0):
            raise ValueError("max_pending must be a positive integer")
        self.max_pending = max_pending
        self._pending: dict[tuple[type, Any], ApprovalRequest] = {}

    def reset(self) -> None:
        self._pending.clear()

    def validate(self, event: RuntimeEvent) -> None:
        if event.kind not in _APPROVAL_KINDS:
            return
        request_id = event.payload.get("request_id")
        if not _valid_request_id(request_id):
            raise ValueError("approval event requires a scalar request_id")
        key = type(request_id), request_id
        if event.kind == "approval.requested":
            _request_from_event(event)
            if key in self._pending:
                raise ValueError("duplicate pending approval request_id")
            if len(self._pending) >= self.max_pending:
                raise OverflowError("approval projection pending limit reached")
            return
        entry = self._pending.get(key)
        claimed = _field(event.payload.get("approval_type", ""), 256)
        if entry is not None and claimed not in {"", "unknown", entry.approval_type}:
            raise ValueError("approval resolution type mismatch")

    def consume(self, event: RuntimeEvent) -> None:
        if event.kind == "approval.requested":
            entry = _request_from_event(event)
            self._pending[entry.key] = entry
        elif event.kind in {"approval.resolved", "approval.expired"}:
            request_id = event.payload.get("request_id")
            if _valid_request_id(request_id):
                self._pending.pop((type(request_id), request_id), None)
        elif event.kind in {"turn.completed", "turn.failed", "turn.interrupted"}:
            self._drop_turn(event)

    def snapshot(self) -> ApprovalSnapshot:
        return ApprovalSnapshot(tuple(self._pending.values()))

    def _drop_turn(self, event: RuntimeEvent) -> None:
        stale = [key for key, item in self._pending.items()
                 if item.session_id == event.session_id
                 and item.run_id == event.run_id and item.turn_id == event.turn_id]
        for key in stale:
            self._pending.pop(key, None)


@dataclass(frozen=True)
class ApprovalScreenAction:
    """A user decision returned by the modal without runtime authority."""

    request_id: str | int
    approval_type: str
    decision: str


class TUIApprovalScreen(ModalScreen[Optional[ApprovalScreenAction]]):
    """Fail-closed typed approval card; Enter defaults to deny."""

    BINDINGS = [
        Binding("escape", "close", "Keep pending", priority=True),
        Binding("q", "close", "Keep pending", show=False),
        Binding("a", "allow", "Allow once"),
        Binding("s", "allow_session", "Allow session"),
        Binding("d", "deny", "Deny"),
        Binding("enter", "deny", "Deny", show=False),
        Binding("i", "cancel", "Interrupt"),
    ]
    CSS = """
    TUIApprovalScreen { align: center middle; background: $background 75%; }
    #approval-dialog {
        width: 86%; height: 82%; min-width: 42;
        background: $surface; border: round $warning;
    }
    #approval-heading {
        height: 3; padding: 1 2; text-style: bold; background: $boost;
    }
    #approval-meta { height: auto; padding: 1 2; color: $text-muted; }
    #approval-target-scroll {
        height: 1fr; margin: 0 2; border: solid $panel-lighten-1;
    }
    #approval-target { width: 100%; padding: 1 2; }
    #approval-expiry { height: 2; padding: 0 2; color: $warning; }
    #approval-hint { height: 3; padding: 0 2 1 2; color: $text-muted; }
    """

    def __init__(self, request: ApprovalRequest, *, read_only: bool = False) -> None:
        super().__init__(id="typed-approval")
        self.request = request
        self.read_only = bool(read_only)
        self._expiry_timer = None

    def compose(self) -> ComposeResult:
        request = self.request
        with Vertical(id="approval-dialog"):
            yield Static(
                f"Approval required · {request.approval_type}",
                id="approval-heading", markup=False)
            yield Static(_approval_meta(request), id="approval-meta", markup=False)
            with VerticalScroll(id="approval-target-scroll"):
                yield Static(request.target, id="approval-target", markup=False)
            yield Static(self._expiry_text(), id="approval-expiry", markup=False)
            yield Static(self._hint(), id="approval-hint", markup=False)

    def on_mount(self) -> None:
        self._expiry_timer = self.set_interval(1.0, self._refresh_expiry)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_allow(self) -> None:
        self._choose("allow")

    def action_allow_session(self) -> None:
        self._choose("allow_session")

    def action_deny(self) -> None:
        self._choose("deny")

    def action_cancel(self) -> None:
        self._choose("cancel")

    def _choose(self, decision: str) -> None:
        if self.read_only:
            self.app.notify("Recovered approval history is read-only.", severity="warning")
            return
        if decision not in self.request.available_decisions:
            self.app.notify("This runtime did not offer that approval decision.",
                            severity="warning")
            return
        self.dismiss(ApprovalScreenAction(
            self.request.request_id, self.request.approval_type, decision))

    def _hint(self) -> str:
        if self.read_only:
            return "Read-only history · Esc close"
        labels = []
        offered = set(self.request.available_decisions)
        if "allow" in offered:
            labels.append("a allow once")
        if "allow_session" in offered:
            labels.append("s allow session")
        if "deny" in offered:
            labels.append("d/Enter deny")
        if "cancel" in offered:
            labels.append("i interrupt")
        labels.append("Esc keep pending")
        return " · ".join(labels)

    def _refresh_expiry(self) -> None:
        try:
            self.query_one("#approval-expiry", Static).update(self._expiry_text())
        except Exception:
            return

    def _expiry_text(self) -> str:
        remaining = _seconds_until(self.request.expires_at)
        if remaining is None:
            return "Expiry is controlled by the runtime."
        if remaining <= 0:
            return "Expired locally; any late decision will be rejected."
        return f"Expires in {remaining}s; expiry defaults to decline."


class TUIApprovalMixin:
    """Wire immutable approval views to one live App Server decision path."""

    def _init_approval_controls(self) -> None:
        self._approval_auto_seen: set[tuple[Any, ...]] = set()
        self._approval_decisions_pending: set[tuple[Any, ...]] = set()

    def _slash_approvals(self, args: tuple[str, ...] = ()) -> None:
        if not self._require_no_args("approvals", args):
            return
        self._open_approval_screen()

    def _palette_approvals(self) -> None:
        self._slash_approvals(())

    def _open_approval_screen(self, *, automatic: bool = False) -> None:
        request = self._approval_snapshot().active
        if request is None:
            if not automatic:
                self.notify("There are no pending typed approvals.")
            return
        can_resolve = self._can_resolve_approval(request)
        if automatic and not can_resolve:
            return
        self._approval_auto_seen.add(request.scope_key)
        self.push_screen(
            TUIApprovalScreen(request, read_only=not can_resolve),
            self._handle_approval_screen_action)

    def _handle_approval_screen_action(self, action: Any) -> None:
        if action is None:
            return
        request = self._find_pending_approval(action)
        if request is None or not self._can_resolve_approval(request):
            self.notify("Approval is stale or no longer belongs to the active turn.",
                        severity="error")
            return
        if request.scope_key in self._approval_decisions_pending:
            self.notify("An approval decision is already being sent.", severity="warning")
            return
        self._approval_decisions_pending.add(request.scope_key)
        self._last_status = "Sending approval decision"
        self._refresh_status()
        self.run_worker(
            lambda: self._approval_worker(request, action.decision),
            name="approval-decision", group="approval-control",
            exit_on_error=False, exclusive=True, thread=True)

    def _approval_worker(self, request: ApprovalRequest, decision: str) -> None:
        accepted, error = False, None
        try:
            payload = {
                "request_id": request.request_id,
                "approval_type": request.approval_type,
                "decision": decision,
                "workspace_id": request.workspace_id,
                "session_id": request.session_id,
                "run_id": request.run_id,
                "turn_id": request.turn_id,
                "actor_id": request.actor_id,
            }
            if request.runtime == "pbatch-native":
                channel = getattr(self, "_native_pipeline_approval", None)
                accepted = bool(channel is not None and channel.resolve(payload))
            else:
                accepted = self._adapter.resolve_approval(payload) is True
        except Exception as exc:
            error = exc
        try:
            self.call_from_thread(
                self._finish_approval_decision, request, decision, accepted, error)
        except RuntimeError:
            pass

    def _finish_approval_decision(
            self, request: ApprovalRequest, decision: str,
            accepted: bool, error: Optional[Exception]) -> None:
        self._approval_decisions_pending.discard(request.scope_key)
        if error is not None:
            self.notify(f"Approval decision failed: {error}", severity="error")
            self._last_status = "Approval decision failed"
        elif not accepted:
            self.notify("Approval expired or was already resolved.", severity="error")
            self._last_status = "Approval decision rejected"
        else:
            self.notify(f"Approval decision sent: {_decision_label(decision)}.")
            self._last_status = "Approval decision sent"
        self._refresh_status()

    def _refresh_approval_ui(self, view: Any) -> None:
        pending = getattr(getattr(view, "approvals", None), "pending", ())
        screen = self._current_approval_screen()
        if isinstance(screen, TUIApprovalScreen):
            keys = {item.scope_key for item in pending}
            if screen.request.scope_key not in keys:
                screen.dismiss(None)
            return
        if not pending or len(getattr(self, "screen_stack", ())) > 1:
            return
        request = pending[0]
        if request.scope_key in self._approval_auto_seen:
            return
        self._open_approval_screen(automatic=True)

    def _reset_approval_controls(self) -> None:
        self._approval_auto_seen.clear()
        self._approval_decisions_pending.clear()
        screen = self._current_approval_screen()
        if screen is not None:
            screen.dismiss(None)

    def _current_approval_screen(self) -> Optional[TUIApprovalScreen]:
        """Return the active approval modal without assuming a mounted screen stack."""
        stack = getattr(self, "screen_stack", ())
        if not stack:
            return None
        screen = stack[-1]
        return screen if isinstance(screen, TUIApprovalScreen) else None

    def _approval_status_lines(self) -> list[str]:
        snapshot = self._approval_snapshot()
        if not snapshot.pending:
            return []
        active = snapshot.pending[0]
        return [f"approvals {len(snapshot.pending)} pending",
                f"approval  {active.approval_type[:20]}"]

    def _approval_snapshot(self) -> ApprovalSnapshot:
        view = getattr(self, "_runtime_view", None)
        value = getattr(view, "approvals", None)
        return value if isinstance(value, ApprovalSnapshot) else ApprovalSnapshot()

    def _find_pending_approval(self, action: Any) -> Optional[ApprovalRequest]:
        request_id = getattr(action, "request_id", None)
        approval_type = str(getattr(action, "approval_type", "") or "")
        for request in self._approval_snapshot().pending:
            if (type(request.request_id) is type(request_id)
                    and request.request_id == request_id
                    and request.approval_type == approval_type):
                return request
        return None

    def _can_resolve_approval(self, request: ApprovalRequest) -> bool:
        if getattr(self._event_controller, "read_only_reason", ""):
            return False
        if request.runtime == "pbatch-native":
            channel = getattr(self, "_native_pipeline_approval", None)
            return bool(channel is not None and channel.matches(request))
        try:
            supported = self._active_runtime_supports("typed_approvals")
        except Exception:
            supported = False
        if not supported:
            return False
        adapter = getattr(self, "_adapter", None)
        handle = getattr(self, "_turn_handle", None)
        if adapter is None or handle is None or getattr(handle, "done", False):
            return False
        expected = (request.session_id, request.run_id, request.turn_id, request.actor_id)
        actual = tuple(str(getattr(handle, name, "") or "") for name in (
            "session_id", "run_id", "turn_id", "actor_id"))
        return expected == actual


def _request_from_event(event: RuntimeEvent) -> ApprovalRequest:
    payload = event.payload
    request_id = payload.get("request_id")
    if not _valid_request_id(request_id):
        raise ValueError("approval event requires a scalar request_id")
    return ApprovalRequest(
        request_id=request_id,
        approval_type=_required_field(payload.get("approval_type"), "approval_type", 256),
        method=_required_field(payload.get("method"), "approval method", 512),
        target=_required_field(payload.get("target"), "approval target"),
        received_at=_field(payload.get("received_at", ""), 256),
        expires_at=_field(payload.get("expires_at", ""), 256),
        available_decisions=_available_decisions(payload.get("available_decisions")),
        workspace_id=event.workspace_id, session_id=event.session_id,
        run_id=event.run_id, turn_id=event.turn_id, actor_id=event.actor_id,
        runtime=str(event.extensions.get("runtime", "") or ""))


def _available_decisions(value: Any) -> tuple[str, ...]:
    if value is None:
        return _DECISIONS
    if not isinstance(value, (list, tuple)):
        raise ValueError("available_decisions must be a list")
    decisions = []
    for raw in value:
        name = str(raw or "").strip()
        normalized = _DECISION_ALIASES.get(name.lower(), name)
        if normalized not in _DECISIONS:
            continue
        if normalized not in decisions:
            decisions.append(normalized)
    if "deny" not in decisions:
        decisions.append("deny")
    return tuple(decisions)


def _valid_request_id(value: Any) -> bool:
    return isinstance(value, str) or (
        isinstance(value, int) and not isinstance(value, bool))


def _required_field(value: Any, name: str, maximum: int = APPROVAL_FIELD_BYTES) -> str:
    text = _field(value, maximum)
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text


def _field(value: Any, maximum: int) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text
    marker = "…[truncated]"
    prefix = encoded[:maximum - len(marker.encode())].decode("utf-8", "ignore")
    return prefix + marker


def _approval_meta(request: ApprovalRequest) -> str:
    request_kind = "string" if isinstance(request.request_id, str) else "integer"
    request_label = repr(request.request_id)
    source = f"{request.session_id} / {request.turn_id} / {request.actor_id}"
    return (f"Source: {source}\nMethod: {request.method}\n"
            f"Request: {request_kind} {request_label}\n"
            f"Received: {request.received_at or 'unknown'}")


def _seconds_until(value: str) -> Optional[int]:
    if not value:
        return None
    try:
        expires = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        seconds = int((expires - datetime.now(timezone.utc)).total_seconds())
    except (OverflowError, TypeError, ValueError):
        return None
    return max(0, seconds)


def _decision_label(value: str) -> str:
    return {
        "allow": "allow once", "allow_session": "allow for session",
        "deny": "deny", "cancel": "interrupt",
    }.get(value, value)
