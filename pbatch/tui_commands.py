"""Command mixins for the product TUI.

Keeping review and workspace commands outside the application shell leaves the
interactive lifecycle small enough for the repository's file budget while
preserving the public ``PiBatchTUI`` class and command names.
"""

from __future__ import annotations

import os
from pathlib import Path
import queue
import time
import threading
import uuid
from typing import Any, Optional

from . import review
from .tui_lifecycle import (
    TUIRuntimeLifecycleMixin, next_runtime_event, operation_timeout, require_terminal_projection)
from .product import format_product_brief, product_brief
from .tui_review import (
    REVIEW_TIMEOUT_SECONDS,
    AppServerReviewCapture,
    ReviewRequest,
    capture_runtime_review_event,
    collect_review_fix_evidence,
    new_review_fix_context,
    parse_review_args,
    render_review,
    review_fix_output_path,
    review_fix_summary,
    review_output_path,
    runtime_review_payload,
    ReviewRuntimeScope,
    write_review_fix_evidence,
    write_review_report,
)
from .tui_actor_controls import TUIActorControlMixin
from .tui_findings import (
    FindingsSnapshot, ReviewFinding, finding_fix_prompt, findings_snapshot)
from .tui_review_findings import TUIReviewFindingsScreen
from .tui_pipeline import TUIPipelineMixin
from .tui_view_support import inline_label as _inline_label
from .tui_view_support import short_path as _short_path


class TUIProductMixin:
    """Render a local product-direction proposal without an agent turn."""

    def _slash_product(self, args: tuple[str, ...]) -> None:
        requirement = " ".join(args).strip() or self._latest_task_prompt()
        if not requirement:
            self._usage("product", "<requirement>")
            return
        try:
            brief = product_brief(requirement)
            rendered = format_product_brief(brief)
        except Exception as exc:
            self._append_entry("error", f"Product brief failed: {exc}")
            return
        self._append_entry("system", rendered)
        self._last_status = "Product direction ready"
        self._refresh_status()

    def _latest_task_prompt(self) -> str:
        return str(getattr(self, "_last_task_prompt", "") or "")

    def _palette_product(self) -> None:
        self._slash_product(())


