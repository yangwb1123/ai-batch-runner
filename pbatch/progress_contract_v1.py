"""Authoritative, pure stdlib validation for the closed progress.v1 envelope.

No execution, authorization, ledger discovery, clock reads, or I/O belongs here.
See docs/PROGRESS_V1_CONTRACT.md for the mapping and numeric wire contracts.
"""

from datetime import datetime, timedelta, timezone
import math
import re

SCHEMA_VERSION = 1
SCHEMA = f"progress.v{SCHEMA_VERSION}"
IDENTITY_FIELDS = ("tenant_id", "workspace_id", "task_id", "run_id")
USAGE_FIELDS = ("input_tokens", "output_tokens", "cost_amount", "cost_currency")
RECORD_FIELDS = IDENTITY_FIELDS + (
    "sequence", "observed_at", "state", "completed_units", "total_units",
    "percent", "usage", "eta_seconds", "stale",
)
ENVELOPE_FIELDS = (
    "schema", "generated_at", "stale_after_seconds", "records", "truncated",
)
STATES = frozenset({
    "queued", "running", "blocked", "succeeded", "failed", "cancelled", "unknown",
})
REQUIRED_FIELD_MAP_KEYS = IDENTITY_FIELDS + ("sequence", "observed_at")
FIELD_MAP_KEYS = frozenset(REQUIRED_FIELD_MAP_KEYS + (
    "state", "completed_units", "total_units",
) + tuple("usage." + key for key in USAGE_FIELDS))
STATE_MAP_KEYS = STATES
MAX_SAFE_INTEGER = 9007199254740991
MAX_COST_AMOUNT = 1_000_000_000_000
MAX_ROWS = 1000
MAX_RECORDS = 200
MAX_ROW_BYTES = 16 * 1024
MAX_ROW_DEPTH = 4
MAX_CONTAINER_ENTRIES = 64
MAX_STRING_BYTES = 4096
MAX_IDENTITY_BYTES = 128
MAX_STATE_ALIASES = 64
MAX_STATE_ALIAS_BYTES = 128
MAX_STALE_SECONDS = 86400
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.([0-9]{1,6}))?(?:Z|\+00:00)"
)
_CURRENCY = re.compile(r"[A-Z]{3}")


class ProgressContractError(ValueError):
    """Sanitized contract failure: messages never include source data or paths."""


def _string(value, maximum, *, nonempty=False):
    if type(value) is not str or len(value) > maximum:
        raise ProgressContractError("invalid string type or size")
    try:
        length = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        raise ProgressContractError("string is not valid UTF-8") from None
    if length > maximum or (nonempty and not value.strip()):
        raise ProgressContractError("invalid string size or empty evidence")
    return value


