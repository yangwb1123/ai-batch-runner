"""Opaque, token-keyed comparison of locally loaded pool configuration and keys.

This is a drift check, not remote attestation: a process holding the local token
can forge it. Never log the canonical payload or tag, and never persist either.
The gateway captures it once; doctor compares with its own bounded local reads.
"""

from __future__ import annotations

from dataclasses import asdict, fields
import hmac
import json
import re


SNAPSHOT_HEADER = "X-Pbatch-Pool-Snapshot"
_DOMAIN = b"pbatch-llm-pool-loaded-snapshot-v1\0"


def snapshot_tag(settings, token, keys, *, port=None):
    """Pure comparison token; inputs must be validated, complete local snapshots."""
    expected = {account.id for account in settings.accounts if account.enabled}
    if keys is None or set(keys) != expected:
        raise ValueError("complete enabled credentials required for snapshot comparison")
    payload = {field.name: getattr(settings, field.name) for field in fields(settings)}
    payload.update(accounts=[asdict(account) for account in settings.accounts],
                   models=dict(settings.models), token_file=str(settings.token_file),
                   state_file=str(settings.state_file), credentials=dict(keys),
                   port=settings.port if port is None else port)
    for name in ("request_timeout", "probe_after", "usage_max_age", "usage_timeout"):
        payload[name] = float(payload[name])  # YAML 60 and 60.0 have identical semantics.
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False,
                         separators=(",", ":")).encode("ascii")
    return "v1:" + hmac.digest(token.encode("ascii"), _DOMAIN + encoded, "sha256").hex()


def valid_tag(value):
    return isinstance(value, str) and re.fullmatch(r"v1:[0-9a-f]{64}", value) is not None
