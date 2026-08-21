"""Identifiers shared by task execution and operational events.

The identifiers are deliberately opaque.  A task id is derived from the
run id and task identity without storing the prompt in the id, so retries can
share one task id while each attempt remains distinguishable in the event
ledger.
"""

from __future__ import annotations

import hashlib
import uuid


def new_run_id() -> str:
    """Return a short, human-searchable id for one CLI process run."""
    return "run-" + uuid.uuid4().hex[:16]


def task_id(run_id: str, prompt: str, output: str, cwd: str,
            index: int) -> str:
    """Derive a stable id for one task within a run.

    The prompt participates only in the digest.  The returned id contains no
    user content and remains stable when a retry appends validator feedback
    to the prompt, provided callers pass the original prompt identity.
    """
    material = "\0".join((run_id or "run-unknown", str(index), cwd or "",
                           output or "", prompt or ""))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return "task-" + digest
