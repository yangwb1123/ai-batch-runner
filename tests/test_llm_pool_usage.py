"""Authoritative three-window Go usage parsing; no credentials or real networking."""

from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from pbatch.llm_pool_usage import parse_usage
from pbatch.llm_pool_config import parse_config


NOW = 1_790_000_000.0


def document():
    reset = datetime.fromtimestamp(NOW + 3600, timezone.utc).isoformat()
    return {"usage": {name: {"status": "ok", "percent": 20, "resetsAt": reset}
                      for name in ("rolling", "weekly", "monthly")}}


def encode(value):
    return json.dumps(value).encode()


def test_authoritative_usage_percent_is_used_not_remaining():
    value = document()
    value["usage"]["rolling"].update(status="rate-limited", percent=100)
    value["usage"]["weekly"]["resetsAt"] = "2026-09-22T00:00:00.000Z"
    result = parse_usage(encode(value), now=NOW)
    assert set(result) == {"5h", "weekly", "monthly"}
    assert result["5h"] == {"status": "rate-limited", "percent": 100,
                            "resets_at": NOW + 3600}
    assert result["weekly"]["percent"] == 20
    assert result["weekly"]["resets_at"] > NOW


@pytest.mark.parametrize("field,value", [
    ("percent", True), ("percent", -1), ("percent", 101), ("percent", 1.5),
    ("percent", "50"), ("percent", 100), ("percent", float("nan")),
    ("percent", float("inf")), ("status", "rate-limited"), ("status", "unknown"),
    ("status", []), ("status", None), ("resetsAt", "2026-09-21T00:00:00"),
    ("resetsAt", "tomorrow"), ("resetsAt", []), ("resetsAt", NOW + 100),
    ("resetsAt", "2099-01-01T00:00:00Z"), ("resetsAt", "2000-01-01T00:00:00Z"),
])
def test_invalid_or_inconsistent_window_is_not_available(field, value):
    data = document()
    data["usage"]["rolling"][field] = value
    with pytest.raises(ValueError):
        parse_usage(encode(data), now=NOW)


@pytest.mark.parametrize("path", [(), ("usage",), ("usage", "rolling"),
                                  ("usage", "weekly", "percent")])
def test_missing_any_required_field_fails_closed(path):
    data = document()
    if not path:
        data = []
    else:
        current = data
        for key in path[:-1]:
            current = current[key]
        del current[path[-1]]
    with pytest.raises(ValueError):
        parse_usage(encode(data), now=NOW)


@pytest.mark.parametrize("raw", [b'{"usage":{},"usage":{}}', b'null', b'{}',
                                 b'{"error":{"message":"quota available"}}',
                                 b'\xff', b' ' * 65537])
def test_malformed_oversized_and_duplicate_usage_responses_are_rejected(raw):
    with pytest.raises(ValueError):
        parse_usage(raw, now=NOW)


@pytest.mark.parametrize("now", [True, -1, float("nan"), float("inf"), 10 ** 1000])
def test_invalid_observation_time_is_rejected(now):
    with pytest.raises(ValueError):
        parse_usage(encode(document()), now=now)


def test_percent_zero_and_99_are_valid_and_unrelated_fields_rejected():
    data = document()
    data["usage"]["rolling"]["percent"] = 0
    data["usage"]["weekly"]["percent"] = 99
    assert parse_usage(encode(data), now=NOW)["5h"]["percent"] == 0
    bad = deepcopy(data)
    bad["usage"]["rolling"]["raw_account_secret"] = "never-preserved"
    with pytest.raises(ValueError):
        parse_usage(encode(bad), now=NOW)


def config_data():
    return {"schema_version": 1, "pool_id": "usage-test",
            "accounts": [{"id": "go-a", "quota_group": "a", "credential_file": "keys/a"}],
            "models": {"glm-5.1": "chat/completions"},
            "gateway": {"token_file": "keys/local"}}


def test_usage_config_opt_in_preserves_previous_behavior(tmp_path):
    data = config_data()
    assert parse_config(data, tmp_path).usage_enabled is False
    data["usage"] = {"enabled": True, "max_age": 30, "timeout": 3}
    settings = parse_config(data, tmp_path)
    assert (settings.usage_enabled, settings.usage_max_age, settings.usage_timeout) == (True, 30, 3)


@pytest.mark.parametrize("value", [[], None, {"enabled": "true"}, {"enabled": 1},
                                  {"timeout": 0}, {"timeout": 31}, {"timeout": True},
                                  {"max_age": 0}, {"max_age": float("nan")},
                                  {"max_age": 3601}, {"fail_open": True}])
def test_usage_policy_is_strict_and_bounded(tmp_path, value):
    data = config_data()
    data["usage"] = value
    with pytest.raises(ValueError):
        parse_config(data, tmp_path)
