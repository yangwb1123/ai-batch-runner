"""Strict, provider-neutral normalization for Pi RPC control responses."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .tui_runtime import (
    RuntimeCompactionResult,
    RuntimeContextUsage,
    RuntimeModelCatalog,
    RuntimeModelRef,
    RuntimeSessionState,
    RuntimeSessionStats,
    RuntimeThinkingLevels,
    RuntimeTokenTotals,
)


MAX_REQUEST_TEXT_BYTES = 512 * 1024
MAX_SESSION_ID_BYTES = 16 * 1024
MAX_MODEL_FIELD_BYTES = 16 * 1024
MAX_SUMMARY_BYTES = 64 * 1024
MAX_AVAILABLE_MODELS = 4096
MAX_THINKING_LEVELS = 32
_TRUNCATION_MARKER = "\n...[truncated]"
_MISSING = object()


def request_text(
        request: Any, name: str, *, maximum: int = MAX_REQUEST_TEXT_BYTES,
        required: bool = False) -> str:
    """Read one bounded UTF-8 request field without coercing arbitrary values."""
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum <= 0:
        raise ValueError("maximum must be a positive integer")
    value = request.get(name, _MISSING) if isinstance(request, Mapping) else getattr(
        request, name, _MISSING)
    if value is _MISSING:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    encoded = _utf8(value, name)
    if "\x00" in value or len(encoded) > maximum:
        raise ValueError(f"{name} exceeds its safe wire boundary")
    if required and not value:
        raise ValueError(f"{name} is required")
    return value


def request_session_id(request: Any, active_session_id: str) -> str:
    """Resolve an optional local target and reject stale-session controls."""
    active = request_text(
        {"session_id": active_session_id}, "session_id",
        maximum=MAX_SESSION_ID_BYTES, required=True)
    requested = request_text(
        request, "session_id", maximum=MAX_SESSION_ID_BYTES)
    if requested and requested != active:
        raise ValueError("request does not target the active session")
    return active


def normalize_session_state(
        raw: Any, local_session_id: str,
        expected_provider_id: str) -> RuntimeSessionState:
    """Normalize ``get_state`` while exposing only the model's public identity."""
    value = _mapping(raw, "Pi get_state data")
    local_id = request_text(
        {"session_id": local_session_id}, "session_id",
        maximum=MAX_SESSION_ID_BYTES, required=True)
    provider_id = _matched_provider_id(
        value, expected_provider_id, allow_missing=False)
    return RuntimeSessionState(
        session_id=local_id,
        provider_session_id=provider_id,
        name=_optional_text(value, "sessionName", MAX_SESSION_ID_BYTES),
        model=_model_ref(value),
        thinking_level=_optional_text(value, "thinkingLevel", 256),
        is_streaming=_optional_bool(value, "isStreaming"),
        is_compacting=_optional_bool(value, "isCompacting"),
        auto_compaction_enabled=_optional_bool(
            value, "autoCompactionEnabled"),
        message_count=_optional_int(value, "messageCount"),
        pending_message_count=_optional_int(value, "pendingMessageCount"),
    )


def normalize_session_stats(
        raw: Any, local_session_id: str,
        expected_provider_id: str) -> RuntimeSessionStats:
    """Normalize ``get_session_stats`` and reconcile its optional session path."""
    value = _mapping(raw, "Pi get_session_stats data")
    local_id = request_text(
        {"session_id": local_session_id}, "session_id",
        maximum=MAX_SESSION_ID_BYTES, required=True)
    provider_id = _matched_provider_id(
        value, expected_provider_id, allow_missing=True)
    return RuntimeSessionStats(
        session_id=local_id,
        provider_session_id=provider_id,
        user_messages=_optional_int(value, "userMessages"),
        assistant_messages=_optional_int(value, "assistantMessages"),
        tool_calls=_optional_int(value, "toolCalls"),
        tool_results=_optional_int(value, "toolResults"),
        total_messages=_optional_int(value, "totalMessages"),
        tokens=_token_totals(value.get("tokens", _MISSING), "total"),
        cost=_optional_number(value, "cost"),
        context_usage=_context_usage(value.get("contextUsage", _MISSING)),
    )