class TUIReviewMixin:
    """Run and render one bounded, read-only AI review."""

    def _init_review_controls(self) -> None:
        self._review_busy = False
        self._review_runtime = False
        self._review_handle = None
        self._review_output = None
        self._review_cancel = threading.Event()
        self._review_started, self._review_finished = threading.Event(), threading.Event()
        self._review_source_scope = ReviewRuntimeScope()
        self._review_source_report = ""
        self._pending_review_fix = None
        self._review_fix_evidence = None

    def _run_review_worker(self, request: ReviewRequest, cwd: Path,
                           output: Path) -> None:
        self._review_started.set()
        operation = self._runtime_operation if self._review_runtime else None
        if self._review_runtime:
            payload, outcome, error = self._run_appserver_review(request, output)
        else:
            try:
                payload, outcome = review.run_review(
                    request.scope, request.scope_argv, cwd,
                    operation_timeout(self, REVIEW_TIMEOUT_SECONDS), output,
                    runner=self._review_runner, cancellation=self._review_cancel)
                error = None
            except Exception as exc:
                payload, outcome, error = {}, None, exc
        error = self._finish_runtime_operation(operation, error)
        # Signal provider teardown before call_from_thread: unmount may be
        # synchronously waiting here and cannot dispatch a UI callback.
        self._review_finished.set()
        if error is None:
            try:
                write_review_report(output, payload, bool(outcome.timed_out))
            except Exception as exc:
                error = exc
        try:
            self.call_from_thread(self._finish_review, payload, outcome, output, error)
        except RuntimeError:
            pass  # application closed while the review was unwinding

    def _run_appserver_review(
            self, request: ReviewRequest, output: Path
            ) -> tuple[dict[str, Any], Any, Optional[Exception]]:
        capture = AppServerReviewCapture()
        started = time.monotonic()
        try:
            self._runtime_operation.check()
            adapter = self._adapter
            if adapter is None:
                raise RuntimeError("App Server review adapter is unavailable")
            stream = adapter.events()
            handle = adapter.start_review({
                "scope": request.scope, "scope_argv": request.scope_argv,
                "workspace_id": self._runtime_view.workspace_id,
                "session_id": self._adapter_session_id,
                "run_id": f"review-run-{uuid.uuid4().hex}",
                "turn_id": f"review-turn-{uuid.uuid4().hex}",
                **self._runtime_submission_request(),
            })
            self._bind_runtime_submission(handle)
            self._review_handle = handle
            self._runtime_operation.bind(handle)
            terminal_kind, terminal_payload = self._consume_appserver_review(
                stream, handle, capture)
            outcome = review.RunOutcome(
                0 if terminal_kind == "turn.completed" else 1,
                max(0, int((time.monotonic() - started) * 1000)),
                stderr=str(terminal_payload.get("reason", "") or ""))
            scope = {
                "workspace_id": self._runtime_view.workspace_id,
                "session_id": handle.session_id, "run_id": handle.run_id,
                "turn_id": handle.turn_id, "actor_id": handle.actor_id,
            }
            return runtime_review_payload(
                request.scope, output, capture, outcome, scope), outcome, None
        except Exception as exc:
            return {}, None, exc
        finally:
            self._review_handle = None

    def _consume_appserver_review(
            self, stream: Any, handle: Any,
            capture: AppServerReviewCapture) -> tuple[str, dict[str, Any]]:
        while True:
            self._runtime_operation.check()
            try:
                event = next_runtime_event(stream, handle, "App Server review")
            except queue.Empty:
                continue
            self._runtime_operation.check()
            if event is None:
                raise RuntimeError("App Server review event stream closed")
            kind, payload = self._runtime_event_context(handle, event)
            terminal = kind in {"turn.completed", "turn.failed", "turn.interrupted"}
            admitted = self._ingest_runtime_event(event)
            require_terminal_projection(kind, admitted)
            if admitted is not False:
                capture_runtime_review_event(capture, event)
            if terminal:
                self._runtime_operation.complete()
                return kind, payload

    def _finish_review(self, payload: dict[str, Any], outcome: Any,
                       output: Path, error: Optional[Exception]) -> None:
        runtime_mode = self._review_runtime
        if error is not None:
            self._reconcile_runtime_failure(str(error))
            self._review_source_scope = ReviewRuntimeScope()
            self._review_source_report = ""
            self._append_entry("error", f"Review failed: {error}")
            self._last_status = "Review failed"
        else:
            self._review_source_scope = ReviewRuntimeScope.from_mapping(
                payload.get("runtime_scope") if isinstance(payload, dict) else None)
            self._review_source_report = _workspace_relative(
                output, self._review_cwd())
            projected = (getattr(self._runtime_view, "findings", FindingsSnapshot())
                         if runtime_mode else FindingsSnapshot())
            typed_findings = tuple(
                finding for finding in projected.findings
                if (not self._review_source_scope.turn_id
                    or finding.turn_id == self._review_source_scope.turn_id))
            self._review_findings = (FindingsSnapshot(
                typed_findings, projected.skipped, projected.truncated)
                if typed_findings else findings_snapshot(
                    payload.get("findings", ()) if isinstance(payload, dict) else ()))
            body = render_review(payload, bool(outcome.timed_out))
            self._append_entry(
                "system", f"{body}\n\nReport: `{_short_path(str(output), 120)}`")
            count = len(payload.get("findings", ()))
            self._last_status = ("Review cancelled" if outcome.cancelled else
                                 "Review timed out" if outcome.timed_out else
                                 f"Review finished ({count} finding(s); inspect report)")
        self._review_busy = False
        self._review_runtime = False
        self._review_handle = None
        self._review_output = None
        self._worker = None
        self._set_busy(False)
        self._mark_agent_operation_finished()

    def _cancel_review(self, *, wait: bool = False) -> None:
        self._review_cancel.set()
        if self._review_runtime and self._runtime_operation is not None:
            self._runtime_operation.cancel()
        if wait and self._review_started.is_set():
            self._review_finished.wait(timeout=3)
        self._last_status = "Review cancellation requested"
        if self.is_mounted and not getattr(self, "_unmounting", False):
            self._refresh_status()

    def _slash_review(self, args: tuple[str, ...]) -> bool:
        try:
            request = parse_review_args(args)
        except ValueError as exc:
            self._append_entry("error", str(exc))
            return False
        return self._start_review(request)

    def _appserver_review_mode(self) -> tuple[bool, str]:
        adapter = getattr(self, "_adapter", None)
        if not getattr(self, "_strict_adapter", False) or adapter is None:
            return False, ""
        try:
            info = adapter.runtime_info()
            runtime = str(getattr(info, "runtime", "") or "")
            if runtime != "codex-app-server":
                return False, ""
            capabilities = getattr(info, "capabilities", None)
            supports = getattr(capabilities, "supports", None)
            if not callable(supports) or not supports("review"):
                return False, "Codex App Server did not declare review capability."
            if not callable(getattr(adapter, "start_review", None)):
                return False, "Codex App Server review operation is unavailable."
            return True, ""
        except Exception as exc:
            return False, f"Unable to inspect App Server review capability: {exc}"

    def _start_review(self, request: ReviewRequest) -> bool:
        if self._busy or self._native_pipeline_is_running():
            self.notify("Wait for the active operation before starting a review.",
                        severity="warning")
            return False
        use_runtime, blocked = self._appserver_review_mode()
        if blocked:
            self._append_entry("error", blocked)
            self._last_status = "Review unavailable"
            self._refresh_status()
            return False
        if use_runtime and not self._prepare_runtime_action(review=True):
            return False
        cwd = self._review_cwd()
        output = review_output_path(cwd)
        self._review_runtime = use_runtime
        self._review_cancel = threading.Event()
        self._review_started, self._review_finished = threading.Event(), threading.Event()
        if use_runtime:
            self._begin_runtime_operation("AI review", REVIEW_TIMEOUT_SECONDS)
        self._review_busy = True
        self._review_findings = FindingsSnapshot()
        self._review_output = output
        self._last_status = f"Review running ({request.scope})"
        self._set_busy(True)
        self._mark_agent_operation_started(
            "review", f"Read-only review: {request.scope}")
        self._append_entry("system", f"Starting read-only AI review: `{request.scope}`")
        self._worker = self.run_worker(
            lambda: self._run_review_worker(request, cwd, output),
            name="review", group="agent", exit_on_error=False,
            exclusive=True, thread=True)
        return True

    def _review_cwd(self) -> Path:
        value = getattr(self._backend, "cwd", "") or self.options.cwd or os.getcwd()
        return Path(str(value)).expanduser().resolve()

    def _slash_findings(self, args: tuple[str, ...]) -> None:
        if not self._require_no_args("findings", args):
            return
        snapshot = getattr(self, "_review_findings", None)
        if not isinstance(snapshot, FindingsSnapshot):
            snapshot = (getattr(self._runtime_view, "findings", FindingsSnapshot())
                        if getattr(self, "_projection_driven", False)
                        else FindingsSnapshot())
        self.push_screen(TUIReviewFindingsScreen(
            snapshot, self._open_finding_changes, self._fix_finding))

    def _fix_finding(self, finding: ReviewFinding) -> None:
        if not isinstance(finding, ReviewFinding):
            self.notify("The selected finding is invalid.", severity="error")
            return
        if self._busy or self._native_pipeline_is_running():
            self.notify(
                "Finding fixes require an idle ordinary agent turn.",
                severity="warning")
            return
        try:
            prompt = finding_fix_prompt(finding)
            context = new_review_fix_context(
                finding, source_scope=self._review_source_scope,
                source_report=self._review_source_report)
        except (TypeError, ValueError) as exc:
            self.notify(f"Unable to prepare finding fix: {exc}", severity="error")
            return
        self._pending_review_fix = context
        if self._start_turn(prompt):
            self._last_status = f"Fixing finding in {finding.file}"
            self._refresh_status()
        else:
            self._pending_review_fix = None

    def _collect_review_fix_evidence(self) -> None:
        context = self._pending_review_fix
        if context is None:
            return
        self._pending_review_fix = None
        view = getattr(self, "_runtime_view", None)
        typed = bool(getattr(self, "_projection_driven", False))
        controller = getattr(self, "_event_controller", None)
        if typed and controller is not None:
            try:
                view = controller.snapshot()
            except (RuntimeError, TypeError):
                typed = False
        scope = (ReviewRuntimeScope(
            workspace_id=getattr(view, "workspace_id", ""),
            session_id=getattr(view, "session_id", ""),
            run_id=getattr(view, "run_id", ""),
            turn_id=getattr(view, "turn_id", ""),
            actor_id=getattr(view, "actor_id", "")) if typed else ReviewRuntimeScope())
        completion = getattr(view, "completion", None) if typed else None
        evidence = collect_review_fix_evidence(
            context, fix_scope=scope, completion=completion)
        self._review_fix_evidence = evidence
        report = None
        try:
            report = review_fix_output_path(self._review_cwd(), context.fix_id)
            write_review_fix_evidence(report, evidence)
        except (OSError, ValueError, RuntimeError) as exc:
            self._append_entry("error", f"Review fix evidence could not be written: {exc}")
        notice = review_fix_summary(evidence)
        if report is not None:
            notice += f" Report: `{_short_path(str(report), 120)}`"
        self._append_entry("system", notice)

    def _open_finding_changes(self, finding: ReviewFinding) -> None:
        if not isinstance(finding, ReviewFinding):
            self.notify("The selected finding is invalid.", severity="error")
            return
        if self._busy or self._native_pipeline_is_running():
            self.notify(
                "Finding navigation is available after the active operation settles.",
                severity="warning")
            return
        try:
            from .tui_changes import ChangesRequest, TUIChangesScreen
            request = ChangesRequest(
                scope="all", path=finding.file, line=finding.line)
            snapshot = self._load_changes_snapshot(request)
            self.push_screen(TUIChangesScreen(
                snapshot, self._load_changes_snapshot))
        except Exception as exc:
            self.notify(f"Unable to open finding location: {exc}", severity="error")

    def _palette_review(self) -> None:
        self._slash_review(())

    def _palette_findings(self) -> None:
        self._slash_findings(())

    def _refresh_findings_inspector(self) -> None:
        current = self.screen
        if not isinstance(current, TUIReviewFindingsScreen):
            return
        snapshot = getattr(self, "_review_findings", None)
        if not isinstance(snapshot, FindingsSnapshot):
            snapshot = (getattr(self._runtime_view, "findings", FindingsSnapshot())
                        if getattr(self, "_projection_driven", False)
                        else FindingsSnapshot())
        current.refresh_snapshot(snapshot)


