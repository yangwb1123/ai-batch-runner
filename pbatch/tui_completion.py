"""Bounded, evidence-only completion projection for one TUI turn."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import math
from typing import Any, Iterable, Mapping, Optional

from .tui_protocol import TERMINAL_TURN_STATES, TurnState
from .tui_view_support import clean_text, one_line


FACT_LIMIT = 32
PATH_LIMIT = 64
USAGE_LIMIT = 32
DIAGNOSTIC_LIMIT = 16
_LABEL_BYTES = 256
_DETAIL_BYTES = 1024


@dataclass(frozen=True)
class CompletionFact:
    """One explicit runtime fact; status never comes from message text."""

    label: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class CompletionStep:
    """One bounded step from the latest structured plan."""

    text: str
    state: str


@dataclass(frozen=True)
class CompletionSnapshot:
    """Immutable current-turn completion evidence for an inspector."""

    source: str = ""
    state: str = ""
    run_id: str = ""
    turn_id: str = ""
    actor_id: str = ""
    elapsed_ms: Optional[int] = None
    returncode: Optional[int] = None
    reason: str = ""
    commands: tuple[CompletionFact, ...] = ()
    tools: tuple[CompletionFact, ...] = ()
    validations: tuple[CompletionFact, ...] = ()
    gates: tuple[CompletionFact, ...] = ()
    artifacts: tuple[CompletionFact, ...] = ()
    diagnostics: tuple[CompletionFact, ...] = ()
    plan_available: bool = False
    plan_status: str = ""
    plan_steps: tuple[CompletionStep, ...] = ()
    workspace_phase: str = ""
    workspace_changed: bool = False
    dirty_files: int = 0
    changed_paths: tuple[str, ...] = ()
    head_changed: bool = False
    usage: tuple[tuple[str, float], ...] = ()
    truncated_sections: tuple[str, ...] = ()

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_TURN_STATES


def build_completion_snapshot(
        *, source: str, state: str = "", run_id: str = "",
        turn_id: str = "", actor_id: str = "", timeline: Iterable[Any] = (),
        workspace: Any = None, events: Iterable[Any] = (),
        usage: Optional[Mapping[str, Any]] = None,
        result: Any = None) -> CompletionSnapshot:
    """Build a bounded snapshot from typed events and local authority facts."""
    normalized_state = _normalize_state(state, result)
    if not turn_id and not normalized_state and result is None:
        return CompletionSnapshot(source=_text(source, _LABEL_BYTES))
    timeline_rows = tuple(timeline)
    event_rows = _relevant_events(tuple(events), run_id, turn_id)
    terminal = _terminal_event(event_rows)
    elapsed_ms, returncode, reason = _terminal_values(terminal, result)
    commands, command_cut = _command_facts(timeline_rows, normalized_state)
    tools, tool_cut = _operation_facts(
        event_rows, "tool", normalized_state, ("tool", "name", "title"))
    validations, validation_cut = _operation_facts(
        event_rows, "validation", normalized_state,
        ("validator", "name", "title", "command"))
    gates, gate_cut = _gate_facts(event_rows)
    artifacts, artifact_cut = _artifact_facts(event_rows)
    diagnostics, diagnostic_cut = _diagnostic_facts(event_rows)
    plan_available, plan_status, plan_steps, plan_cut = _plan_facts(timeline_rows)
    workspace_values, path_cut = _workspace_values(workspace)
    usage_values, usage_cut = _usage_values(usage, timeline_rows)
    risk_cut = _risk_signal_count(
        commands, tools, validations, gates, diagnostics, reason,
        normalized_state) > FACT_LIMIT
    cuts = _truncation_names(
        event_rows, turn_id, source,
        commands=command_cut, tools=tool_cut, validations=validation_cut,
        gates=gate_cut, artifacts=artifact_cut, diagnostics=diagnostic_cut,
        plan=plan_cut, changes=path_cut, usage=usage_cut, risks=risk_cut)
    return CompletionSnapshot(
        source=_text(source, _LABEL_BYTES), state=normalized_state,
        run_id=_text(run_id, _LABEL_BYTES),
        turn_id=_text(turn_id, _LABEL_BYTES),
        actor_id=_text(actor_id, _LABEL_BYTES), elapsed_ms=elapsed_ms,
        returncode=returncode, reason=reason, commands=commands, tools=tools,
        validations=validations, gates=gates, artifacts=artifacts,
        diagnostics=diagnostics, plan_available=plan_available,
        plan_status=plan_status, plan_steps=plan_steps,
        workspace_phase=workspace_values[0],
        workspace_changed=workspace_values[1], dirty_files=workspace_values[2],
        changed_paths=workspace_values[3], head_changed=workspace_values[4],
        usage=usage_values, truncated_sections=cuts)


def build_backend_completion_snapshot(
        snapshot: Any, timeline: Iterable[Any]) -> CompletionSnapshot:
    """Fallback for callback-only backends that expose an explicit result."""
    values = snapshot if isinstance(snapshot, Mapping) else {}
    status = str(values.get("status", ""))
    active = values.get("running") is True or status.lower() in {
        "running", "queued", "cancelling", "interrupting"}
    result = None if active else values.get("last_result")
    delta = values.get("last_turn_workspace")
    workspace = (dict(delta) if result is not None
                 and isinstance(delta, Mapping) else {})
    workspace["phase"] = "after" if result is not None else ""
    return build_completion_snapshot(
        source="local backend snapshot", state=status,
        timeline=timeline, workspace=workspace, result=result)


def render_completion(snapshot: CompletionSnapshot) -> str:
    """Render a bounded completion report without promoting model prose."""
    if not isinstance(snapshot, CompletionSnapshot):
        raise TypeError("snapshot must be a CompletionSnapshot")
    lines = [
        "# Completion", "",
        "Read-only current-turn facts from typed runtime evidence and local Git authority.",
    ]
    if not snapshot.state and not snapshot.turn_id and snapshot.returncode is None:
        return "\n".join(lines + ["", "No turn completion evidence is available."])
    lines.extend(_outcome_section(snapshot))
    lines.extend(_fact_section("Commands", snapshot.commands,
                               "No command events were emitted; no command pass is claimed."))
    lines.extend(_change_section(snapshot))
    lines.extend(_plan_section(snapshot))
    lines.extend(_verification_section(snapshot))
    lines.extend(_fact_section("Tools", snapshot.tools,
                               "No tool lifecycle events were emitted."))
    lines.extend(_usage_section(snapshot))
    lines.extend(_fact_section("Artifacts", snapshot.artifacts,
                               "No artifact references were emitted."))
    lines.extend(_risk_section(snapshot))
    if snapshot.truncated_sections:
        value = ", ".join(_markdown(item) for item in snapshot.truncated_sections)
        lines.extend(["", "## Evidence limits", "",
                      f"Newest bounded evidence shown; truncated: {value}."])
    return "\n".join(lines)


def _outcome_section(snapshot: CompletionSnapshot) -> list[str]:
    state = snapshot.state or "unavailable"
    current = " (current)" if not snapshot.terminal else ""
    lines = ["", "## Outcome", "", f"- State: `{_code(state)}`{current}",
             f"- Source: {_markdown(snapshot.source or 'unavailable')}"]
    identities = [("Run", snapshot.run_id), ("Turn", snapshot.turn_id),
                  ("Actor", snapshot.actor_id)]
    lines.extend(f"- {name}: `{_code(value)}`" for name, value in identities if value)
    if snapshot.elapsed_ms is not None:
        lines.append(f"- Elapsed: {_duration(snapshot.elapsed_ms)}")
    if snapshot.returncode is not None:
        lines.append(f"- Return code: `{snapshot.returncode}`")
    if snapshot.reason:
        lines.append(f"- Reason: {_markdown(snapshot.reason)}")
    return lines


def _fact_section(title: str, facts: tuple[CompletionFact, ...],
                  empty: str) -> list[str]:
    lines = ["", f"## {title}", ""]
    if not facts:
        return lines + [empty]
    return lines + _fact_lines(facts)


def _fact_lines(facts: tuple[CompletionFact, ...]) -> list[str]:
    lines = []
    for fact in facts:
        detail = f" · {_markdown(fact.detail)}" if fact.detail else ""
        lines.append(
            f"- {_status_marker(fact.status)} `{_code(fact.status)}` · "
            f"{_markdown(fact.label)}{detail}")
    return lines


def _change_section(snapshot: CompletionSnapshot) -> list[str]:
    lines = ["", "## Changes", ""]
    if snapshot.workspace_phase != "after":
        return lines + ["Authoritative after-turn workspace evidence is not available yet."]
    if not snapshot.workspace_changed:
        return lines + ["No authoritative workspace change was observed."]
    facts = f"{snapshot.dirty_files} dirty file(s)"
    if snapshot.head_changed:
        facts += " · HEAD changed"
    lines.append(f"- {facts}")
    lines.extend(f"- `{_code(path)}`" for path in snapshot.changed_paths)
    return lines


def _plan_section(snapshot: CompletionSnapshot) -> list[str]:
    lines = ["", "## Unfinished plan", ""]
    if not snapshot.plan_available:
        return lines + ["No structured plan evidence was emitted."]
    completed = sum(step.state == "completed" for step in snapshot.plan_steps)
    lines.append(f"- Progress: {completed}/{len(snapshot.plan_steps)} completed")
    if snapshot.plan_status:
        lines.append(f"- Runtime status: `{_code(snapshot.plan_status)}`")
    pending = [step for step in snapshot.plan_steps if step.state != "completed"]
    if not pending:
        return lines + ["- No unfinished structured plan steps."]
    lines.extend(
        f"- {_status_marker(step.state)} `{_code(step.state)}` · "
        f"{_markdown(step.text)}" for step in pending)
    return lines


def _verification_section(snapshot: CompletionSnapshot) -> list[str]:
    lines = _fact_section(
        "Validation", snapshot.validations,
        "No validation events were emitted; no validator pass is claimed.")
    lines.extend(_fact_section(
        "Gate verdicts", snapshot.gates,
        "No gate verdict events were emitted; no gate pass is claimed."))
    return lines


def _usage_section(snapshot: CompletionSnapshot) -> list[str]:
    lines = ["", "## Usage", ""]
    if not snapshot.usage:
        return lines + ["No finite usage metrics were emitted."]
    lines.extend(f"- `{_code(name)}`: {_number_text(value)}"
                 for name, value in snapshot.usage)
    return lines


def _risk_section(snapshot: CompletionSnapshot) -> list[str]:
    risky = []
    for group in (snapshot.commands, snapshot.tools, snapshot.validations,
                  snapshot.gates):
        risky.extend(fact for fact in group if fact.status in {
            "failed", "rejected", "denied", "blocked", "not_completed"})
    risky.extend(snapshot.diagnostics)
    if snapshot.reason and snapshot.state != TurnState.SUCCEEDED:
        risky.append(CompletionFact("terminal", "failed", snapshot.reason))
    lines = ["", "## Risks and diagnostics", ""]
    if not risky:
        return lines + [
            "No explicit risk signal appears in bounded runtime evidence; "
            "this is not a safety guarantee."]
    return lines + _fact_lines(tuple(risky[:FACT_LIMIT]))


def _risk_signal_count(
        commands: tuple[CompletionFact, ...], tools: tuple[CompletionFact, ...],
        validations: tuple[CompletionFact, ...], gates: tuple[CompletionFact, ...],
        diagnostics: tuple[CompletionFact, ...], reason: str, state: str) -> int:
    count = sum(
        fact.status in {"failed", "rejected", "denied", "blocked", "not_completed"}
        for group in (commands, tools, validations, gates) for fact in group)
    count += len(diagnostics)
    return count + int(bool(reason and state != TurnState.SUCCEEDED))


def _command_facts(entries: tuple[Any, ...], state: str
                   ) -> tuple[tuple[CompletionFact, ...], bool]:
    commands = [entry for entry in entries
                if getattr(entry, "kind", "") == "command"]
    values = []
    for entry in commands[-FACT_LIMIT:]:
        code = getattr(entry, "exit_code", None)
        if code is None:
            status = "not_completed" if state in TERMINAL_TURN_STATES else "running"
            detail = "exit unavailable"
        else:
            status = "passed" if code == 0 else "failed"
            detail = f"exit {code}"
        values.append(CompletionFact(
            _text(getattr(entry, "summary", "command"), _LABEL_BYTES),
            status, detail))
    return tuple(values), len(commands) > len(values)


def _operation_facts(
        events: tuple[Any, ...], prefix: str, state: str,
        label_fields: tuple[str, ...]) -> tuple[tuple[CompletionFact, ...], bool]:
    rows: OrderedDict[str, CompletionFact] = OrderedDict()
    selected = [event for event in events
                if _event_kind(event).startswith(prefix + ".")]
    for index, event in enumerate(selected):
        payload = _payload(event)
        key = _event_key(event, payload, prefix, index)
        label = _event_label(payload, label_fields, prefix)
        suffix = _event_kind(event).rsplit(".", 1)[-1]
        status = "running" if suffix == "started" else _event_outcome(payload, suffix)
        rows[key] = CompletionFact(label, status, _event_detail(payload))
        rows.move_to_end(key)
    values = list(rows.values())
    if state in TERMINAL_TURN_STATES:
        values = [CompletionFact(item.label, (
            "not_completed" if item.status == "running" else item.status), item.detail)
                  for item in values]
    return tuple(values[-FACT_LIMIT:]), len(values) > FACT_LIMIT


def _gate_facts(events: tuple[Any, ...]
                ) -> tuple[tuple[CompletionFact, ...], bool]:
    selected = [event for event in events if _event_kind(event) == "gate.verdict"]
    values = []
    for event in selected[-FACT_LIMIT:]:
        payload = _payload(event)
        values.append(CompletionFact(
            _event_label(payload, ("gate", "name", "stage", "item_id"), "gate"),
            _event_outcome(payload, "observed"), _event_detail(payload)))
    return tuple(values), len(selected) > len(values)


def _artifact_facts(events: tuple[Any, ...]
                    ) -> tuple[tuple[CompletionFact, ...], bool]:
    selected = [event for event in events
                if _event_kind(event) == "artifact.created"]
    values = []
    for event in selected[-FACT_LIMIT:]:
        payload = _payload(event)
        label = _event_label(
            payload, ("name", "path", "uri", "artifact_id", "item_id"),
            "artifact")
        detail = _first_value(payload, ("media_type", "mime_type", "kind"))
        values.append(CompletionFact(label, "created", _text(detail, _DETAIL_BYTES)))
    return tuple(values), len(selected) > len(values)


def _diagnostic_facts(events: tuple[Any, ...]
                      ) -> tuple[tuple[CompletionFact, ...], bool]:
    kinds = {"runtime.warning", "runtime.error", "runtime.unknown_item"}
    selected = [event for event in events if _event_kind(event) in kinds]
    values = []
    for event in selected[-DIAGNOSTIC_LIMIT:]:
        payload = _payload(event)
        status = "failed" if _event_kind(event) == "runtime.error" else "warning"
        label = _first_value(payload, ("category", "stream", "type")) or _event_kind(event)
        values.append(CompletionFact(
            _text(label, _LABEL_BYTES), status, _event_detail(payload)))
    return tuple(values), len(selected) > len(values)


def _plan_facts(entries: tuple[Any, ...]
                ) -> tuple[bool, str, tuple[CompletionStep, ...], bool]:
    plans = [entry for entry in entries if getattr(entry, "kind", "") == "plan"]
    if not plans:
        return False, "", (), False
    plan = getattr(plans[-1], "plan", None)
    if plan is None:
        return True, "", (), len(plans) > 1
    raw_steps = tuple(getattr(plan, "steps", ()))
    steps = tuple(CompletionStep(
        _text(getattr(step, "text", "step"), _LABEL_BYTES),
        _text(getattr(step, "state", "unknown"), 64) or "unknown")
        for step in raw_steps[:FACT_LIMIT])
    truncated = bool(getattr(plan, "truncated", False)
                     or len(raw_steps) > len(steps) or len(plans) > 1)
    return True, _text(getattr(plan, "status", ""), 64), steps, truncated


def _workspace_values(workspace: Any
                      ) -> tuple[tuple[str, bool, int, tuple[str, ...], bool], bool]:
    phase = _text(_value(workspace, "phase", ""), 32)
    raw_paths = _value(workspace, "paths", ())
    raw_paths = tuple(raw_paths) if isinstance(raw_paths, (list, tuple)) else ()
    paths = tuple(_text(path, _LABEL_BYTES) for path in raw_paths[:PATH_LIMIT]
                  if _text(path, _LABEL_BYTES))
    truncated = bool(_value(workspace, "paths_truncated", False)
                     or len(raw_paths) > len(paths))
    changed = _value(workspace, "changed", False) is True
    dirty = _nonnegative_int(_value(workspace, "dirty_files", 0))
    head_changed = _value(workspace, "head_changed", False) is True
    return (phase, changed, dirty, paths, head_changed), truncated


def _usage_values(usage: Optional[Mapping[str, Any]], entries: tuple[Any, ...]
                  ) -> tuple[tuple[tuple[str, float], ...], bool]:
    values = dict(usage) if isinstance(usage, Mapping) else _timeline_usage(entries)
    finite = {str(name): number for name, value in values.items()
              if (number := _finite(value)) is not None}
    preferred = [name for name in (
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
        "output_tokens", "reasoning_output_tokens", "total_tokens") if name in finite]
    ordered = preferred + sorted(name for name in finite if name not in preferred)
    selected = ordered[:USAGE_LIMIT]
    return tuple((name, finite[name]) for name in selected), len(ordered) > len(selected)


def _timeline_usage(entries: tuple[Any, ...]) -> dict[str, Any]:
    usage = next((entry for entry in reversed(entries)
                  if getattr(entry, "kind", "") == "usage"), None)
    if usage is None:
        return {}
    try:
        value = json.loads(str(getattr(usage, "detail", "") or ""))
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _relevant_events(events: tuple[Any, ...], run_id: str,
                     turn_id: str) -> tuple[Any, ...]:
    values = []
    for event in events:
        if run_id and str(getattr(event, "run_id", "")) != run_id:
            continue
        event_turn = str(getattr(event, "turn_id", "") or "")
        if turn_id and event_turn and event_turn != turn_id:
            continue
        values.append(event)
    return tuple(values)


def _terminal_event(events: tuple[Any, ...]) -> Any:
    kinds = {"turn.completed", "turn.failed", "turn.interrupted"}
    return next((event for event in reversed(events)
                 if _event_kind(event) in kinds), None)


def _terminal_values(event: Any, result: Any
                     ) -> tuple[Optional[int], Optional[int], str]:
    payload = _payload(event)
    elapsed_ms = _milliseconds(payload, result)
    returncode = _int_or_none(payload.get(
        "returncode", _value(result, "returncode", None)))
    reason = _first_value(payload, ("reason", "error")) or _value(result, "reason", "")
    return elapsed_ms, returncode, _text(reason, _DETAIL_BYTES)


def _milliseconds(payload: Mapping[str, Any], result: Any) -> Optional[int]:
    direct = _finite(payload.get("elapsed_ms"))
    if direct is not None and direct >= 0:
        return int(direct)
    seconds = _finite(payload.get(
        "elapsed_seconds", _value(result, "elapsed", None)))
    return int(seconds * 1000) if seconds is not None and seconds >= 0 else None


def _event_key(event: Any, payload: Mapping[str, Any], prefix: str,
               index: int) -> str:
    identities = getattr(event, "extensions", {})
    identities = identities.get("journal_payload_identities", {}) \
        if isinstance(identities, Mapping) else {}
    for name in ("item_id", "call_id", "tool_call_id", "request_id", "id"):
        value = payload.get(name, identities.get(name) if isinstance(identities, Mapping)
                            else None)
        if value not in (None, ""):
            return f"{prefix}:{value}"
    label = _first_value(payload, ("validator", "tool", "name", "title"))
    return f"{prefix}:{label or index}"


def _event_label(payload: Mapping[str, Any], fields: tuple[str, ...],
                 fallback: str) -> str:
    return _text(_first_value(payload, fields) or fallback, _LABEL_BYTES)


def _event_outcome(payload: Mapping[str, Any], fallback: str) -> str:
    explicit = _first_value(payload, ("verdict", "status", "result", "outcome"))
    normalized = _text(explicit, 64).lower().replace(" ", "_").replace("-", "_")
    if normalized in {"pass", "passed", "success", "succeeded", "ok", "approved"}:
        return "passed"
    if normalized in {"fail", "failed", "error"}:
        return "failed"
    if normalized in {"reject", "rejected"}:
        return "rejected"
    if normalized in {"deny", "denied"}:
        return "denied"
    if normalized:
        return normalized
    success = payload.get("success", payload.get("passed", payload.get("ok")))
    if success is True:
        return "passed"
    if success is False:
        return "failed"
    code = _int_or_none(payload.get("exit_code", payload.get("returncode")))
    if code is not None:
        return "passed" if code == 0 else "failed"
    return "failed" if fallback == "failed" else fallback


def _event_detail(payload: Mapping[str, Any]) -> str:
    value = _first_value(payload, ("reason", "error", "message", "detail", "raw"))
    return _text(value, _DETAIL_BYTES)


def _truncation_names(events: tuple[Any, ...], turn_id: str, source: str,
                      **sections: bool) -> tuple[str, ...]:
    values = [name for name, truncated in sections.items() if truncated]
    saw_start = any(_event_kind(event) in {"turn.queued", "turn.started"}
                    for event in events)
    if turn_id and source == "runtime events" and not saw_start:
        values.append("runtime event window")
    return tuple(dict.fromkeys(values))


def _normalize_state(value: Any, result: Any) -> str:
    text = _text(value, 64).upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "SUCCESS": TurnState.SUCCEEDED, "SUCCEEDED": TurnState.SUCCEEDED,
        "IDLE": "", "READY": "", "CANCELLED": TurnState.INTERRUPTED,
        "CANCELLING": TurnState.INTERRUPTING,
    }
    normalized = aliases.get(text, text)
    if normalized:
        return normalized
    success = _value(result, "success", None)
    return (TurnState.SUCCEEDED if success is True else
            TurnState.FAILED if success is False else "")


def _event_kind(event: Any) -> str:
    return str(getattr(event, "kind", "") or "")


def _payload(event: Any) -> Mapping[str, Any]:
    value = getattr(event, "payload", {}) if event is not None else {}
    return value if isinstance(value, Mapping) else {}


def _first_value(values: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    return next((values[name] for name in names
                 if values.get(name) not in (None, "")), "")


def _value(value: Any, name: str, default: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default) if value is not None else default


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text(value: Any, maximum: int) -> str:
    text = one_line(clean_text(value))
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text
    return encoded[:maximum - 3].decode("utf-8", "ignore") + "…"


def _markdown(value: Any) -> str:
    text = _text(value, _DETAIL_BYTES)
    for character in "\\`*_{}[]<>#|":
        text = text.replace(character, "\\" + character)
    return text or "-"


def _code(value: Any) -> str:
    return _text(value, _DETAIL_BYTES).replace("`", "'") or "-"


def _status_marker(status: str) -> str:
    return {
        "passed": "✓", "created": "+", "completed": "•",
        "running": "…", "warning": "!", "not_completed": "○",
        "failed": "✗", "rejected": "✗", "denied": "✗", "blocked": "✗",
    }.get(status, "•")


def _duration(milliseconds: int) -> str:
    return f"{milliseconds / 1000:.2f}s"


def _number_text(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:.4g}"
