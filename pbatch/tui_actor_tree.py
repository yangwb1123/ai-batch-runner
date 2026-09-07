"""Bounded typed actor-tree projection for the Agents inspector.

Actor nodes are facts from RuntimeEvent envelopes.  This module only normalizes
already accepted projection state for display; it never infers actors from
assistant text, stderr, or the shared run ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .tui_view_support import clean_text, one_line


ACTOR_ID_BYTES = 256
ACTOR_LABEL_BYTES = 512
ACTOR_PROVIDER_BYTES = 256
ACTOR_TREE_LIMIT = 256
ACTOR_HISTORY_LIMIT = 32
ACTOR_HISTORY_NODE_LIMIT = 64
ACTOR_HISTORY_SCAN_LIMIT = 8192
AUTHORITY_STATE_BYTES = 64
AUTHORITY_SCOPE_BYTES = 512
AUTHORITY_CAPABILITY_BYTES = 128
AUTHORITY_CAPABILITY_LIMIT = 16
_AUTHORITY_STATES = frozenset({"DECLARED", "INHERITED"})


@dataclass(frozen=True)
class ActorAuthority:
    """Explicit provider authority metadata; never an execution grant."""

    state: str = "UNKNOWN"
    source_actor_id: str = ""
    capabilities: tuple[str, ...] = ()
    scope: str = ""


@dataclass
class ActorState:
    """Mutable projection record owned by :class:`tui_events.Projection`."""

    actor_id: str
    parent_actor_id: str
    workspace_id: str
    session_id: str
    run_id: str
    turn_id: str
    name: str = ""
    role: str = ""
    provider: str = ""
    status: str = "UNKNOWN"
    terminal: bool = False
    success: bool | None = None
    started_at: str = ""
    last_event_at: str = ""
    authority: ActorAuthority = field(default_factory=ActorAuthority)


@dataclass(frozen=True)
class ActorNode:
    """Immutable, display-safe actor node."""

    actor_id: str
    parent_actor_id: str
    name: str = ""
    role: str = ""
    provider: str = ""
    status: str = "UNKNOWN"
    terminal: bool = False
    success: bool | None = None
    current: bool = False
    depth: int = 0
    started_at: str = ""
    last_event_at: str = ""
    authority: ActorAuthority = field(default_factory=ActorAuthority)


@dataclass(frozen=True)
class ActorTreeSnapshot:
    """A bounded actor tree and honest truncation diagnostics."""

    nodes: tuple[ActorNode, ...] = ()
    truncated: bool = False
    skipped: int = 0
    turn_id: str = ""


@dataclass(frozen=True)
class ActorHistoryGroup:
    """One trusted, historical turn with its provider/meta actor tree."""

    turn_id: str
    run_id: str
    session_id: str
    workspace_id: str
    state: str
    last_event_at: str
    tree: ActorTreeSnapshot


@dataclass(frozen=True)
class ActorHistorySnapshot:
    """Bounded historical actor groups and explicit truncation diagnostics."""

    groups: tuple[ActorHistoryGroup, ...] = ()
    truncated: bool = False
    skipped: int = 0


def build_actor_tree(states: Iterable[ActorState], *, turn_id: str = "",
                     limit: int = ACTOR_TREE_LIMIT,
                     current_turn_id: str | None = None) -> ActorTreeSnapshot:
    """Copy bounded actor state into an immutable, deterministic snapshot."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("actor tree limit must be a positive integer")
    selected_turn = turn_id if current_turn_id is None else current_turn_id
    normalized: list[ActorNode] = []
    seen: set[tuple[str, str]] = set()
    skipped = 0
    for state in states:
        if not isinstance(state, ActorState):
            skipped += 1
            continue
        if turn_id and state.turn_id != turn_id:
            continue
        key = (state.turn_id, state.actor_id)
        if not state.actor_id or key in seen:
            skipped += 1
            continue
        if len(normalized) >= limit:
            skipped += 1
            continue
        seen.add(key)
        normalized.append(ActorNode(
            actor_id=_text(state.actor_id, ACTOR_ID_BYTES),
            parent_actor_id=_text(state.parent_actor_id, ACTOR_ID_BYTES),
            name=_text(state.name, ACTOR_LABEL_BYTES),
            role=_text(state.role, ACTOR_LABEL_BYTES),
            provider=_text(state.provider, ACTOR_PROVIDER_BYTES),
            status=_state(state.status), terminal=bool(state.terminal),
            success=state.success if isinstance(state.success, bool) else None,
            current=bool(selected_turn and state.turn_id == selected_turn
                         and not state.terminal),
            depth=0,
            started_at=_text(state.started_at, ACTOR_LABEL_BYTES),
            last_event_at=_text(state.last_event_at, ACTOR_LABEL_BYTES),
            authority=normalize_actor_authority(
                state.authority, state.parent_actor_id),
        ))
    depths = _depths(normalized)
    normalized = [node.__class__(
        node.actor_id, node.parent_actor_id, node.name, node.role,
        node.provider, node.status, node.terminal, node.success, node.current,
        depths.get(node.actor_id, 0), node.started_at, node.last_event_at,
        node.authority)
        for node in normalized]
    normalized.sort(key=lambda node: node.depth)
    return ActorTreeSnapshot(tuple(normalized), bool(skipped), skipped, turn_id)


