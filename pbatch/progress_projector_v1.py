"""Bounded, deterministic projection of explicitly mapped cumulative snapshots."""

import json
import math
from datetime import timedelta

from . import progress_contract_v1 as contract


def _scalar_size(value):
    kind = type(value)
    if kind is str:
        contract._string(value, contract.MAX_STRING_BYTES)
    elif kind is int:
        if not -contract.MAX_SAFE_INTEGER <= value <= contract.MAX_SAFE_INTEGER:
            raise contract.ProgressContractError("row contains an unsafe JSON integer")
    elif kind is float:
        if not math.isfinite(value):
            raise contract.ProgressContractError("row contains a nonfinite number")
    elif value is not None and kind is not bool:
        raise contract.ProgressContractError("row contains unsupported JSON evidence")
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def _container_size(value, depth, budget):
    if depth > contract.MAX_ROW_DEPTH or len(value) > contract.MAX_CONTAINER_ENTRIES:
        raise contract.ProgressContractError("row exceeds container depth or width bounds")
    size = 2 + max(0, len(value) - 1)
    if size > budget:
        raise contract.ProgressContractError("row exceeds canonical JSON byte budget")
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise contract.ProgressContractError("row object keys must be strings")
            size += _json_size(key, depth + 1, budget - size) + 1
            size += _json_size(item, depth + 1, budget - size)
    else:
        for item in value:
            size += _json_size(item, depth + 1, budget - size)
    return size


def _json_size(value, depth, budget):
    if budget < 0:
        raise contract.ProgressContractError("row exceeds canonical JSON byte budget")
    if type(value) in (dict, list):
        size = _container_size(value, depth, budget)
    else:
        size = _scalar_size(value)
    if size > budget:
        raise contract.ProgressContractError("row exceeds canonical JSON byte budget")
    return size


def _bound_rows(rows):
    if type(rows) not in (list, tuple) or len(rows) > contract.MAX_ROWS:
        raise contract.ProgressContractError("rows must be a list or tuple of at most 1000")
    for row in rows:
        if type(row) is not dict:
            raise contract.ProgressContractError("each row must be a plain JSON object")
        _json_size(row, 1, contract.MAX_ROW_BYTES)


def _field_mapping(value):
    if type(value) is not dict or len(value) > len(contract.FIELD_MAP_KEYS):
        raise contract.ProgressContractError("field_map must be a bounded object")
    if any(type(key) is not str for key in value):
        raise contract.ProgressContractError("field_map keys must be canonical targets")
    if (not set(value) <= contract.FIELD_MAP_KEYS
            or not set(contract.REQUIRED_FIELD_MAP_KEYS) <= set(value)):
        raise contract.ProgressContractError("field_map has unknown or missing targets")
    result = {}
    for target, path in value.items():
        if type(path) is str:
            path = (path,)
        if type(path) not in (list, tuple) or not 1 <= len(path) <= contract.MAX_ROW_DEPTH:
            raise contract.ProgressContractError("field_map paths must have 1..4 string keys")
        result[target] = tuple(contract._string(key, contract.MAX_STRING_BYTES,
                                                nonempty=True) for key in path)
    return result


def _state_mapping(value):
    if type(value) is not dict or len(value) > len(contract.STATE_MAP_KEYS):
        raise contract.ProgressContractError("state_map must be a bounded object")
    if any(type(key) is not str for key in value) or not set(value) <= contract.STATE_MAP_KEYS:
        raise contract.ProgressContractError("state_map keys must be canonical states")
    count = 0
    for aliases in value.values():
        if type(aliases) not in (list, tuple):
            raise contract.ProgressContractError("state aliases must be lists or tuples")
        count += len(aliases)
        if count > contract.MAX_STATE_ALIASES:
            raise contract.ProgressContractError("state_map exceeds total alias budget")
    result = {}
    for state, aliases in value.items():
        for alias in aliases:
            contract._string(alias, contract.MAX_STATE_ALIAS_BYTES, nonempty=True)
            if alias in result:
                raise contract.ProgressContractError("state aliases must be unique")
            result[alias] = state
    return result


def _lookup(row, path):
    current = row
    for key in path:
        if current is None:
            return None
        if type(current) is not dict:
            raise contract.ProgressContractError("mapped path crosses non-object evidence")
        if key not in current:
            return None
        current = current[key]
    return current


def _mapped_state(value, aliases):
    if value is None:
        return "unknown"
    contract._string(value, contract.MAX_STATE_ALIAS_BYTES, nonempty=True)
    if value not in aliases:
        raise contract.ProgressContractError("non-null state evidence is not mapped")
    return aliases[value]


def _project_record(row, fields, aliases, now, stale_after):
    values = {target: _lookup(row, path) for target, path in fields.items()}
    observed = contract._utc_fact(values["observed_at"])
    completed, total = values.get("completed_units"), values.get("total_units")
    record = {key: values[key] for key in contract.IDENTITY_FIELDS}
    record.update({
        "sequence": values["sequence"], "observed_at": contract._format_utc(observed),
        "state": _mapped_state(values.get("state"), aliases),
        "completed_units": completed, "total_units": total,
        "percent": contract._percent_for_counts(completed, total),
        "usage": {key: values.get("usage." + key) for key in contract.USAGE_FIELDS},
        "eta_seconds": None,
        "stale": now - observed >= timedelta(seconds=stale_after),
    })
    record["usage"]["cost_amount"] = contract._cost_amount(record["usage"]["cost_amount"])
    contract._validate_record(record, now, stale_after)
    return record


def _latest_records(rows, fields, aliases, now, stale_after):
    seen, latest = {}, {}
    for row in rows:
        record = _project_record(row, fields, aliases, now, stale_after)
        identity = contract._identity(record)
        key = (identity, record["sequence"])
        canonical = json.dumps(record, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"), allow_nan=False)
        if key in seen and seen[key] != canonical:
            raise contract.ProgressContractError("conflicting snapshots at one identity and sequence")
        seen[key] = canonical
        if identity not in latest or record["sequence"] > latest[identity]["sequence"]:
            latest[identity] = record
    return sorted(latest.values(), key=contract._record_order)


def project_progress(rows, *, now, field_map, state_map, limit=100,
                     stale_after_seconds=300):
    """Project validated bounded rows into an exact progress.v1 JSON envelope.

    now is an injected UTC datetime or UTC ISO timestamp, never an implicit clock.
    field_map maps canonical scalar targets to literal source keys or key paths;
    state_map maps canonical states to bounded lists/tuples of source aliases.
    Missing optional evidence is null (state: unknown); required evidence fails.
    Every row is validated before limiting. All usage is cumulative, never summed.
    Failures raise sanitized ProgressContractError, without output or side effects.
    """
    limit = contract._integer(limit, minimum=1, maximum=contract.MAX_RECORDS)
    stale_after = contract._integer(stale_after_seconds, minimum=1,
                                   maximum=contract.MAX_STALE_SECONDS)
    instant = contract._utc_fact(now, allow_datetime=True)
    fields, aliases = _field_mapping(field_map), _state_mapping(state_map)
    _bound_rows(rows)
    retained = _latest_records(rows, fields, aliases, instant, stale_after)
    payload = {
        "schema": contract.SCHEMA, "generated_at": contract._format_utc(instant),
        "stale_after_seconds": stale_after, "records": retained[:limit],
        "truncated": len(retained) > limit,
    }
    contract.validate_progress(payload)
    return payload
