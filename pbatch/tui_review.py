"""Small, bounded helpers for the TUI's read-only AI review command."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from . import config, review
from .tui_completion import CompletionSnapshot, render_completion
from .tui_findings import ReviewFinding
from .tui_protocol import RuntimeEvent


REVIEW_TIMEOUT_SECONDS = 600.0
REVIEW_VIEW_MAX_BYTES = 32 * 1024


@dataclass(frozen=True)
class ReviewRequest:
    """Validated scope passed to :func:`review.run_review`."""

    scope: str
    scope_argv: tuple[str, ...]


@dataclass(frozen=True)
class ReviewRuntimeScope:
    """Exact bounded identity for one review or fix turn."""

    workspace_id: str = ""
    session_id: str = ""
    run_id: str = ""
    turn_id: str = ""
    actor_id: str = ""

    @property
    def complete(self) -> bool:
        values = (self.workspace_id, self.session_id, self.run_id,
                  self.turn_id, self.actor_id)
        return all(isinstance(value, str) and value
                   and len(value.encode("utf-8", "replace")) <= 256
                   for value in values)

    @classmethod
    def from_mapping(cls, value: Any) -> "ReviewRuntimeScope":
        if not isinstance(value, Mapping):
            return cls()
        values: dict[str, str] = {}
        for name in ("workspace_id", "session_id", "run_id", "turn_id", "actor_id"):
            item = value.get(name)
            if (isinstance(item, str) and item
                    and len(item.encode("utf-8", "replace")) <= 256):
                values[name] = item
        return cls(**values)


@dataclass(frozen=True)
class ReviewFixContext:
    """Local association between a finding and its ordinary fix turn."""

    fix_id: str
    finding: ReviewFinding
    source_scope: ReviewRuntimeScope = ReviewRuntimeScope()
    source_report: str = ""


@dataclass(frozen=True)
class ReviewFixEvidence:
    """Post-fix facts; association is true only for an exact terminal turn."""

    context: ReviewFixContext
    fix_scope: ReviewRuntimeScope = ReviewRuntimeScope()
    state: str = "UNAVAILABLE"
    associated: bool = False
    completion: Optional[CompletionSnapshot] = None

    @property
    def source_associated(self) -> bool:
        scope = self.context.source_scope
        finding = self.context.finding
        same_session = (not self.fix_scope.complete
                        or (scope.workspace_id == self.fix_scope.workspace_id
                            and scope.session_id == self.fix_scope.session_id))
        return (scope.complete and same_session
                and (not finding.turn_id or finding.turn_id == scope.turn_id)
                and (not finding.actor_id or finding.actor_id == scope.actor_id))


def new_review_fix_context(
        finding: ReviewFinding, *, source_scope: Optional[ReviewRuntimeScope] = None,
        source_report: str = "") -> ReviewFixContext:
    """Create a bounded local fix id without treating review text as authority."""
    if not isinstance(finding, ReviewFinding):
        raise TypeError("finding must be a ReviewFinding")
    scope = source_scope or ReviewRuntimeScope()
    if not isinstance(scope, ReviewRuntimeScope):
        raise TypeError("source_scope must be a ReviewRuntimeScope")
    return ReviewFixContext(
        fix_id=f"fix-{uuid.uuid4().hex}", finding=finding,
        source_scope=scope, source_report=_workspace_report(source_report),
    )


def collect_review_fix_evidence(
        context: ReviewFixContext, *, fix_scope: ReviewRuntimeScope,
        completion: Optional[CompletionSnapshot]) -> ReviewFixEvidence:
    """Accept only an exact, terminal typed runtime completion as evidence."""
    if not isinstance(context, ReviewFixContext):
        raise TypeError("context must be a ReviewFixContext")
    if not isinstance(fix_scope, ReviewRuntimeScope):
        raise TypeError("fix_scope must be a ReviewRuntimeScope")
    source_scope = context.source_scope
    same_session = (not source_scope.complete
                    or (source_scope.workspace_id == fix_scope.workspace_id
                        and source_scope.session_id == fix_scope.session_id))
    matches = (isinstance(completion, CompletionSnapshot)
               and fix_scope.complete and same_session and completion.terminal
               and all(getattr(completion, name) == getattr(fix_scope, name)
                       for name in ("run_id", "turn_id", "actor_id")))
    return ReviewFixEvidence(
        context=context, fix_scope=fix_scope,
        state=(completion.state if isinstance(completion, CompletionSnapshot)
                else "UNAVAILABLE"),
        associated=bool(matches), completion=completion if matches else None,
    )


def review_fix_output_path(cwd: Path, fix_id: str) -> Path:
    """Return a private runtime path for one bounded post-fix report."""
    if (not isinstance(fix_id, str) or not fix_id.startswith("fix-")
            or not fix_id[4:] or not all(char.isalnum() or char == "-"
                                         for char in fix_id[4:])):
        raise ValueError("invalid review fix id")
    root = Path(cwd).expanduser().resolve()
    return (config.data_dir(str(root)) / "reviews" /
            f"review-fix-{fix_id[4:]}.md")


def render_review_fix_evidence(evidence: ReviewFixEvidence) -> str:
    """Render typed post-fix evidence without claiming resolution."""
    if not isinstance(evidence, ReviewFixEvidence):
        raise TypeError("evidence must be a ReviewFixEvidence")
    finding = evidence.context.finding
    lines = [
        "# Review fix evidence", "",
        f"- Fix ID: `{_markdown_value(evidence.context.fix_id)}`",
        f"- State: `{_markdown_value(evidence.state)}`",
        f"- Exact fix-turn association: {'yes' if evidence.associated else 'no'}",
        f"- Source review association: {'yes' if evidence.source_associated else 'no'}",
        "- Finding resolution: not claimed; run a follow-up review to reassess.",
        "", "## Finding", "",
        f"- Severity: {_markdown_value(finding.severity)}",
        f"- File: `{_markdown_value(finding.file)}`",
        f"- Line: {_markdown_value(finding.line or 'unspecified')}",
        f"- Problem: {_markdown_value(finding.problem or '(not provided)')}",
        f"- Evidence: {_markdown_value(finding.evidence or '(not provided)')}",
        f"- Fix hint: {_markdown_value(finding.fix_hint or '(not provided)')}",
    ]
    if evidence.context.source_report:
        lines.append(f"- Source report: `{_markdown_value(evidence.context.source_report)}`")
    for name in ("event_id", "turn_id", "actor_id"):
        value = getattr(finding, name)
        if value:
            lines.append(f"- Finding {name}: `{_markdown_value(value)}`")
    lines.extend(_scope_lines("Source review scope", evidence.context.source_scope))
    lines.extend(_scope_lines("Fix turn scope", evidence.fix_scope))
    if evidence.completion is None:
        lines.extend(["", "## Typed post-fix runtime evidence", "",
                      "No exact terminal RuntimeEvent completion was available; "
                      "no validation, gate, or workspace pass is claimed."])
    else:
        lines.extend(["", "## Typed post-fix runtime evidence", "",
                      render_completion(evidence.completion)])
    return _bounded_markdown("\n".join(lines))


def write_review_fix_evidence(path: Path, evidence: ReviewFixEvidence) -> None:
    """Persist the bounded evidence report only after the fix turn ends."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_review_fix_evidence(evidence), encoding="utf-8")


