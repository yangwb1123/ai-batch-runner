"""Fail-closed binding for native plan evidence and lifecycle events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_NATIVE_RUNTIME = "pbatch-native"
_MAX_ID_BYTES = 256
_DIGEST_BYTES = 64
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class PlanEvidenceSnapshot:
    """Current native plan identity; display evidence, never an execution grant."""

    plan_id: str = ""
    plan_digest: str = ""


class PlanEvidenceProjection:
    """Require native lifecycle evidence to retain one exact plan digest."""

    def __init__(self) -> None:
        self._plan_id = self._plan_digest = ""

    def reset(self) -> None:
        self._plan_id = self._plan_digest = ""

    def validate(self, event: Any) -> None:
        if not self._is_native(event):
            return
        extensions = getattr(event, "extensions", {})
        claimed_id = extensions.get("plan_id", "")
        claimed_digest = extensions.get("plan_digest", "")
        if not claimed_id and not claimed_digest:
            if self._plan_digest and getattr(event, "kind", "") != "plan.updated":
                raise ValueError("native event is missing current plan evidence")
            return
        _identity(claimed_id, "plan_id")
        _digest(claimed_digest)
        if getattr(event, "kind", "") == "plan.updated":
            payload = getattr(event, "payload", {})
            if (payload.get("plan_id") != claimed_id
                    or payload.get("plan_digest") != claimed_digest):
                raise ValueError("native plan event evidence does not match payload")
            return
        if not self._plan_digest:
            raise ValueError("native event has plan evidence without a plan")
        if (claimed_id != self._plan_id
                or claimed_digest != self._plan_digest):
            raise ValueError("native event plan evidence does not match current plan")

    def consume(self, event: Any) -> None:
        if not self._is_native(event) or event.kind != "plan.updated":
            return
        self.validate(event)
        extensions = event.extensions
        if extensions.get("plan_digest"):
            self._plan_id = extensions["plan_id"]
            self._plan_digest = extensions["plan_digest"]

    def snapshot(self) -> PlanEvidenceSnapshot:
        return PlanEvidenceSnapshot(self._plan_id, self._plan_digest)

    @staticmethod
    def _is_native(event: Any) -> bool:
        extensions = getattr(event, "extensions", {})
        return (isinstance(extensions, dict)
                and extensions.get("runtime") == _NATIVE_RUNTIME
                and extensions.get("read_only") is True)


def _identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value:
        raise ValueError(f"native {label} is invalid")
    if len(value.encode("utf-8")) > _MAX_ID_BYTES:
        raise ValueError(f"native {label} is too long")
    return value


def _digest(value: Any) -> str:
    if (not isinstance(value, str) or len(value) != _DIGEST_BYTES
            or any(char not in _HEX for char in value)):
        raise ValueError("native plan digest is invalid")
    return value
