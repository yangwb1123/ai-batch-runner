"""Aero IM status-card sink for durable Agent Hub lifecycle facts."""

from __future__ import annotations

import hashlib
import math
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

from .hub_ecosystem_http import IntegrationError, JSONTransport, canonical

_EVENT_NAMESPACE = uuid.UUID("a6990362-4269-4fb8-b898-73fd5e44f41b")
_TERMINAL = {
    "compute.completed": "completed",
    "compute.failed": "failed",
    "compute.cancelled": "cancelled",
    "compute.lost": "lost",
    "turn.completed": "completed",
    "turn.failed": "failed",
    "turn.interrupted": "interrupted",
    "turn.cancelled": "cancelled",
    "turn.lost": "lost",
}
_STATUS_LABEL = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "lost": "lost",
    "interrupted": "interrupted",
}
_CONFIG_FIELDS = {
    "name", "tenant_id", "base_url", "installation_id", "token_file",
    "recipient_subject_map",
    "timeout", "ca_file", "cert_file", "key_file",
}


@dataclass(frozen=True)
class _NotificationFact:
    tenant_id: str
    event_id: str
    kind: str
    instance_id: str
    session_id: str
    turn_id: str
    task_id: str
    actor_subject: str
    status: str


class MessagingSink:
    """Publish bounded terminal task/turn cards to a trusted Aero IM installation."""

    name = "aero-im"
    kind = "im"

    def __init__(self, config: dict[str, Any]):
        if not isinstance(config, dict) or set(config) - _CONFIG_FIELDS:
            raise ValueError("unsupported Aero IM sink settings")
        self.name = _identifier(config.get("name", "aero-im"), "name", 64)
        self.tenant_id = _identifier(config.get("tenant_id"), "tenant_id")
        self.base_url = config.get("base_url", "")
        installation = config.get("installation_id", "")
        try:
            self.installation_id = str(uuid.UUID(installation))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("installation_id must be a UUID") from None
        self.token_file = config.get("token_file", "")
        if not isinstance(self.token_file, str) or not self.token_file:
            raise ValueError("Aero IM token_file is required")
        self.recipient_subject_map = _subject_map(config.get("recipient_subject_map"))
        self.timeout = config.get("timeout", 5.0)
        self.ca_file = _optional_path(config.get("ca_file", ""), "ca_file")
        self.cert_file = _optional_path(config.get("cert_file", ""), "cert_file")
        self.key_file = _optional_path(config.get("key_file", ""), "key_file")
        self.transport = JSONTransport(
            self.base_url, self.token_file, timeout=self.timeout,
            ca_file=self.ca_file, cert_file=self.cert_file, key_file=self.key_file,
            max_response_bytes=262144,
        )
        identity = canonical({
            "tenant_id": self.tenant_id,
            "base_url": self.transport.base_url,
            "installation_id": self.installation_id,
        })
        self.fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return (f"MessagingSink(tenant_id={self.tenant_id!r}, "
                f"installation_id={self.installation_id!r}, token=<redacted>)")

    def prepare(self, fact: dict[str, Any]) -> dict[str, Any] | None:
        """Freeze the recipient and safe display payload before outbox persistence."""
        event = _fact(fact)
        if event.tenant_id != self.tenant_id:
            raise IntegrationError("tenant_mismatch", retryable=False)
        status = _TERMINAL.get(event.kind)
        if status is None:
            return None
        if event.status != status:
            raise IntegrationError("invalid_fact", retryable=False)
        subject = self.recipient_subject_map.get(event.actor_subject)
        if subject is None:
            raise IntegrationError("recipient_unmapped", retryable=False)
        noun = "task" if event.kind.startswith("compute.") else "turn"
        lines = [f"Agent Hub {noun} {_STATUS_LABEL[status]}"]
        for label, value in (
            ("Task", event.task_id), ("Session", event.session_id),
            ("Turn", event.turn_id), ("Instance", event.instance_id),
            ("Event", event.event_id),
        ):
            if value:
                lines.append(f"{label}: {value}")
        return {
            "actor_subject": event.actor_subject,
            "expected_subject": subject,
            "event_id": event.event_id,
            "target": {"type": "snaplink_user", "subject": subject},
            "blocks": [{"type": "text", "content": "\n".join(lines)}],
        }

    def deliver(self, payload: dict[str, Any], delivery_id: str) -> dict[str, Any]:
        """Post a prepared card with the outbox's stable IM idempotency key."""
        prepared = _prepared(payload)
        current = self.recipient_subject_map.get(prepared["actor_subject"])
        if current != prepared["expected_subject"]:
            raise IntegrationError("recipient_binding_changed", retryable=False)
        try:
            idem = str(uuid.UUID(delivery_id))
        except (ValueError, TypeError, AttributeError):
            raise IntegrationError("invalid_delivery_id", retryable=False) from None
        path = ("/api/integrations/v1/installations/" +
                urllib.parse.quote(self.installation_id, safe="") + "/notifications")
        body = {"target": prepared["target"], "blocks": prepared["blocks"]}
        result = self.transport.request(
            "POST", path, body=body,
            headers={"Idempotency-Key": idem}, expected=(200, 201),
            retry_codes=("integration_request_pending",),
        )
        return _receipt(result)


