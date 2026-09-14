"""Bounded, idempotent delivery of redacted Hub facts to Audit Governance."""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
from typing import Any

from .hub_ecosystem_http import IntegrationError, JSONTransport, canonical
from .hub_store_helpers import identifier

_CONFIG = {
    "name", "base_url", "tenant_id", "token_file", "source_system",
    "event_type", "schema_id", "schema_version", "data_classification",
    "retention_class", "timeout", "ca_file", "cert_file", "key_file",
    "max_response_bytes",
}
_FACT = {
    "tenant_id", "event_id", "cursor", "kind", "created_at", "instance_id",
    "session_id", "turn_id", "task_id", "actor_subject", "status",
    "trigger_subject",
}
_REQUIRED_FACT = _FACT - {"trigger_subject"}
_PAYLOAD = {
    "hub_event_id", "cursor", "kind", "created_at", "instance_id",
    "session_id", "turn_id", "task_id", "actor_subject", "trigger_subject",
    "status",
}
_ENVELOPE = {
    "event_id", "tenant_id", "source_system", "event_type", "schema_id",
    "schema_version", "occurred_at", "actor", "action", "outcome",
    "data_classification", "retention_class", "idempotency_key", "payload",
}
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_RECEIPTS = {"ledgered", "indexed", "archived"}


class AuditSink:
    """Prepare immutable Audit envelopes and deliver them with a verified receipt."""

    kind = "audit"

    def __init__(self, config: dict[str, Any]):
        if not isinstance(config, dict) or set(config) - _CONFIG:
            raise ValueError("unsupported Audit sink configuration")
        self.name = _token(config.get("name", "audit"), "name")
        self.tenant_id = _tenant(config.get("tenant_id"))
        self.source_system = _component(config.get("source_system"), "source_system")
        self.event_type = _token(config.get("event_type", "agent.lifecycle"), "event_type")
        self.schema_id = _component(config.get("schema_id", "agent-hub.lifecycle"), "schema_id")
        self.schema_version = _positive_int(config.get("schema_version", 1), "schema_version")
        self.data_classification = _token(
            config.get("data_classification", "internal"), "data_classification")
        self.retention_class = _token(config.get("retention_class", "standard"), "retention_class")
        self.transport = JSONTransport(
            config.get("base_url", ""), config.get("token_file", ""),
            timeout=config.get("timeout", 5.0), ca_file=config.get("ca_file", ""),
            cert_file=config.get("cert_file", ""), key_file=config.get("key_file", ""),
            max_response_bytes=config.get("max_response_bytes", 262144))
        self._mtls = bool(config.get("cert_file"))
        self.fingerprint = self._fingerprint()

    def __repr__(self) -> str:
        return (f"AuditSink(name={self.name!r}, tenant_id={self.tenant_id!r}, "
                f"base_url={self.transport.base_url!r}, token=<redacted>)")

    def prepare(self, fact: dict[str, Any]) -> dict[str, Any] | None:
        """Build a stable request from the exact redacted Hub fact contract."""
        safe = _validate_fact(fact, self.tenant_id)
        event_id = _audit_event_id(self.tenant_id, self.source_system, safe["event_id"])
        actor = safe.get("trigger_subject") or safe["actor_subject"]
        payload = {"hub_event_id": safe["event_id"]}
        payload.update({key: safe[key] for key in _PAYLOAD - {"hub_event_id"} if key in safe})
        return {
            "event_id": event_id,
            "tenant_id": self.tenant_id,
            "source_system": self.source_system,
            "event_type": self.event_type,
            "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "occurred_at": _rfc3339(safe["created_at"]),
            "actor": {"id": actor, "type": "user"},
            "action": safe["kind"],
            "outcome": safe["status"],
            "data_classification": self.data_classification,
            "retention_class": self.retention_class,
            "idempotency_key": event_id,
            "payload": payload,
        }

    def deliver(self, payload: dict[str, Any], delivery_id: str) -> dict[str, Any]:
        """POST one prepared envelope and return only a verified ledger receipt."""
        del delivery_id  # The deterministic Audit idempotency key is in the persisted envelope.
        event_id = _validate_prepared(payload, self)
        response = self.transport.request(
            "POST", "/api/v1/events?wait_for=ledgered", body=payload, expected=(202,))
        return _verified_receipt(response, event_id, self.tenant_id)

    def _fingerprint(self) -> str:
        target = {
            "name": self.name, "base_url": self.transport.base_url,
            "tenant_id": self.tenant_id, "source_system": self.source_system,
            "event_type": self.event_type, "schema_id": self.schema_id,
            "schema_version": self.schema_version,
            "data_classification": self.data_classification,
            "retention_class": self.retention_class,
            "tls": "mutual" if self._mtls else "server",
        }
        return "sha256:" + hashlib.sha256(canonical(target).encode("utf-8")).hexdigest()


