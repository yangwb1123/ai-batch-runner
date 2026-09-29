"""Identity and scalar helpers shared by command artifact storage."""

from __future__ import annotations

import hashlib
from typing import Any


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _output_bytes(value: Any) -> bytes:
    return str(value if value is not None else "").encode("utf-8", "replace")


def _context_key(context: Any) -> str:
    raw = "\0".join((
        context.workspace_id, context.session_id, context.run_id,
        context.turn_id, context.item_id)).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def _identity_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:24]