def review_fix_summary(evidence: ReviewFixEvidence) -> str:
    """Return a concise truthful transcript notice for one fix turn."""
    if not isinstance(evidence, ReviewFixEvidence):
        raise TypeError("evidence must be a ReviewFixEvidence")
    completion = evidence.completion
    validation_count = len(completion.validations) if completion else 0
    gate_count = len(completion.gates) if completion else 0
    source = "typed runtime evidence" if evidence.associated else "typed evidence unavailable"
    return (f"Review fix `{evidence.context.fix_id}` finished: `{evidence.state}`; "
            f"{source}; validation events={validation_count}, gate events={gate_count}; "
            "finding remains unresolved until a follow-up review.")


def _scope_lines(title: str, scope: ReviewRuntimeScope) -> list[str]:
    lines = ["", f"## {title}", ""]
    if not any((scope.workspace_id, scope.session_id, scope.run_id,
                scope.turn_id, scope.actor_id)):
        return lines + ["No exact runtime scope is available."]
    for name in ("workspace_id", "session_id", "run_id", "turn_id", "actor_id"):
        value = getattr(scope, name)
        if value:
            lines.append(f"- {name}: `{_markdown_value(value)}`")
    return lines


def _workspace_report(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\\", "/").strip()
    if (not text or text.startswith("/")
            or any(part == ".." for part in text.split("/"))):
        return ""
    return review._bounded_text(text, 4096)


def _markdown_value(value: Any) -> str:
    text = review._bounded_text(value, 4096).replace("`", "\\`")
    return text.replace("\n", " ") or "-"


def _bounded_markdown(value: str) -> str:
    maximum = 64 * 1024
    marker = "\n\n_[Review fix evidence truncated.]_\n"
    encoded = value.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return value
    kept = encoded[:maximum - len(marker.encode("utf-8"))]
    return kept.decode("utf-8", "ignore") + marker


class AppServerReviewCapture(review.ReviewCapture):
    """Bounded review evidence collected from trusted RuntimeEvents."""

    def __init__(self) -> None:
        super().__init__()
        self.typed_findings: list[dict[str, Any]] = []


def capture_runtime_review_event(
        capture: AppServerReviewCapture, event: RuntimeEvent) -> None:
    """Collect review evidence without treating it as execution control."""
    if not isinstance(capture, AppServerReviewCapture):
        raise TypeError("capture must be an AppServerReviewCapture")
    if not isinstance(event, RuntimeEvent):
        raise TypeError("event must be a RuntimeEvent")
    kind = event.kind
    payload = event.payload
    if kind == "turn.started":
        capture.turn_started = True
        return
    if kind == "turn.completed":
        capture.turn_completed = True
        return
    if kind in {"message.started", "message.completed"}:
        capture.item_counts["agent_message"] += 1
        if kind == "message.completed":
            capture._append_message(payload.get("text", ""))
        return
    if kind in {"command.started", "command.completed"}:
        capture.item_counts["command_execution"] += 1
        return
    if kind == "file.changed":
        capture.item_counts["file_change"] += 1
        capture.flagged_writes = True
        return
    if kind == "review.finding":
        value = payload.get("finding", payload)
        if isinstance(value, Mapping) and len(capture.typed_findings) < review.INVALID_MAX_ITEMS:
            capture.typed_findings.append(dict(value))


def runtime_review_payload(
        scope: str, output: Path, capture: AppServerReviewCapture,
        outcome: review.RunOutcome, runtime_scope: Optional[Mapping[str, Any]] = None
        ) -> dict[str, Any]:
    """Build the common report shape and merge typed findings exactly once."""
    payload = review._payload(scope, output, capture, outcome)
    if capture.typed_findings:
        findings, invalid = review.extract_findings(capture.messages)
        known = {_finding_key(item) for item in findings}
        for index, value in enumerate(capture.typed_findings, 1):
            normalized = review._normalize_finding(
                value, invalid, f"runtime finding {index}")
            if normalized is not None and _finding_key(normalized) not in known:
                findings.append(normalized)
                known.add(_finding_key(normalized))
        summary_invalid = invalid
        invalid = (invalid + capture.invalid_events)[:review.INVALID_MAX_ITEMS]
        payload["findings"] = findings
        payload["invalid_findings"] = invalid
        payload["summary"] = review._summary(
            capture, outcome, findings, summary_invalid)
    if isinstance(runtime_scope, Mapping):
        exact_scope = ReviewRuntimeScope.from_mapping(runtime_scope)
        payload["runtime_scope"] = {
            name: getattr(exact_scope, name)
            for name in ("workspace_id", "session_id", "run_id", "turn_id", "actor_id")
            if getattr(exact_scope, name)
        }
    return payload


def _finding_key(value: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(value.get(name, "")) for name in review.FINDING_FIELDS)


def parse_review_args(args: Sequence[str]) -> ReviewRequest:
    """Parse the small TUI scope vocabulary without invoking a shell."""

    values = tuple(str(value) for value in args)
    if not values:
        return ReviewRequest("default", ())
    mode = values[0].lower()
    if mode == "uncommitted" and len(values) == 1:
        return ReviewRequest("uncommitted", ("--uncommitted",))
    if mode in {"base", "commit"} and len(values) == 2 and values[1].strip():
        return ReviewRequest(f"{mode}:{values[1]}", (f"--{mode}", values[1]))
    if mode == "custom" and len(values) > 1:
        custom = " ".join(values[1:]).strip()
        if custom:
            return ReviewRequest("custom", (custom,))
    raise ValueError(
        "usage: /review [uncommitted|base BRANCH|commit SHA|custom TEXT]")


def review_output_path(cwd: Path) -> Path:
    """Return a unique private runtime report path for the workspace."""

    root = Path(cwd).expanduser().resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    return config.data_dir(str(root)) / "reviews" / f"review-{stamp}-{suffix}.md"


def write_review_report(path: Path, payload: dict[str, Any], timed_out: bool) -> None:
    """Persist the same bounded Markdown representation as headless review."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(review._markdown(payload, timed_out), encoding="utf-8")


def render_review(payload: dict[str, Any], timed_out: bool) -> str:
    """Render a bounded transcript view without promoting findings to verdicts."""

    text = review._markdown(payload, timed_out)
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= REVIEW_VIEW_MAX_BYTES:
        return text
    marker = "\n\n_[Review display truncated; report file contains the bounded full output.]_\n"
    marker_bytes = marker.encode("utf-8")
    kept = encoded[:max(0, REVIEW_VIEW_MAX_BYTES - len(marker_bytes))]
    return kept.decode("utf-8", "ignore") + marker