def normalize_model_catalog(raw: Any, local_session_id: str) -> RuntimeModelCatalog:
    """Redact Pi's full model definitions to bounded public identities."""
    value = _mapping(raw, "Pi get_available_models data")
    models = value.get("models", _MISSING)
    if not isinstance(models, list):
        raise ValueError("models must be an array")
    if len(models) > MAX_AVAILABLE_MODELS:
        raise ValueError("models exceeds the safe catalog boundary")
    normalized = tuple(_catalog_model(item) for item in models)
    identities = {(item.provider, item.model_id) for item in normalized}
    if len(identities) != len(normalized):
        raise ValueError("models contains duplicate provider/model identities")
    return RuntimeModelCatalog(_local_session_id(local_session_id), normalized)


def normalize_thinking_levels(
        raw: Any, local_session_id: str) -> RuntimeThinkingLevels:
    """Normalize the provider's dynamic thinking-level choices."""
    value = _mapping(raw, "Pi get_available_thinking_levels data")
    levels = value.get("levels", _MISSING)
    if not isinstance(levels, list):
        raise ValueError("levels must be an array")
    if len(levels) > MAX_THINKING_LEVELS:
        raise ValueError("levels exceeds the safe choice boundary")
    normalized = tuple(_thinking_level(item) for item in levels)
    if len(set(normalized)) != len(normalized):
        raise ValueError("levels contains duplicate choices")
    return RuntimeThinkingLevels(_local_session_id(local_session_id), normalized)


def normalize_compaction_result(
        raw: Any, local_session_id: str) -> RuntimeCompactionResult:
    """Normalize a completed compaction and discard provider-only details."""
    value = _mapping(raw, "Pi compact data")
    local_id = request_text(
        {"session_id": local_session_id}, "session_id",
        maximum=MAX_SESSION_ID_BYTES, required=True)
    summary, truncated = _bounded_summary(value.get("summary", _MISSING))
    usage = value.get("usage", _MISSING)
    if usage is not _MISSING:
        usage = _mapping(usage, "usage")
    return RuntimeCompactionResult(
        session_id=local_id,
        summary=summary,
        summary_truncated=truncated,
        tokens_before=_optional_int(value, "tokensBefore"),
        estimated_tokens_after=_optional_int(value, "estimatedTokensAfter"),
        tokens=_token_totals(usage, "totalTokens"),
        cost=_usage_cost(usage),
    )


def _local_session_id(value: str) -> str:
    return request_text(
        {"session_id": value}, "session_id",
        maximum=MAX_SESSION_ID_BYTES, required=True)


def _catalog_model(value: Any) -> RuntimeModelRef:
    model = _mapping(value, "model")
    provider = request_text(
        {"provider": model.get("provider", "")}, "provider",
        maximum=MAX_MODEL_FIELD_BYTES, required=True)
    model_id = request_text(
        {"model_id": model.get("id", "")}, "model_id",
        maximum=MAX_MODEL_FIELD_BYTES, required=True)
    name = _optional_text(model, "name", MAX_MODEL_FIELD_BYTES)
    return RuntimeModelRef(provider, model_id, name)


def _thinking_level(value: Any) -> str:
    level = request_text(
        {"level": value}, "level", maximum=256, required=True)
    if level != level.strip():
        raise ValueError("thinking levels must not contain outer whitespace")
    return level


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return value