def _fact(value: Any) -> _NotificationFact:
    if not isinstance(value, dict):
        raise IntegrationError("invalid_fact", retryable=False)
    required = (
        "tenant_id", "event_id", "cursor", "kind", "created_at", "instance_id",
        "session_id", "turn_id", "task_id", "actor_subject", "status",
    )
    try:
        if any(key not in value for key in required):
            raise ValueError("missing lifecycle field")
        cursor = value["cursor"]
        created_at = value["created_at"]
        if (isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0
                or isinstance(created_at, bool)
                or not isinstance(created_at, (int, float))
                or not math.isfinite(created_at)):
            raise ValueError("invalid lifecycle ordering fields")
        return _NotificationFact(
            tenant_id=_identifier(value["tenant_id"], "tenant_id"),
            event_id=_identifier(value["event_id"], "event_id"),
            kind=_identifier(value["kind"], "kind"),
            instance_id=_optional_identifier(value["instance_id"], "instance_id"),
            session_id=_optional_identifier(value["session_id"], "session_id"),
            turn_id=_optional_identifier(value["turn_id"], "turn_id"),
            task_id=_optional_identifier(value["task_id"], "task_id"),
            actor_subject=_identifier(value["actor_subject"], "actor_subject"),
            status=_identifier(value["status"], "status"),
        )
    except (TypeError, ValueError):
        raise IntegrationError("invalid_fact", retryable=False) from None


def _prepared(value: Any) -> dict[str, Any]:
    keys = {"actor_subject", "expected_subject", "event_id", "target", "blocks"}
    if not isinstance(value, dict) or set(value) != keys:
        raise IntegrationError("invalid_prepared_notification", retryable=False)
    try:
        actor = _identifier(value["actor_subject"], "actor_subject")
        expected = _identifier(value["expected_subject"], "expected_subject")
        event_id = _identifier(value["event_id"], "event_id")
        target = value["target"]
        blocks = value["blocks"]
        if target != {"type": "snaplink_user", "subject": expected}:
            raise ValueError("target does not match frozen recipient")
        if not isinstance(blocks, list) or len(blocks) != 1:
            raise ValueError("notification must contain exactly one text block")
        block = blocks[0]
        if (not isinstance(block, dict) or set(block) != {"type", "content"}
                or block.get("type") != "text"):
            raise ValueError("notification block is not plain text")
        content = _message_content(block["content"])
        return {
            "actor_subject": actor, "expected_subject": expected,
            "event_id": event_id, "target": target,
            "blocks": [{"type": "text", "content": content}],
        }
    except (TypeError, ValueError):
        raise IntegrationError("invalid_prepared_notification", retryable=False) from None


def _receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise IntegrationError("invalid_receipt", retryable=False)
    message = value.get("message")
    message_id = message.get("id") if isinstance(message, dict) else None
    if (not isinstance(message_id, str) or not message_id or len(message_id) > 128
            or not isinstance(value.get("deduplicated"), bool)):
        raise IntegrationError("invalid_receipt", retryable=False)
    return {"message_id": message_id, "deduplicated": value["deduplicated"]}


def _subject_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not value or len(value) > 10000:
        raise ValueError("recipient_subject_map must be a non-empty object")
    result = {}
    for actor, subject in value.items():
        actor = _identifier(actor, "actor_subject")
        subject = _identifier(subject, "IM subject", 2048)
        result[actor] = subject
    return result


def _identifier(value: Any, name: str, maximum: int = 256) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError(f"{name} is invalid")
    return value


def _optional_identifier(value: Any, name: str, maximum: int = 256) -> str:
    return "" if value is None or value == "" else _identifier(value, name, maximum)


def _message_content(value: Any) -> str:
    if (not isinstance(value, str) or not value
            or len(value.encode("utf-8")) > 4096
            or any((ord(char) < 32 and char != "\n") or ord(char) == 127 for char in value)):
        raise ValueError("notification content is invalid")
    return value


def _optional_path(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a path")
    return value


__all__ = ["MessagingSink"]
