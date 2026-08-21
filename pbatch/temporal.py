"""Temporal validity & evidence decay (AADM-G §8).

四种时间（事件/处理/业务有效/入库）+ 证据时效：容易变化的信息置信度
随时间衰减（static 不衰减 / slow_changing 半衰期 7 天 / volatile 半衰期
6 小时）。世界状态、规则、接口文档与外部服务状态都不能被认为永久有效。

Usage:
    pi-batch temporal decay --validity volatile --confidence 0.9 --age-hours 12
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
import sys
import time
from typing import Optional

FRESHNESS_CLASSES = ("static", "slow_changing", "volatile")
# 半衰期（秒）：volatile 6h / slow_changing 7d / static 不衰减。
HALF_LIVES = {"volatile": 6 * 3600, "slow_changing": 7 * 86400,
              "static": None}
TEMPORAL_FIELDS = ("event_time", "processing_time", "valid_from", "valid_until",
                   "transaction_time", "version")


def _parse_time(value, name: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; naive times are rejected."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    else:
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return result.astimezone(timezone.utc)


def temporal_record(*, event_time=None, processing_time=None,
                    valid_from=None, valid_until=None, transaction_time=None,
                    version=None) -> dict:
    """Build the four-time evidence envelope without inventing timestamps.

    Event time describes when a fact happened, processing time when the
    system observed it, valid time bounds when it applies, and transaction
    time when it was persisted.  Missing values remain ``None`` and therefore
    cannot silently be treated as current truth.
    """
    values = {"event_time": event_time, "processing_time": processing_time,
              "valid_from": valid_from, "valid_until": valid_until,
              "transaction_time": transaction_time, "version": version}
    record = {}
    for name in TEMPORAL_FIELDS:
        if name == "version":
            if version is not None and (isinstance(version, bool)
                                        or not isinstance(version, int)
                                        or version < 0):
                raise ValueError("version must be a non-negative integer")
            record[name] = version
        else:
            parsed = _parse_time(values[name], name)
            record[name] = parsed.isoformat().replace("+00:00", "Z") \
                if parsed else None
    start = _parse_time(record["valid_from"], "valid_from")
    end = _parse_time(record["valid_until"], "valid_until")
    if start and end and end < start:
        raise ValueError("valid_until must not precede valid_from")
    return record


def is_valid_at(record: dict, at=None) -> bool:
    """Return whether an evidence record is valid at an instant."""
    if not isinstance(record, dict):
        raise ValueError("record must be a mapping")
    instant = _parse_time(at or datetime.now(timezone.utc), "at")
    start = _parse_time(record.get("valid_from"), "valid_from")
    end = _parse_time(record.get("valid_until"), "valid_until")
    return (not start or instant >= start) and (not end or instant <= end)


def age_seconds(record: dict, now=None) -> float:
    """Age evidence from event time, then processing/transaction time."""
    if not isinstance(record, dict):
        raise ValueError("record must be a mapping")
    current = _parse_time(now or datetime.now(timezone.utc), "now")
    observed = next((_parse_time(record.get(name), name)
                     for name in ("event_time", "processing_time",
                                  "transaction_time")
                     if record.get(name)), None)
    if observed is None:
        raise ValueError("record has no event, processing, or transaction time")
    return max(0.0, (current - observed).total_seconds())


def effective_confidence(confidence: float, freshness_class: str,
                         record: dict, now=None) -> float:
    """Apply evidence decay using the record's temporal metadata."""
    return decayed_confidence(confidence, freshness_class,
                              age_seconds(record, now))


def decayed_confidence(confidence: float, freshness_class: str,
                       age_seconds: float) -> float:
    """指数衰减：C(t) = C0 × 0.5^(t / half_life)。"""
    if confidence < 0 or confidence > 1:
        raise ValueError("confidence must be in [0, 1]")
    if freshness_class not in FRESHNESS_CLASSES:
        raise ValueError(f"freshness_class must be one of {FRESHNESS_CLASSES}")
    half_life = HALF_LIVES[freshness_class]
    if half_life is None or age_seconds <= 0:
        return round(confidence, 3)
    decayed = confidence * (0.5 ** (age_seconds / half_life))
    return round(max(0.0, decayed), 3)


def is_fresh(confidence: float, threshold: float = 0.5) -> bool:
    """衰减后置信度仍高于阈值的证据视为新鲜。"""
    return confidence >= threshold


def temporal_main(argv: Optional[list] = None) -> int:
    """`pi-batch temporal decay --validity X --confidence N --age-hours H`。"""
    parser = argparse.ArgumentParser(
        prog="pi-batch.py temporal",
        description="Temporal validity & evidence decay (AADM-G §8)")
    parser.add_argument("--validity", default="volatile",
                        choices=FRESHNESS_CLASSES)
    parser.add_argument("--confidence", type=float, default=0.9)
    parser.add_argument("--age-hours", type=float, default=1.0)
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args(argv)
    decayed = decayed_confidence(args.confidence, args.validity,
                                 args.age_hours * 3600)
    print(f"decayed: {args.confidence} → {decayed} "
          f"({args.validity}, {args.age_hours:.1f}h 后)")
    print(f"fresh: {is_fresh(decayed, args.threshold)} "
          f"(threshold {args.threshold})")
    raise SystemExit(0 if is_fresh(decayed, args.threshold) else 1)