def build_actor_history(
        states: Iterable[ActorState], *, current_turn_id: str = "",
        limit: int = ACTOR_HISTORY_LIMIT,
        node_limit: int = ACTOR_HISTORY_NODE_LIMIT,
        scan_limit: int = ACTOR_HISTORY_SCAN_LIMIT) -> ActorHistorySnapshot:
    """Aggregate retained trusted actors by turn without inferring history."""
    _positive_limit(limit, "actor history limit")
    _positive_limit(node_limit, "actor history node limit")
    _positive_limit(scan_limit, "actor history scan limit")
    buckets, skipped, truncated = _history_buckets(
        states, current_turn_id, limit, node_limit, scan_limit)
    groups, extra_skipped, extra_truncated = _history_groups(buckets, node_limit)
    return ActorHistorySnapshot(
        tuple(groups), truncated or extra_truncated,
        skipped + extra_skipped)


def _history_buckets(
        states: Iterable[ActorState], current_turn_id: str, limit: int,
        node_limit: int, scan_limit: int
        ) -> tuple[dict[str, list[ActorState]], int, bool]:
    """Collect a finite, per-turn subset from a possibly untrusted iterable."""
    buckets: dict[str, list[ActorState]] = {}
    skipped = 0
    truncated = False
    for scanned, state in enumerate(states, 1):
        if scanned > scan_limit:
            return buckets, skipped + 1, True
        if not isinstance(state, ActorState):
            skipped += 1
            continue
        if not state.turn_id or state.turn_id == current_turn_id:
            continue
        if not state.actor_id:
            skipped += 1
            continue
        bucket = buckets.get(state.turn_id)
        if bucket is None:
            if len(buckets) >= limit:
                skipped += 1
                truncated = True
                continue
            bucket = []
            buckets[state.turn_id] = bucket
        if len(bucket) >= node_limit:
            skipped += 1
            truncated = True
            continue
        bucket.append(state)
    return buckets, skipped, truncated


def _history_groups(
        buckets: dict[str, list[ActorState]], node_limit: int
        ) -> tuple[list[ActorHistoryGroup], int, bool]:
    groups: list[ActorHistoryGroup] = []
    skipped = 0
    truncated = False
    for turn_id, bucket in buckets.items():
        tree = build_actor_tree(
            bucket, turn_id=turn_id, limit=node_limit, current_turn_id="")
        root = next((item for item in bucket if not item.parent_actor_id), bucket[0])
        groups.append(ActorHistoryGroup(
            _text(turn_id, ACTOR_ID_BYTES), _text(root.run_id, ACTOR_ID_BYTES),
            _text(root.session_id, ACTOR_ID_BYTES),
            _text(root.workspace_id, ACTOR_ID_BYTES), _state(root.status),
            _text(max((item.last_event_at for item in bucket), default=""),
                  ACTOR_LABEL_BYTES), tree))
        skipped += tree.skipped
        truncated = truncated or tree.truncated
    groups.sort(key=lambda item: (item.last_event_at, item.turn_id), reverse=True)
    return groups, skipped, truncated


def render_actor_history(snapshot: ActorHistorySnapshot,
                         width: int = 120) -> list[str]:
    """Render compact historical groups; details remain available by cycling."""
    if not isinstance(snapshot, ActorHistorySnapshot) or not snapshot.groups:
        return ["ACTOR HISTORY", "No trusted historical actors."]
    width = max(1, int(width)) if not isinstance(width, bool) else 1
    lines = ["ACTOR HISTORY"]
    for group in snapshot.groups:
        lines.append(_truncate(
            f"○ {group.state} · turn {group.turn_id} · run {group.run_id} · "
            f"{len(group.tree.nodes)} actor(s)", width))
    if snapshot.truncated or snapshot.skipped:
        lines.append(_truncate(
            f"… {snapshot.skipped} historical actor record(s) omitted", width))
    return lines