def _workspace_relative(path: Path, cwd: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(cwd).resolve()).as_posix()
    except (OSError, ValueError):
        return ""


class TUIWorkspaceMixin:
    """Render bounded workspace status and diff commands."""

    def _slash_files(self, args: tuple[str, ...]) -> None:
        if not self._require_no_args("files", args):
            return
        payload = self._workspace_call("workspace_status")
        if payload is not None:
            from .tui_agents import render_workspace_files
            self._append_entry("system", render_workspace_files(payload))
            self._last_status = "Workspace refreshed"
            self._refresh_status()

    def _slash_diff(self, args: tuple[str, ...]) -> None:
        if len(args) > 1:
            self._usage("diff", "[path]")
            return
        payload = self._workspace_call("workspace_diff", args[0] if args else "")
        if payload is not None:
            from .tui_agents import render_workspace_diff
            self._append_entry("system", render_workspace_diff(payload))
            self._last_status = "Diff refreshed"
            self._refresh_status()

    def _slash_refresh(self, args: tuple[str, ...]) -> None:
        if not self._require_no_args("refresh", args):
            return
        payload = self._workspace_call("workspace_status")
        if payload is not None:
            changed = payload.get("changed", 0) if isinstance(payload, dict) else 0
            self._last_status = f"Workspace refreshed ({changed} changed)"
            self._refresh_status()
            self.notify(self._last_status)

    def action_show_diff(self) -> None:
        if self._busy:
            self.notify("Wait for the active turn before reviewing changes.",
                        severity="warning")
            return
        self._slash_diff(())

    def _palette_files(self) -> None:
        self._slash_files(())

    def _palette_refresh(self) -> None:
        self._slash_refresh(())

    def _workspace_call(self, method_name: str, *args: str) -> Optional[dict]:
        if self._busy or self._native_pipeline_is_running():
            self.notify("Wait for the active operation before reviewing changes.",
                        severity="warning")
            return None
        method = getattr(self._backend, method_name, None)
        if not callable(method):
            self._append_entry("error", "Workspace review is unavailable for this backend.")
            return None
        try:
            payload = method(*args)
        except Exception as exc:
            self._append_entry("error", f"Workspace review failed: {exc}")
            return None
        return payload if isinstance(payload, dict) else {"error": str(payload)}

    def _append_workspace_change(self) -> None:
        delta = self._projected_workspace_delta()
        if delta is None:
            snapshot = self._backend_snapshot()
            delta = snapshot.get("last_turn_workspace", {})
        if not isinstance(delta, dict) or not delta.get("changed"):
            return
        paths = [str(path) for path in delta.get("paths", [])]
        details = ", ".join(f"`{_inline_label(path)}`" for path in paths[:6])
        if delta.get("paths_truncated") or len(paths) > 6:
            details += ", …"
        suffix = f" Files: {details}." if details else ""
        head = " HEAD changed." if delta.get("head_changed") else ""
        self._append_entry(
            "system", f"Workspace changed; {delta.get('dirty_files', 0)} dirty file(s)."
            f"{head}{suffix} Use `/changes` to inspect.")
