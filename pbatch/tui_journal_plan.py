"""Bounded journal projection helpers for PlanSnapshot evidence."""

from __future__ import annotations

from typing import Any, Callable


_PLAN_PROJECTION_ITEMS = 64
_PLAN_PROJECTION_VALUES = 32
_PLAN_PROJECTION_BYTES = 512
_PLAN_PROJECTION_BUDGET = 16


def preserve_actor_lineage(original: dict, safe: dict) -> None:
    """Keep actor envelope and payload identities comparable after redaction."""
    source = original.get("payload")
    target = safe.get("payload")
    if not isinstance(source, dict) or not isinstance(target, dict):
        return
    copied = dict(target)
    for name in ("actor_id", "parent_actor_id"):
        value = source.get(name)
        if isinstance(value, str) and len(value.encode("utf-8")) <= 256:
            copied[name] = value
    safe["payload"] = copied


def plan_projection_payload(payload: dict) -> dict:
    """Retain explicit plan evidence when a journal payload is compacted."""
    if not isinstance(payload, dict):
        return {}
    result = {}
    fields = (
        "item_id", "plan_id", "plan_digest", "plan_identity", "goal", "read_scope",
        "write_scope", "commands", "network_intent", "external_tools",
        "validators", "gates", "budget", "assumptions", "unknowns",
        "stop_policy", "source", "version", "boundaries_truncated")
    for name in fields:
        if name in payload:
            result[name] = _plan_value(payload[name])
    values = payload.get("items", [])
    if isinstance(values, list):
        result["items"] = [_plan_item(item) for item in
                            values[:_PLAN_PROJECTION_ITEMS]]
        if len(values) > _PLAN_PROJECTION_ITEMS:
            result["truncated"] = True
    if _boundaries_truncated(payload):
        result["boundaries_truncated"] = True
    if payload.get("truncated") is True:
        result["truncated"] = True
    return result


def attach_plan_projection_identities(
        original: dict, safe: dict, digest: Callable[[object], str]) -> None:
    """Add stable pseudonyms without persisting raw plan or step identities."""
    if original.get("kind") != "plan.updated":
        return
    source = original.get("payload", {})
    target = safe.get("payload", {})
    if not isinstance(source, dict) or not isinstance(target, dict):
        return
    target = dict(target)
    identity = source.get("item_id") or source.get("plan_id")
    if identity:
        target["plan_identity"] = digest(identity)
    source_items = source.get("items", [])
    target_items = target.get("items", [])
    if isinstance(source_items, list) and isinstance(target_items, list):
        target["items"] = _with_item_identities(
            source_items, target_items, digest)
    safe["payload"] = target


def _with_item_identities(source: list, target: list,
                          digest: Callable[[object], str]) -> list:
    copied = []
    for original, persisted in zip(source[:_PLAN_PROJECTION_ITEMS], target):
        if not isinstance(original, dict):
            copied.append(persisted)
            continue
        item = dict(persisted) if isinstance(persisted, dict) else {}
        identity = original.get("id")
        if identity:
            item["step_identity"] = digest(identity)
        actor = original.get("actor_id")
        if actor:
            item["actor_identity"] = digest(actor)
        copied.append(item)
    return copied


def _plan_item(value: object) -> object:
    if not isinstance(value, dict):
        return _plan_value(value)
    names = (
        "status", "state", "completed", "text", "title", "name", "id",
        "step_id", "stepId", "step_identity", "actor", "actor_id",
        "actorId", "actor_identity", "dependencies", "depends_on",
        "dependsOn")
    return {name: _plan_value(value[name]) for name in names
            if name in value}


def _plan_value(value: object) -> object:
    if isinstance(value, str):
        return _bounded_text(value, _PLAN_PROJECTION_BYTES)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {_bounded_text(str(key), 128): _plan_value(item)
                for key, item in tuple(value.items())[:_PLAN_PROJECTION_BUDGET]}
    if isinstance(value, (list, tuple)):
        return [_plan_value(item) for item in
                value[:_PLAN_PROJECTION_VALUES]]
    return _bounded_text(str(value), _PLAN_PROJECTION_BYTES)


def _boundaries_truncated(payload: dict) -> bool:
    scalar = ("goal", "stop_policy", "source", "version")
    if any(_text_long(payload.get(name)) for name in scalar):
        return True
    fields = ("read_scope", "write_scope", "commands", "network_intent",
              "external_tools", "validators", "gates", "assumptions",
              "unknowns")
    return any(_values_long(payload.get(name)) for name in fields)


def _text_long(value: object) -> bool:
    return isinstance(value, str) and len(value.encode("utf-8")) > _PLAN_PROJECTION_BYTES


def _values_long(value: object) -> bool:
    if not isinstance(value, (list, tuple)):
        return False
    return len(value) > _PLAN_PROJECTION_VALUES or any(
        _text_long(item) for item in value[:_PLAN_PROJECTION_VALUES])


def _bounded_text(value: object, maximum: int) -> str:
    encoded = str(value or "").encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return encoded.decode("utf-8", "replace")
    return encoded[-maximum:].decode("utf-8", "ignore")