def _integer(value, *, minimum=0, maximum=MAX_SAFE_INTEGER, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProgressContractError("integer is missing, invalid, or out of bounds")
    return value


def _exact_fields(value, fields):
    if type(value) is not dict or len(value) != len(fields):
        raise ProgressContractError("contract object has missing or unknown fields")
    if any(type(key) is not str for key in value) or set(value) != set(fields):
        raise ProgressContractError("contract object has missing or unknown fields")


def _format_utc(value):
    return value.isoformat(timespec="microseconds")[:-6] + "Z"


def _utc_fact(value, *, allow_datetime=False, canonical=False):
    if type(value) is str:
        _string(value, 32, nonempty=True)
        match = _TIMESTAMP.fullmatch(value)
        if match is None:
            raise ProgressContractError("timestamp must be an explicit UTC fact")
        # Python 3.9/3.10 only parse three or six fractional digits; pad, never round.
        fraction = (match.group(1) or "").ljust(6, "0")
        normalized = value[:19] + "." + fraction + "+00:00"
        try:
            result = datetime.fromisoformat(normalized)
        except ValueError:
            raise ProgressContractError("timestamp is not a valid UTC instant") from None
    elif allow_datetime and type(value) is datetime:
        if type(value.tzinfo) is not timezone or value.utcoffset() != timedelta(0):
            raise ProgressContractError("datetime must have an explicit UTC timezone")
        result = value
    else:
        raise ProgressContractError("timestamp must be an explicit UTC fact")
    if canonical and value != _format_utc(result):
        raise ProgressContractError("timestamp is not canonically serialized")
    return result


def _percent_for_counts(completed, total):
    _integer(completed, nullable=True)
    _integer(total, nullable=True)
    if completed is not None and total is not None and completed > total:
        raise ProgressContractError("completed units exceed the reported total")
    if completed is None or total is None or total == 0:
        return None
    hundredths, remainder = divmod(completed * 10000, total)
    return (hundredths + int(2 * remainder >= total)) / 100


def _cost_amount(value):
    if value is None:
        return None
    if type(value) not in (int, float):
        raise ProgressContractError("cost amount must be a bounded nonnegative number")
    if not 0 <= value <= MAX_COST_AMOUNT or not math.isfinite(value):
        raise ProgressContractError("cost amount must be a bounded nonnegative number")
    # Normalize number representations (including signed zero) for stable dedupe.
    return float(value) + 0.0


def _validate_usage(value):
    _exact_fields(value, USAGE_FIELDS)
    for key in ("input_tokens", "output_tokens"):
        _integer(value[key], nullable=True)
    _cost_amount(value["cost_amount"])
    currency = value["cost_currency"]
    if currency is not None:
        if type(currency) is not str or not _CURRENCY.fullmatch(currency):
            raise ProgressContractError("currency must be null or three uppercase letters")


def _validate_percent(value, expected):
    if expected is None:
        if value is not None:
            raise ProgressContractError("percent must be null without positive known totals")
    elif type(value) not in (int, float) or value != expected:
        raise ProgressContractError("percent does not match the reported counters")


def _identity(record):
    return tuple(record[key] for key in IDENTITY_FIELDS)


def _record_order(record):
    instant = _utc_fact(record["observed_at"], canonical=True)
    seconds = (instant.toordinal() * 86400 + instant.hour * 3600
               + instant.minute * 60 + instant.second)
    return (-(seconds * 1_000_000 + instant.microsecond), _identity(record))


def _validate_record(value, now, stale_after_seconds):
    _exact_fields(value, RECORD_FIELDS)
    for key in IDENTITY_FIELDS:
        _string(value[key], MAX_IDENTITY_BYTES, nonempty=True)
    _integer(value["sequence"])
    observed = _utc_fact(value["observed_at"], canonical=True)
    if observed > now:
        raise ProgressContractError("observation cannot be in the future")
    if type(value["state"]) is not str or value["state"] not in STATES:
        raise ProgressContractError("record state is not supported")
    expected = _percent_for_counts(value["completed_units"], value["total_units"])
    _validate_percent(value["percent"], expected)
    _validate_usage(value["usage"])
    if value["eta_seconds"] is not None:
        raise ProgressContractError("ETA must remain null in progress.v1")
    expected_stale = now - observed >= timedelta(seconds=stale_after_seconds)
    if type(value["stale"]) is not bool or value["stale"] != expected_stale:
        raise ProgressContractError("stale flag does not match the observation age")


def validate_progress(payload):
    """Return None for a valid exact envelope; otherwise raise ProgressContractError.

    Validates independent wire data, including derived values, unique identities,
    and ordering. Truncation provenance requires the original rows and limit.
    Only plain JSON dict/list/scalar values are accepted; inputs are never changed.
    """
    _exact_fields(payload, ENVELOPE_FIELDS)
    if type(payload["schema"]) is not str or payload["schema"] != SCHEMA:
        raise ProgressContractError("unsupported progress schema")
    now = _utc_fact(payload["generated_at"], canonical=True)
    stale_after = _integer(payload["stale_after_seconds"], minimum=1,
                           maximum=MAX_STALE_SECONDS)
    records = payload["records"]
    if type(records) is not list or len(records) > MAX_RECORDS:
        raise ProgressContractError("records must be a bounded JSON array")
    if type(payload["truncated"]) is not bool:
        raise ProgressContractError("truncated must be a boolean")
    if not records and payload["truncated"]:
        raise ProgressContractError("an empty projection cannot be truncated")
    seen, previous = set(), None
    for record in records:
        _validate_record(record, now, stale_after)
        identity, order = _identity(record), _record_order(record)
        if identity in seen:
            raise ProgressContractError("records contain duplicate identities")
        if previous is not None and order < previous:
            raise ProgressContractError("records are not in canonical order")
        seen.add(identity)
        previous = order
