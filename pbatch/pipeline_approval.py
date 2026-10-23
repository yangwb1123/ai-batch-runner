"""Human approval gate for pipeline stages (env / approve-file / TTY / native)."""

from __future__ import annotations

import sys
from pathlib import Path

from .config import log
from .runner_cancel import cancel_requested as _cancel_requested
from .tui_native import current_native_approval


def check_approval(stage, outputs: list, approval_file: str,
                   cancel_event=None) -> bool:
    """T12c (D5): human gate; a campaign cutoff refuses to start or accept a wait."""
    if _cancel_requested(cancel_event):
        log.warning("APPROVAL: stage '%s' refused after cancellation", stage.name)
        return False
    native = current_native_approval()
    if native is not None:
        if _cancel_requested(cancel_event):
            return False
        return (native.request(stage.name, outputs)
                and not _cancel_requested(cancel_event))
    import os as _os
    if _os.environ.get("PBATCH_APPROVE", "").lower() in ("1", "true", "yes"):
        log.info("APPROVAL: stage '%s' approved via PBATCH_APPROVE env", stage.name)
        return True
    if approval_file and Path(approval_file).exists():
        log.info("APPROVAL: stage '%s' approved via %s", stage.name, approval_file)
        return True
    log.info("APPROVAL: stage '%s' deliverables: %s", stage.name, ", ".join(outputs))
    try:
        if not sys.stdin.isatty():
            log.warning("APPROVAL: stage '%s' needs human approval but stdin is not a TTY "
                        "(use --approve-file or PBATCH_APPROVE=1)", stage.name)
            return False
        if _cancel_requested(cancel_event):
            log.warning("APPROVAL: stage '%s' cancelled before prompt", stage.name)
            return False
        reply = input(f"Approve stage '{stage.name}' outputs? (y/n): ").strip().lower()
        if _cancel_requested(cancel_event):
            return False
        return reply in ("y", "yes")
    except Exception:
        return False