def _utf8(value: str, label: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 text") from exc


def _matched_provider_id(
        value: Mapping[str, Any], expected: str, *, allow_missing: bool) -> str:
    provider_id = request_text(
        {"provider_session_id": expected}, "provider_session_id",
        maximum=MAX_SESSION_ID_BYTES, required=True)
    observed = value.get("sessionFile", _MISSING)
    if observed is _MISSING and allow_missing:
        return provider_id
    if observed is _MISSING:
        raise ValueError("Pi response is missing sessionFile")
    observed_id = request_text(
        {"sessionFile": observed}, "sessionFile",
        maximum=MAX_SESSION_ID_BYTES, required=True)
    if observed_id != provider_id:
        raise ValueError("Pi response does not match the active provider session")
    return provider_id


def _optional_text(
        value: Mapping[str, Any], name: str, maximum: int) -> str:
    if name not in value:
        return ""
    if not isinstance(value[name], str):
        raise ValueError(f"{name} must be a string")
    return request_text(
        {name: value[name]}, name, maximum=maximum)


def _model_ref(value: Mapping[str, Any]) -> RuntimeModelRef | None:
    raw = value.get("model", _MISSING)
    if raw is _MISSING or raw is None:
        return None
    model = _mapping(raw, "model")
    provider = request_text(
        {"model.provider": model.get("provider", "")}, "model.provider",
        maximum=MAX_MODEL_FIELD_BYTES, required=True)
    model_id = request_text(
        {"model.id": model.get("id", "")}, "model.id",
        maximum=MAX_MODEL_FIELD_BYTES, required=True)
    name = _optional_text(model, "name", MAX_MODEL_FIELD_BYTES)
    return RuntimeModelRef(provider, model_id, name)


def _optional_bool(value: Mapping[str, Any], name: str) -> bool | None:
    if name not in value:
        return None
    item = value[name]
    if not isinstance(item, bool):
        raise ValueError(f"{name} must be a boolean")
    return item


def _optional_int(
        value: Mapping[str, Any], name: str, *, nullable: bool = False) -> int | None:
    if name not in value:
        return None
    item = value[name]
    if item is None and nullable:
        return None
    if isinstance(item, bool) or not isinstance(item, int) or item < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return item


def _optional_number(
        value: Mapping[str, Any], name: str,
        *, nullable: bool = False) -> float | None:
    if name not in value:
        return None
    item = value[name]
    if item is None and nullable:
        return None
    if (isinstance(item, bool) or not isinstance(item, (int, float))
            or not math.isfinite(item) or item < 0):
        raise ValueError(f"{name} must be a non-negative finite number")
    return float(item)


def _token_totals(value: Any, total_name: str) -> RuntimeTokenTotals:
    if value is _MISSING:
        return RuntimeTokenTotals()
    tokens = _mapping(value, "tokens")
    return RuntimeTokenTotals(
        input_tokens=_optional_int(tokens, "input"),
        output_tokens=_optional_int(tokens, "output"),
        cache_read_tokens=_optional_int(tokens, "cacheRead"),
        cache_write_tokens=_optional_int(tokens, "cacheWrite"),
        total_tokens=_optional_int(tokens, total_name),
    )


def _context_usage(value: Any) -> RuntimeContextUsage | None:
    if value is _MISSING:
        return None
    context = _mapping(value, "contextUsage")
    return RuntimeContextUsage(
        tokens=_optional_int(context, "tokens", nullable=True),
        context_window=_optional_int(context, "contextWindow"),
        percent=_optional_number(context, "percent", nullable=True),
    )


def _usage_cost(usage: Any) -> float | None:
    if usage is _MISSING:
        return None
    cost = usage.get("cost", _MISSING)
    if cost is _MISSING:
        return None
    cost_value = _mapping(cost, "usage.cost")
    return _optional_number(cost_value, "total")


def _bounded_summary(value: Any) -> tuple[str, bool]:
    if not isinstance(value, str):
        raise ValueError("summary must be a string")
    encoded = _utf8(value, "summary")
    if len(encoded) <= MAX_SUMMARY_BYTES:
        return value, False
    marker = _TRUNCATION_MARKER.encode("utf-8")
    prefix = encoded[:MAX_SUMMARY_BYTES - len(marker)].decode("utf-8", "ignore")
    return prefix + _TRUNCATION_MARKER, True
