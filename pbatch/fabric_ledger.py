"""Device-scoped, idempotent resource ledger for the execution fabric.

The normal agent metering ledger is session-scoped.  A device Runner needs a
separate fact source keyed by ``device_id`` and execution attempt so a retry
or duplicate evidence submission cannot charge the same work twice.  This
module deliberately records observed usage only; admission budgets remain a
separate policy decision in the control plane.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional


LEDGER_SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 128 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
RESOURCE_FIELDS = (
    "elapsed_seconds", "cpu_seconds", "input_bytes", "output_bytes",
    "network_bytes", "estimated_cost",
)


class UsageLedgerError(ValueError):
    """Raised when durable usage facts cannot be trusted or appended."""


def _number(value, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise UsageLedgerError(f"usage {field} must be numeric") from exc
    if not math.isfinite(number) or number < 0:
        raise UsageLedgerError(f"usage {field} must be finite and non-negative")
    return round(number, 6)


def _normalize_usage(usage: dict) -> dict:
    if not isinstance(usage, dict):
        raise UsageLedgerError("usage must be an object")
    unknown = sorted(set(usage) - set(RESOURCE_FIELDS))
    if unknown:
        raise UsageLedgerError("unknown usage fields: " + ", ".join(unknown))
    return {field: _number(usage.get(field, 0), field)
            for field in RESOURCE_FIELDS}


class DeviceUsageLedger:
    """Append-only usage facts with persistence and idempotent keys."""

    def __init__(self, state_dir, filename: str = "usage.jsonl") -> None:
        self.path = Path(state_dir) / filename
        self._lock = threading.RLock()
        self._records: list[dict] = []
        self._by_key: dict[tuple[str, str], dict] = {}
        self._totals: dict[str, dict[str, float]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        if self.path.is_symlink() or self.path.stat().st_size > MAX_FILE_BYTES:
            raise UsageLedgerError("usage ledger path is unsafe or too large")
        with self.path.open("rb") as handle:
            while True:
                raw = handle.readline(MAX_RECORD_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_RECORD_BYTES or not raw.endswith(b"\n"):
                    raise UsageLedgerError("usage ledger contains oversized record")
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise UsageLedgerError("usage ledger contains invalid JSON") from exc
                self._accept_loaded(record)

    def _accept_loaded(self, record: dict) -> None:
        normalized = self._normalize_record(record)
        key = (normalized["device_id"], normalized["idempotency_key"])
        if key in self._by_key:
            if self._by_key[key]["usage"] != normalized["usage"]:
                raise UsageLedgerError("usage ledger idempotency conflict")
            return
        self._by_key[key] = normalized
        self._records.append(normalized)
        self._add_total(normalized["device_id"], normalized["usage"])

    def _normalize_record(self, record: dict) -> dict:
        if not isinstance(record, dict):
            raise UsageLedgerError("usage ledger record must be an object")
        if int(record.get("schema_version", 0)) != LEDGER_SCHEMA_VERSION:
            raise UsageLedgerError("unsupported usage ledger schema")
        device_id = str(record.get("device_id", "")).strip()
        key = str(record.get("idempotency_key", "")).strip()
        if not device_id or not key:
            raise UsageLedgerError("usage ledger record needs device and key")
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "ledger_id": str(record.get("ledger_id", "")),
            "recorded_at": str(record.get("recorded_at", "")),
            "device_id": device_id,
            "task_id": str(record.get("task_id", "")),
            "attempt_id": str(record.get("attempt_id", "")),
            "idempotency_key": key,
            "usage": _normalize_usage(record.get("usage", {})),
        }

    def _add_total(self, device_id: str, usage: dict) -> None:
        total = self._totals.setdefault(
            device_id, {field: 0.0 for field in RESOURCE_FIELDS})
        for field in RESOURCE_FIELDS:
            total[field] = round(total[field] + usage[field], 6)

    def record(self, device_id: str, usage: dict, *, task_id: str = "",
               attempt_id: str = "", idempotency_key: str = "") -> dict:
        """Durably record one observation; duplicate keys return the old fact."""
        device_id = str(device_id or "").strip()
        key = str(idempotency_key or "").strip()
        if not device_id or not key:
            raise UsageLedgerError("device_id and idempotency_key are required")
        with self._lock:
            record_key = (device_id, key)
            existing = self._by_key.get(record_key)
            if existing is not None:
                if existing["usage"] != _normalize_usage(usage):
                    raise UsageLedgerError("usage idempotency key reused with different usage")
                return {**existing, "duplicate": True}
            record = {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "ledger_id": "usage-" + uuid.uuid4().hex[:16],
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "device_id": device_id,
                "task_id": str(task_id or ""),
                "attempt_id": str(attempt_id or ""),
                "idempotency_key": key,
                "usage": _normalize_usage(usage),
            }
            line = json.dumps(record, ensure_ascii=False,
                              separators=(",", ":")) + "\n"
            encoded = line.encode("utf-8")
            if len(encoded) > MAX_RECORD_BYTES:
                raise UsageLedgerError("usage ledger record too large")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                current = self.path.stat().st_size if self.path.exists() else 0
                if current + len(encoded) > MAX_FILE_BYTES:
                    raise UsageLedgerError(
                        "usage ledger file capacity exhausted")
                with self.path.open("ab") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(self.path, 0o600)
            except OSError as exc:
                raise UsageLedgerError(f"cannot append usage ledger: {exc}") from exc
            self._by_key[record_key] = record
            self._records.append(record)
            self._add_total(device_id, record["usage"])
            return dict(record)

    def totals(self, device_id: str = "", task_id: str = "") -> dict:
        """Return bounded numeric totals, optionally scoped to device/task."""
        with self._lock:
            if not task_id and device_id:
                return dict(self._totals.get(device_id, {
                    field: 0.0 for field in RESOURCE_FIELDS}))
            total = {field: 0.0 for field in RESOURCE_FIELDS}
            for record in self._records:
                if device_id and record["device_id"] != device_id:
                    continue
                if task_id and record["task_id"] != task_id:
                    continue
                for field in RESOURCE_FIELDS:
                    total[field] += record["usage"][field]
            return {field: round(value, 6) for field, value in total.items()}

    def recent(self, limit: int = 20, device_id: str = "") -> list[dict]:
        """Return newest facts without exposing an unbounded ledger."""
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 20
        records = [record for record in self._records
                   if not device_id or record["device_id"] == device_id]
        return [dict(record) for record in records[-limit:]]

    def summary(self, device_id: str = "", task_id: str = "") -> dict:
        return {"records": sum(1 for record in self._records
                                if (not device_id or record["device_id"] == device_id)
                                and (not task_id or record["task_id"] == task_id)),
                "device_id": device_id,
                "task_id": task_id,
                "totals": self.totals(device_id=device_id, task_id=task_id)}
