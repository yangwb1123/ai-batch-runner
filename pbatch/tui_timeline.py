"""Bounded, dependency-free timeline projection for the interactive TUI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import time
from typing import Optional


_ENTRY_KINDS = frozenset({
    "command", "file", "plan", "usage", "message", "status",
    "pipeline", "stage", "task", "validation", "gate", "actor", "review",
})
_SUMMARY_BYTES = 512
_DETAIL_BYTES = 2 * 1024
_PLAN_ID_BYTES = 256
_PLAN_ITEM_BYTES = 256
_PLAN_NOTICE_BYTES = 64 * 1024
_PLAN_ITEMS = 64
_PLAN_BOUNDARY_ITEMS = 32
_PLAN_BOUNDARY_BYTES = 512
_PLAN_BUDGET_ITEMS = 16
_ANONYMOUS_PLAN = "__current_plan__"
_PLAN_DONE = frozenset({"complete", "completed", "done", "success", "succeeded"})
_PLAN_ACTIVE = frozenset({"active", "in_progress", "running", "started"})
_PLAN_FAILED = frozenset({"blocked", "cancelled", "failed", "rejected"})
_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _bounded(value: object, maximum: int) -> str:
    text = _ANSI_ESCAPE.sub("", str(value or ""))
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text
    marker = "…"
    prefix = encoded[:maximum - len(marker.encode())].decode("utf-8", "ignore")
    return prefix + marker


@dataclass(frozen=True)
class PlanStep:
    """One bounded, provider-neutral step in the current plan projection."""

    text: str
    state: str
    step_id: str = ""
    actor: str = ""
    dependencies: tuple[str, ...] = ()

    @property
    def completed(self) -> bool:
        return self.state == "completed"

    def render_line(self) -> str:
        markers = {
            "completed": ("✓", "completed"),
            "in_progress": ("…", "in progress"),
            "pending": ("○", "pending"),
        }
        marker, label = markers.get(
            self.state, ("✗", self.state.replace("_", " ")))
        extras = []
        if self.actor:
            extras.append(f"actor={self.actor}")
        if self.dependencies:
            extras.append("depends=" + ",".join(self.dependencies[:4]))
        suffix = " · " + " · ".join(extras) if extras else ""
        return f"{marker} {label} · {self.text}{suffix}"


@dataclass(frozen=True)
class PlanSnapshot:
    """Structured plan and bounded execution-boundary metadata."""

    status: str = ""
    steps: tuple[PlanStep, ...] = ()
    truncated: bool = False
    plan_id: str = ""
    plan_digest: str = ""
    goal: str = ""
    read_scope: tuple[str, ...] = ()
    write_scope: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    network_intent: tuple[str, ...] = ()
    external_tools: tuple[str, ...] = ()
    validators: tuple[str, ...] = ()
    gates: tuple[str, ...] = ()
    budget: tuple[tuple[str, str], ...] = ()
    assumptions: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    stop_policy: str = ""
    source: str = ""
    version: str = ""
    boundaries_truncated: bool = False

    @property
    def summary(self) -> str:
        completed = sum(step.completed for step in self.steps)
        value = f"{completed}/{len(self.steps)} completed"
        suffix = ""
        if self.truncated:
            suffix += " · truncated"
        if self.boundaries_truncated:
            suffix += " · boundaries truncated"
        return value + suffix


@dataclass
class TimelineEntry:
    """One trusted runtime item rendered by :class:`TimelineLog`."""

    kind: str
    ts: float | str
    summary: str
    detail: str = ""
    exit_code: Optional[int] = None
    collapsed: bool = False
    plan: Optional[PlanSnapshot] = None

    def __post_init__(self) -> None:
        if self.kind not in _ENTRY_KINDS:
            raise ValueError(f"unsupported timeline entry kind: {self.kind}")
        if (self.exit_code is not None and
                (isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int))):
            raise TypeError("exit_code must be an integer or None")
        self.summary = _bounded(self.summary, _SUMMARY_BYTES)
        self.detail = _bounded(self.detail, _DETAIL_BYTES)
        self.collapsed = bool(self.collapsed)
        if self.plan is not None and not isinstance(self.plan, PlanSnapshot):
            raise TypeError("plan must be a PlanSnapshot or None")


class TimelineLog:
    """Current-turn timeline with deterministic folding and bounded retention."""

    def __init__(self, max_entries: int = 500):
        if (isinstance(max_entries, bool) or not isinstance(max_entries, int)
                or max_entries <= 0):
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self._entries: list[TimelineEntry] = []
        self._commands: dict[str, TimelineEntry] = {}
        self._plans: dict[str, TimelineEntry] = {}
        self._command_sequence = 0
        self._file_entry: Optional[TimelineEntry] = None
        self._file_paths: list[str] = []
        self._file_paths_truncated = False
        self._usage_entry: Optional[TimelineEntry] = None
        self._lifecycle: dict[tuple[str, str], TimelineEntry] = {}
        self._lifecycle_sequence = 0

    @property
    def entries(self) -> tuple[TimelineEntry, ...]:
        return tuple(self._entries)

    def append(self, entry: TimelineEntry) -> bool:
        if not isinstance(entry, TimelineEntry):
            raise TypeError("entry must be a TimelineEntry")
        if len(self._entries) >= self.max_entries:
            removable = next((index for index, item in enumerate(self._entries)
                              if not self._running(item)), None)
            if removable is None:
                return False
            self._forget(self._entries.pop(removable))
        self._entries.append(entry)
        self.collapse_rules()
        return True

    def _forget(self, entry: TimelineEntry) -> None:
        self._commands = {key: value for key, value in self._commands.items()
                          if value is not entry}
        self._plans = {key: value for key, value in self._plans.items()
                       if value is not entry}
        if self._file_entry is entry:
            self._file_entry, self._file_paths = None, []
            self._file_paths_truncated = False
        if self._usage_entry is entry:
            self._usage_entry = None
        self._lifecycle = {
            key: value for key, value in self._lifecycle.items()
            if value is not entry}

    def consume(self, kind: str, text: str, ts: float | str | None = None) -> bool:
        """Project one backend notification without deriving types from body text."""
        normalized = str(kind or "").lower()
        when = time.time() if ts is None else ts
        if normalized == "command":
            self._consume_command(_notice(text), when)
        elif normalized == "command-output":
            self._consume_command_output(_notice(text), when)
        elif normalized == "file":
            self._consume_file(_notice(text), when)
        elif normalized == "plan":
            notice = _plan_notice(text)
            if notice is None:
                return False
            self._consume_plan(notice, when)
        elif normalized == "usage":
            self._consume_usage(_notice(text), when)
        elif normalized in {
                "pipeline", "stage", "task", "validation", "gate", "actor", "review"}:
            self._consume_lifecycle(normalized, _notice(text), when)
        elif normalized == "stdout":
            clean = str(text or "")
            self.append(TimelineEntry("message", when, _first_line(clean), clean))
        elif normalized in {"stderr", "status"}:
            clean = str(text or "")
            self.append(TimelineEntry("status", when, _first_line(clean), clean))
        else:
            return False
        self.collapse_rules()
        return True

    def _consume_command(self, notice: dict, when: float | str) -> None:
        phase = str(notice.get("phase") or "")
        item_id = self._command_key(notice.get("item_id"), phase == "started")
        command = str(notice.get("command") or "command")
        entry = self._commands.get(item_id)
        if entry is None:
            entry = TimelineEntry("command", when, command)
            if self.append(entry):
                self._commands[item_id] = entry
        elif command != "command":
            entry.summary = _bounded(command, _SUMMARY_BYTES)
        if phase == "completed":
            code = notice.get("exit_code")
            if isinstance(code, int) and not isinstance(code, bool):
                entry.exit_code = code
            else:
                status = str(notice.get("status") or "").lower()
                entry.exit_code = 0 if status in {"completed", "succeeded", "success"} else -1

    def _consume_command_output(self, notice: dict, when: float | str) -> None:
        item_id = self._command_key(notice.get("item_id"), False)
        entry = self._commands.get(item_id)
        if entry is None:
            self._consume_command({"item_id": item_id}, when)
            entry = self._commands.get(item_id)
        if entry is not None:
            separator = "\n" if entry.detail else ""
            entry.detail = _bounded(entry.detail + separator +
                                    str(notice.get("output") or ""), _DETAIL_BYTES)

    def _command_key(self, value: object, force_new: bool) -> str:
        item_id = str(value or "")
        if item_id:
            return item_id
        if not force_new:
            running = next((key for key, entry in reversed(self._commands.items())
                            if entry.exit_code is None), "")
            if running:
                return running
        self._command_sequence += 1
        return f"command-{self._command_sequence}"

    def _consume_file(self, notice: dict, when: float | str) -> None:
        paths = notice.get("paths", [])
        paths = [str(path) for path in paths] if isinstance(paths, list) else []
        combined = list(dict.fromkeys(self._file_paths + paths))
        self._file_paths_truncated = bool(
            self._file_paths_truncated or notice.get("truncated")
            or len(combined) > self.max_entries)
        self._file_paths = combined[:self.max_entries]
        summary = ", ".join(self._file_paths[:4]) or "file change"
        if len(self._file_paths) > 4 or self._file_paths_truncated:
            summary += ", …"
        if self._file_entry is None:
            entry = TimelineEntry("file", when, summary, "\n".join(self._file_paths))
            if self.append(entry):
                self._file_entry = entry
        else:
            self._file_entry.summary = _bounded(summary, _SUMMARY_BYTES)
            self._file_entry.detail = _bounded("\n".join(self._file_paths), _DETAIL_BYTES)

    def _consume_plan(self, notice: dict, when: float | str) -> None:
        item_id = _bounded(notice.get(
            "plan_identity", notice.get("item_id", notice.get("plan_id", ""))),
            _PLAN_ID_BYTES)
        key = item_id or _ANONYMOUS_PLAN
        raw_items = notice.get("items", [])
        raw_items = raw_items if isinstance(raw_items, list) else []
        items = raw_items[:_PLAN_ITEMS]
        steps = tuple(_plan_step(item, index) for index, item in enumerate(items, 1))
        truncated = bool(notice.get("truncated") or len(raw_items) > len(items))
        plan = PlanSnapshot(
            status=_bounded(notice.get("status", ""), _PLAN_ITEM_BYTES),
            steps=steps,
            truncated=truncated,
            **_plan_boundaries(notice, item_id or key),
        )
        detail = "\n".join(step.render_line() for step in steps)
        entry = self._plans.get(key)
        if entry is None:
            entry = TimelineEntry("plan", when, plan.summary, detail, plan=plan)
            if self.append(entry):
                self._plans[key] = entry
            return
        entry.ts = when
        entry.summary = _bounded(plan.summary, _SUMMARY_BYTES)
        entry.detail = _bounded(detail, _DETAIL_BYTES)
        entry.plan = plan

    def _consume_lifecycle(self, kind: str, notice: dict,
                           when: float | str) -> None:
        """Coalesce trusted stage/gate/actor notices by their item identity."""
        phase = str(notice.get("phase", "") or "").lower()
        identity = next((str(notice.get(name, "") or "") for name in (
            "item_id", "validator_id", "gate_id", "actor_id", "id")
                         if notice.get(name)), "")
        if not identity or (phase == "started" and identity.startswith("anonymous-")):
            self._lifecycle_sequence += 1
            identity = f"anonymous-{self._lifecycle_sequence}"
        key = kind, identity
        entry = self._lifecycle.get(key)
        label = next((str(notice.get(name, "") or "") for name in (
            "name", "validator", "gate", "actor", "title", "task")
                      if notice.get(name)), kind)
        detail = next((str(notice.get(name, "") or "") for name in (
            "detail", "reason", "message", "error") if notice.get(name)), "")
        if entry is None:
            entry = TimelineEntry(kind, when, label, detail)
            if not self.append(entry):
                return
            self._lifecycle[key] = entry
        else:
            entry.ts = when
            entry.summary = _bounded(label, _SUMMARY_BYTES)
            if detail:
                entry.detail = _bounded(detail, _DETAIL_BYTES)
        if _lifecycle_terminal(kind, notice, phase):
            entry.exit_code = _lifecycle_exit(notice)

    def _consume_usage(self, notice: dict, when: float | str) -> None:
        summary = _usage_summary(notice)
        detail = json.dumps(notice.get("usage", {}), ensure_ascii=False,
                            sort_keys=True, default=str)
        if self._usage_entry is None:
            entry = TimelineEntry("usage", when, summary, detail)
            if self.append(entry):
                self._usage_entry = entry
        else:
            self._usage_entry.summary = _bounded(summary, _SUMMARY_BYTES)
            self._usage_entry.detail = _bounded(detail, _DETAIL_BYTES)

    @staticmethod
    def _running(entry: TimelineEntry) -> bool:
        return entry.kind in {
            "command", "pipeline", "stage", "task", "validation", "gate", "actor"} \
            and entry.exit_code is None

    def collapse_rules(self) -> None:
        """Apply the Phase-1 default density without inspecting body text."""
        for entry in self._entries:
            if entry.kind in {
                    "command", "pipeline", "stage", "task", "validation", "gate", "actor"}:
                entry.collapsed = entry.exit_code == 0
            elif entry.kind == "plan":
                entry.collapsed = False
            elif entry.kind in {"file", "usage", "message", "status", "review"}:
                entry.collapsed = True

    def render_lines(self, width: int) -> list[str]:
        """Return ANSI-free lines, each hard-bounded to ``width`` columns."""
        if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
            return []
        lines: list[str] = []
        for entry in self._entries:
            lines.append(_truncate(self._headline(entry), width))
            if not entry.collapsed and entry.detail:
                details = entry.detail.splitlines() or [entry.detail]
                lines.extend(_truncate(f"  {line}", width) for line in details)
        return lines

    @staticmethod
    def _headline(entry: TimelineEntry) -> str:
        label = entry.kind.upper()
        outcome = ""
        if entry.kind in {
                "command", "pipeline", "stage", "task", "validation", "gate", "actor"}:
            if entry.exit_code is None:
                outcome = " …"
            elif entry.exit_code == 0:
                outcome = " ✓"
            else:
                outcome = f" ✗ exit {entry.exit_code}"
        return f"[{_timestamp(entry.ts)}] {label}{outcome} {entry.summary}".rstrip()

    def to_summary(self) -> str:
        """Project command/file/usage/failure facts for the current turn."""
        return _build_summary(self._entries)

    def render_summary(self, width: int) -> str:
        return _truncate(self.to_summary(), max(1, int(width)))


def _build_summary(entries: list[TimelineEntry]) -> str:
    commands = _entries_of(entries, "command")
    files = _unique_file_paths(entries)
    usage = _latest_summary(entries, "usage", "usage unavailable")
    plan = _latest_summary(entries, "plan", "")
    failures = [entry.summary for entry in commands
                if entry.exit_code is not None and entry.exit_code != 0]
    summary = f"{len(commands)} command(s) · {len(files)} file(s) · {usage}"
    summary += _count_summary(entries)
    if plan:
        summary += f" · plan {plan}"
    if failures:
        summary += " · failures: " + ", ".join(failures[:3])
        if len(failures) > 3:
            summary += ", …"
    return _bounded(summary, _SUMMARY_BYTES)


def _entries_of(entries: list[TimelineEntry], kind: str) -> list[TimelineEntry]:
    return [entry for entry in entries if entry.kind == kind]


def _unique_file_paths(entries: list[TimelineEntry]) -> set[str]:
    return {
        path.strip() for entry in entries if entry.kind == "file"
        for path in entry.detail.splitlines() if path.strip()
    }


def _latest_summary(entries: list[TimelineEntry], kind: str,
                    fallback: str) -> str:
    return next((entry.summary for entry in reversed(entries)
                 if entry.kind == kind), fallback)


def _count_summary(entries: list[TimelineEntry]) -> str:
    values = []
    for kind, label in (("pipeline", "pipeline"), ("stage", "stage"),
                        ("task", "task"), ("validation", "validation"),
                        ("gate", "gate"), ("actor", "actor")):
        count = sum(entry.kind == kind for entry in entries)
        if count:
            values.append(f"{count} {label}(s)")
    return " · " + " · ".join(values) if values else ""


def _timestamp(value: float | str) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).strftime("%H:%M:%S")
        except (OverflowError, OSError, ValueError):
            pass
    return _bounded(value, 32) or "--:--:--"


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[:width - 1] + "…"


def _notice(value: str) -> dict:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _plan_notice(value: str) -> Optional[dict]:
    """Parse one plan notice without accepting an unbounded JSON document."""
    text = str(value or "")
    if len(text.encode("utf-8", "replace")) > _PLAN_NOTICE_BYTES:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _plan_step(value: object, index: int) -> PlanStep:
    item = value if isinstance(value, dict) else {}
    raw_status = item.get("status", item.get("state", ""))
    status = str(raw_status or "").strip().lower().replace("-", "_")
    completed = item.get("completed") is True or status in _PLAN_DONE
    if completed:
        state = "completed"
    elif status in _PLAN_ACTIVE:
        state = "in_progress"
    elif status in _PLAN_FAILED:
        state = status
    else:
        state = "pending"
    raw_text = next((item.get(name) for name in ("text", "title", "name")
                     if item.get(name) is not None), value)
    text = " ".join(str(raw_text or "").split()) or f"step {index}"
    return PlanStep(
        _bounded(text, _PLAN_ITEM_BYTES), state,
        _plan_text(item.get("step_identity", item.get(
            "id", item.get("step_id", item.get("stepId", "")))),
                   _PLAN_ID_BYTES),
        _plan_text(item.get("actor", item.get("actor_identity", item.get(
            "actor_id", item.get("actorId", "")))), _PLAN_BOUNDARY_BYTES),
        _plan_values(item.get("dependencies", item.get(
            "depends_on", item.get("dependsOn")))))


def _plan_boundaries(notice: dict, plan_id: str) -> dict:
    """Copy explicit plan boundaries without deriving execution authority."""
    return {
        "plan_id": _plan_text(plan_id, _PLAN_ID_BYTES),
        "plan_digest": _plan_text(_plan_field(notice, "plan_digest"), 64),
        "goal": _plan_text(_plan_field(notice, "goal", "objective"),
                            _PLAN_ITEM_BYTES),
        "read_scope": _plan_values(_plan_field(notice, "read_scope", "readScope")),
        "write_scope": _plan_values(_plan_field(notice, "write_scope", "writeScope")),
        "commands": _plan_values(_plan_field(notice, "commands")),
        "network_intent": _plan_values(
            _plan_field(notice, "network_intent", "networkIntent")),
        "external_tools": _plan_values(
            _plan_field(notice, "external_tools", "externalTools")),
        "validators": _plan_values(_plan_field(notice, "validators")),
        "gates": _plan_values(_plan_field(notice, "gates")),
        "budget": _plan_budget(_plan_field(notice, "budget")),
        "assumptions": _plan_values(_plan_field(notice, "assumptions")),
        "unknowns": _plan_values(_plan_field(notice, "unknowns")),
        "stop_policy": _plan_text(
            _plan_field(notice, "stop_policy", "stopPolicy"), _PLAN_ITEM_BYTES),
        "source": _plan_text(_plan_field(notice, "source"), _PLAN_ITEM_BYTES),
        "version": _plan_text(
            _plan_field(notice, "version", "plan_version", "revision"), 128),
        "boundaries_truncated": _plan_boundary_truncated(notice),
    }


def _plan_field(notice: dict, *names: str) -> object:
    return next((notice[name] for name in names if name in notice), None)


def _plan_boundary_truncated(notice: dict) -> bool:
    if notice.get("boundaries_truncated") is True:
        return True
    scalar_fields = (
        (("goal", "objective"), _PLAN_ITEM_BYTES),
        (("stop_policy", "stopPolicy"), _PLAN_ITEM_BYTES),
        (("source",), _PLAN_ITEM_BYTES),
        (("version", "plan_version", "revision"), 128),
    )
    if any(_plan_text_too_long(_plan_field(notice, *names), maximum)
           for names, maximum in scalar_fields):
        return True
    fields = (
        ("read_scope", "readScope"), ("write_scope", "writeScope"),
        ("commands",), ("network_intent", "networkIntent"),
        ("external_tools", "externalTools"), ("validators",),
        ("gates",), ("assumptions",), ("unknowns",),
    )
    if any(_plan_value_truncated(_plan_field(notice, *names))
           for names in fields):
        return True
    budget = _plan_field(notice, "budget")
    return (isinstance(budget, dict) and (
        len(budget) > _PLAN_BUDGET_ITEMS or any(
            _plan_text_too_long(key, 128) or
            _plan_text_too_long(_plan_atom(value), _PLAN_BOUNDARY_BYTES)
            for key, value in tuple(budget.items())[:_PLAN_BUDGET_ITEMS])))


def _plan_value_truncated(value: object) -> bool:
    if isinstance(value, str):
        return _plan_text_too_long(value, _PLAN_BOUNDARY_BYTES)
    if not isinstance(value, (list, tuple)):
        return False
    return (len(value) > _PLAN_BOUNDARY_ITEMS or any(
        _plan_text_too_long(_plan_atom(item), _PLAN_BOUNDARY_BYTES)
        for item in value[:_PLAN_BOUNDARY_ITEMS]))


def _plan_text_too_long(value: object, maximum: int) -> bool:
    try:
        text = "" if value is None else str(value)
        return len(text.encode("utf-8", "replace")) > maximum
    except (TypeError, ValueError):
        return True


def _plan_text(value: object, maximum: int) -> str:
    text = "" if value is None else str(value)
    return _bounded(" ".join(text.split()), maximum)


def _plan_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple)):
        values = tuple(value[:_PLAN_BOUNDARY_ITEMS])
    else:
        values = ()
    return tuple(text for item in values
                 if (text := _plan_text(_plan_atom(item), _PLAN_BOUNDARY_BYTES)))


def _plan_atom(value: object) -> object:
    if not isinstance(value, dict):
        return value
    for name in ("id", "name", "title", "path", "command", "value"):
        if name in value:
            return value[name]
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _plan_budget(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict):
        return ()
    rows = []
    for key, item in tuple(value.items())[:_PLAN_BUDGET_ITEMS]:
        name = _plan_text(key, 128)
        text = _plan_text(_plan_atom(item), _PLAN_BOUNDARY_BYTES)
        if name and text:
            rows.append((name, text))
    return tuple(rows)


def _first_line(value: str) -> str:
    clean = " ".join(str(value or "").split())
    return _bounded(clean, _SUMMARY_BYTES) or "message"


def _lifecycle_terminal(kind: str, notice: dict, phase: str) -> bool:
    if kind == "gate":
        return bool(notice.get("verdict") or phase in {"completed", "failed"})
    if kind == "actor":
        return phase in {"completed", "failed", "finished", "stopped"}
    return phase in {"completed", "failed", "finished", "stopped"}


def _lifecycle_exit(notice: dict) -> int:
    value = notice.get("exit_code")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    verdict = str(notice.get("verdict", "") or "").lower()
    status = str(notice.get("status", "") or "").lower()
    success = notice.get("success") is True
    return 0 if success or verdict in {"pass", "passed", "ok", "success"} or \
        status in {"completed", "succeeded", "success", "passed", "ok"} else -1


def _usage_summary(notice: dict) -> str:
    usage = notice.get("usage", {})
    usage = usage if isinstance(usage, dict) else {}
    total = notice.get("total_tokens")
    parts = []
    for name in ("input_tokens", "cached_input_tokens", "output_tokens",
                 "reasoning_output_tokens"):
        if name in usage:
            parts.append(f"{name.removesuffix('_tokens')} {usage[name]}")
    if isinstance(total, (int, float)) and not isinstance(total, bool):
        parts.append(f"total {total} tokens")
    return " · ".join(parts) or "usage unavailable"