def _positive_limit(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def authorize_actor_control(capabilities: object, snapshot: ActorTreeSnapshot,
                            turn_id: str, actor_id: str,
                            action: str) -> ActorNode:
    """Return an exact active target or fail closed before any provider call."""
    from .tui_runtime import UnsupportedOperation

    if action not in {"steer", "stop"}:
        raise ValueError("unknown actor control action")
    supports = getattr(capabilities, "supports", None)
    if not callable(supports) or not supports("actor_control"):
        raise UnsupportedOperation(f"actor {action}", "actor_control")
    if (not isinstance(snapshot, ActorTreeSnapshot)
            or not turn_id or snapshot.turn_id != turn_id):
        raise ValueError("actor control turn identity does not match")
    matches = tuple(node for node in snapshot.nodes
                    if node.actor_id == actor_id)
    if len(matches) != 1 or not matches[0].current or matches[0].terminal:
        raise ValueError("actor control identity is not an active tree node")
    return matches[0]


def dispatch_actor_control(runtime: object, snapshot: ActorTreeSnapshot,
                           turn_id: str, actor_id: str, action: str,
                           text: str = "") -> None:
    """Execute only a capability- and identity-bound actor control request."""
    from .tui_runtime import (
        ActorInterruptRequest, ActorSteerRequest, UnsupportedOperation,
    )

    info = runtime.runtime_info() if callable(
        getattr(runtime, "runtime_info", None)) else None
    capabilities = getattr(info, "capabilities", None)
    authorize_actor_control(capabilities, snapshot, turn_id, actor_id, action)
    if action == "steer":
        if not isinstance(text, str) or not text.strip():
            raise ValueError("actor steer text must not be empty")
        method = getattr(runtime, "steer_actor", None)
        if not callable(method):
            raise UnsupportedOperation("actor steer", "actor_control")
        method(ActorSteerRequest(turn_id, actor_id, text))
        return
    method = getattr(runtime, "interrupt_actor", None)
    if not callable(method):
        raise UnsupportedOperation("actor stop", "actor_control")
    method(ActorInterruptRequest(turn_id, actor_id))


def render_actor_tree(snapshot: ActorTreeSnapshot, width: int = 120) -> list[str]:
    """Render a cycle-safe tree without reading any untyped provider text."""
    if not isinstance(snapshot, ActorTreeSnapshot):
        return ["ACTOR TREE", "No trusted actor tree."]
    width = max(1, int(width)) if not isinstance(width, bool) else 1
    if not snapshot.nodes:
        return ["ACTOR TREE", "No trusted active actors."]
    by_id = {node.actor_id: node for node in snapshot.nodes}
    children: dict[str, list[ActorNode]] = {}
    roots: list[ActorNode] = []
    for node in snapshot.nodes:
        if node.parent_actor_id and node.parent_actor_id in by_id:
            children.setdefault(node.parent_actor_id, []).append(node)
        else:
            roots.append(node)
    lines = ["ACTOR TREE"]
    visited: set[str] = set()
    for root in roots:
        _append_node(lines, root, children, visited, width, 0)
    for node in snapshot.nodes:
        if node.actor_id not in visited:
            _append_node(lines, node, children, visited, width, 0)
    if snapshot.truncated:
        lines.append(_truncate(f"… {snapshot.skipped} actor node(s) omitted", width))
    return lines


def _append_node(lines: list[str], node: ActorNode,
                 children: dict[str, list[ActorNode]], visited: set[str],
                 width: int, indent: int) -> None:
    if node.actor_id in visited:
        return
    visited.add(node.actor_id)
    cancelled = node.status in {"CANCELLED", "INTERRUPTED"}
    marker = ("↯" if cancelled else "●" if node.current else
              "✓" if node.success is True else
              "✗" if node.success is False else "○")
    label = node.name or node.role or node.actor_id
    suffix = f" · {node.status}"
    if node.provider:
        suffix += f" · {node.provider}"
    if node.authority.state != "UNKNOWN":
        suffix += f" · authority={node.authority.state.lower()}"
        if node.authority.capabilities:
            suffix += "(" + ",".join(node.authority.capabilities[:4]) + ")"
    lines.append(_truncate("  " * indent + f"{marker} {label} [{node.actor_id}]" + suffix,
                           width))
    for child in children.get(node.actor_id, ()):
        _append_node(lines, child, children, visited, width, indent + 1)


def _depths(nodes: list[ActorNode]) -> dict[str, int]:
    parents = {node.actor_id: node.parent_actor_id for node in nodes}
    depths: dict[str, int] = {}
    for node in nodes:
        current, path = node.actor_id, set()
        depth = 0
        while parents.get(current) in parents and current not in path:
            path.add(current)
            current = parents[current]
            depth += 1
        depths[node.actor_id] = min(depth, len(nodes))
    return depths


def normalize_actor_authority(value: object,
                              parent_actor_id: str = "",
                              parent_authority: object = None) -> ActorAuthority:
    """Accept an explicit authority claim without granting execution rights.

    An ``INHERITED`` claim must name the exact typed parent. When the parent's
    authority is supplied, the child is also restricted to the parent's
    capabilities and scope; an unknown parent never becomes an authority
    source. Omitting ``parent_authority`` preserves the standalone structural
    normalization used by older callers.
    """
    if isinstance(value, ActorAuthority):
        authority = value
    elif isinstance(value, dict):
        state = _state(value.get("state", value.get("mode", "")))
        source = _text(value.get("source_actor_id",
                                  value.get("parent_actor_id", "")),
                       ACTOR_ID_BYTES)
        capabilities = _capability_values(value.get("capabilities"))
        scope = _text(value.get("scope", ""), AUTHORITY_SCOPE_BYTES)
        authority = ActorAuthority(state, source, capabilities, scope)
    else:
        return ActorAuthority()
    state = _text(authority.state, AUTHORITY_STATE_BYTES).upper()
    if state not in _AUTHORITY_STATES:
        return ActorAuthority()
    source = _text(authority.source_actor_id, ACTOR_ID_BYTES)
    parent = _text(parent_actor_id, ACTOR_ID_BYTES)
    capabilities = _capability_values(authority.capabilities)
    scope = _text(authority.scope, AUTHORITY_SCOPE_BYTES)
    if state == "DECLARED" and source:
        return ActorAuthority()
    if state == "INHERITED":
        if not parent or source != parent:
            return ActorAuthority()
        if parent_authority is not None:
            parent_value = (parent_authority if isinstance(
                parent_authority, ActorAuthority) else
                normalize_actor_authority(parent_authority))
            if (parent_value.state not in _AUTHORITY_STATES
                    or not set(capabilities).issubset(parent_value.capabilities)
                    or scope != parent_value.scope):
                return ActorAuthority()
    return ActorAuthority(state, source, capabilities, scope)


def root_actor_authority_payload(value: object,
                                  provider_actors_enabled: bool = False) -> dict:
    """Extract one explicit provider root claim without inferring authority."""
    if not provider_actors_enabled or not isinstance(value, dict):
        return {}
    sources = [value]
    for name in ("turn", "actor", "rootActor", "root_actor"):
        nested = value.get(name)
        if isinstance(nested, dict):
            sources.append(nested)
    for source in sources[:5]:
        for name in ("actorAuthority", "actor_authority", "authority"):
            claim = source.get(name)
            payload = actor_authority_payload(claim)
            if payload:
                return payload
    return {}


def actor_authority_payload(value: object,
                            parent_actor_id: str = "") -> dict:
    """Serialize only a valid authority claim for a RuntimeEvent payload."""
    authority = normalize_actor_authority(value, parent_actor_id)
    if authority.state == "UNKNOWN":
        return {}
    return {
        "state": authority.state,
        "source_actor_id": authority.source_actor_id,
        "capabilities": list(authority.capabilities),
        "scope": authority.scope,
    }


def _capability_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value[:AUTHORITY_CAPABILITY_LIMIT]:
        if not isinstance(item, str):
            continue
        text = _text(item, AUTHORITY_CAPABILITY_BYTES)
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _state(value: object) -> str:
    text = one_line(clean_text(value)).strip().upper().replace("-", "_")
    return text[:ACTOR_LABEL_BYTES] or "UNKNOWN"


def _text(value: object, maximum: int) -> str:
    text = one_line(clean_text(value))
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return text
    return encoded[:maximum - 3].decode("utf-8", errors="ignore") + "..."


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    return "…" if width == 1 else value[:width - 1] + "…"


__all__ = [
    "ACTOR_HISTORY_LIMIT", "ACTOR_HISTORY_NODE_LIMIT", "ACTOR_TREE_LIMIT",
    "ActorAuthority", "ActorHistoryGroup", "ActorHistorySnapshot", "ActorNode",
    "ActorState", "ActorTreeSnapshot", "actor_authority_payload",
    "root_actor_authority_payload",
    "build_actor_history", "build_actor_tree", "normalize_actor_authority",
    "render_actor_history", "render_actor_tree",
]