def _validate_fact(fact: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    if not isinstance(fact, dict) or set(fact) - _FACT or _REQUIRED_FACT - set(fact):
        raise IntegrationError("invalid_fact", retryable=False)
    if fact["tenant_id"] != tenant_id:
        raise IntegrationError("tenant_mismatch", retryable=False)
    try:
        safe = {
            "tenant_id": tenant_id,
            "event_id": _text(fact["event_id"], "event_id", 256),
            "cursor": _positive_int(fact["cursor"], "cursor"),
            "kind": _token(fact["kind"], "kind"),
            "created_at": _epoch(fact["created_at"]),
            "actor_subject": _text(fact["actor_subject"], "actor_subject", 512),
            "status": _token(fact["status"], "status"),
        }
        for key in ("instance_id", "session_id", "turn_id", "task_id", "trigger_subject"):
            if key in fact and fact[key] not in (None, ""):
                safe[key] = _text(fact[key], key, 512)
    except ValueError:
        raise IntegrationError("invalid_fact", retryable=False) from None
    if not safe["actor_subject"]:
        raise IntegrationError("invalid_fact", retryable=False)
    return safe


def _validate_prepared(payload: dict[str, Any], sink: AuditSink) -> str:
    _validate_envelope_shape(payload)
    _validate_sink_binding(payload, sink)
    event_id = _validate_event_key(payload)
    _validate_actor(payload)
    _validate_fact_projection(payload, sink)
    return event_id


def _validate_envelope_shape(payload: Any) -> None:
    if not isinstance(payload, dict) or set(payload) != _ENVELOPE:
        raise IntegrationError("invalid_prepared_event", retryable=False)


def _validate_sink_binding(payload: dict[str, Any], sink: AuditSink) -> None:
    for name, value in (("tenant_id", sink.tenant_id), ("source_system", sink.source_system),
                        ("event_type", sink.event_type), ("schema_id", sink.schema_id),
                        ("schema_version", sink.schema_version),
                        ("data_classification", sink.data_classification),
                        ("retention_class", sink.retention_class)):
        if payload.get(name) != value:
            raise IntegrationError("invalid_prepared_event", retryable=False)


def _validate_event_key(payload: dict[str, Any]) -> str:
    event_id = payload.get("event_id")
    if (not isinstance(event_id, str) or not re.fullmatch(r"hub-[0-9a-f]{64}", event_id)
            or payload.get("idempotency_key") != event_id):
        raise IntegrationError("invalid_prepared_event", retryable=False)

    return event_id


def _validate_actor(payload: dict[str, Any]) -> None:
    actor = payload.get("actor")
    if not isinstance(actor, dict) or set(actor) != {"id", "type"}:
        raise IntegrationError("invalid_prepared_event", retryable=False)
    if actor.get("type") != "user" or not isinstance(actor.get("id"), str):
        raise IntegrationError("invalid_prepared_event", retryable=False)


def _validate_fact_projection(payload: dict[str, Any], sink: AuditSink) -> None:
    facts = payload.get("payload")
    if not isinstance(facts, dict) or set(facts) - _PAYLOAD:
        raise IntegrationError("invalid_prepared_event", retryable=False)
    if not {"hub_event_id", "cursor", "kind", "created_at", "actor_subject", "status"} <= set(facts):
        raise IntegrationError("invalid_prepared_event", retryable=False)
    fact = {"tenant_id": sink.tenant_id, "event_id": facts["hub_event_id"],
            "cursor": facts["cursor"], "kind": facts["kind"],
            "created_at": facts["created_at"], "actor_subject": facts["actor_subject"],
            "status": facts["status"]}
    for key in ("instance_id", "session_id", "turn_id", "task_id", "trigger_subject"):
        fact[key] = facts.get(key, "")
    try:
        rebuilt = sink.prepare(fact)
    except (IntegrationError, ValueError):
        raise IntegrationError("invalid_prepared_event", retryable=False) from None
    if rebuilt != payload:
        raise IntegrationError("invalid_prepared_event", retryable=False)


def _verified_receipt(response: dict[str, Any], event_id: str, tenant_id: str) -> dict[str, Any]:
    receipt = response.get("receipt") if isinstance(response, dict) else None
    if not isinstance(receipt, dict):
        raise IntegrationError("receipt_mismatch", status=202, retryable=False)
    if receipt.get("event_id") != event_id or receipt.get("tenant_id") != tenant_id:
        raise IntegrationError("receipt_mismatch", status=202, retryable=False)
    status = receipt.get("status")
    if status == "accepted":
        raise IntegrationError("ledger_pending", status=202, retryable=True)
    if status not in _TERMINAL_RECEIPTS:
        raise IntegrationError("receipt_mismatch", status=202, retryable=False)
    ledgered_at, digest = receipt.get("ledgered_at"), receipt.get("hash")
    if not _valid_timestamp(ledgered_at) or not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise IntegrationError("receipt_mismatch", status=202, retryable=False)
    sequence = receipt.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise IntegrationError("receipt_mismatch", status=202, retryable=False)
    stream_id, duplicate = receipt.get("stream_id"), receipt.get("duplicate", False)
    if not isinstance(stream_id, str) or not stream_id or not isinstance(duplicate, bool):
        raise IntegrationError("receipt_mismatch", status=202, retryable=False)
    return {
        "event_id": event_id, "tenant_id": tenant_id, "status": status,
        "ledgered_at": ledgered_at, "hash": digest,
        "stream_id": stream_id, "sequence": sequence, "duplicate": duplicate,
    }


def _audit_event_id(tenant: str, source: str, event_id: str) -> str:
    framed = "\0".join((tenant, source, event_id)).encode("utf-8")
    return "hub-" + hashlib.sha256(framed).hexdigest()


def _rfc3339(epoch: float) -> str:
    try:
        return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        raise IntegrationError("invalid_fact", retryable=False) from None


def _epoch(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise IntegrationError("invalid_fact", retryable=False)
    if value <= 0 or value > 10413792000:  # 2300-01-01 UTC, beyond Audit's ledger horizon.
        raise IntegrationError("invalid_fact", retryable=False)
    return float(value)


def _valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    earliest = dt.datetime(1900, 1, 1, tzinfo=dt.timezone.utc)
    latest = dt.datetime(2299, 12, 31, 23, 59, 59, 999999, tzinfo=dt.timezone.utc)
    return parsed.tzinfo is not None and earliest <= parsed <= latest


def _tenant(value: Any) -> str:
    text = _text(value, "tenant_id", 85)
    if (len(text.encode("utf-8")) > 85 or ":" in text
            or any(char.isspace() or char in "/\\" or ord(char) < 32 for char in text)):
        raise ValueError("tenant_id is invalid for Audit")
    return text


def _component(value: Any, label: str) -> str:
    text = _text(value, label, 85)
    if len(text.encode("utf-8")) > 85 or any(
            char.isspace() or char in "/\\" or ord(char) < 32 for char in text):
        raise ValueError(f"{label} is invalid for Audit")
    return text


def _token(value: Any, label: str) -> str:
    text = _text(value, label, 128)
    if not _TOKEN.fullmatch(text):
        raise ValueError(f"{label} must be a bounded identifier")
    return text


def _text(value: Any, label: str, maximum: int) -> str:
    return identifier(value, label, maximum)


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 2**63 - 1:
        raise ValueError(f"{label} must be a positive integer")
    return value
