"""Typed, display-only effective runtime policy projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .tui_view_support import clean_text, one_line


POLICY_FIELDS = ("approval", "sandbox", "network")
MAX_POLICY_VALUE_BYTES = 256


@dataclass(frozen=True)
class PolicySnapshot:
    """Explicit provider policy values; unknown never becomes a local guess."""

    approval: str = "unknown"
    sandbox: str = "unknown"
    network: str = "unknown"
    source: str = ""

    @property
    def available(self) -> bool:
        return any(getattr(self, name) != "unknown" for name in POLICY_FIELDS)


class PolicyProjection:
    """Fold only typed ``policy.updated`` facts into an immutable snapshot."""

    def __init__(self) -> None:
        self._values = {name: "unknown" for name in POLICY_FIELDS}
        self._source = ""

    def reset(self) -> None:
        self._values = {name: "unknown" for name in POLICY_FIELDS}
        self._source = ""

    def validate(self, event: Any) -> None:
        if getattr(event, "kind", "") != "policy.updated":
            return
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict) or not payload:
            raise ValueError("policy.updated requires a non-empty object")
        unknown = set(payload).difference(POLICY_FIELDS)
        if unknown:
            raise ValueError("policy.updated contains unknown fields")
        for name, value in payload.items():
            _policy_value(value, name)

    def consume(self, event: Any) -> None:
        if getattr(event, "kind", "") != "policy.updated":
            return
        self.validate(event)
        for name, value in event.payload.items():
            self._values[name] = _policy_value(value, name)
        runtime = getattr(event, "extensions", {}).get("runtime", "")
        self._source = _bounded(runtime)

    def snapshot(self) -> PolicySnapshot:
        return PolicySnapshot(source=self._source, **self._values)


def _policy_value(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"policy {name} must be a string")
    text = one_line(clean_text(value))
    if not text or len(text.encode("utf-8")) > MAX_POLICY_VALUE_BYTES:
        raise ValueError(f"policy {name} is invalid")
    return text


def _bounded(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    encoded = value.encode("utf-8", errors="replace")
    return encoded[:MAX_POLICY_VALUE_BYTES].decode("utf-8", errors="ignore")
