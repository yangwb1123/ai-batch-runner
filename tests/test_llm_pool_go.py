"""Go quota types and retry timing follow the official handler wire contract."""

from datetime import datetime, timezone
from email.utils import format_datetime
import json

import pytest

from pbatch.llm_pool_go import classify_failure, MAX_RETRY_SECONDS


NOW = 1_790_000_000.0


def wire(code="GoUsageLimitError", limit="5 hour", **extra):
    return json.dumps({"type": "error", "error": {"type": code,
                       "message": "localized provider text; not used for classification"},
                       "metadata": {"workspace": "wrk_fixture", "limitName": limit},
                       **extra}).encode()


@pytest.mark.parametrize("limit,expected", [("5 hour", "5h"), ("weekly", "weekly"),
                                           ("monthly", "monthly"), ("unknown", "unknown")])
def test_go_subscription_quota_is_account_scoped_and_uses_top_level_metadata(limit, expected):
    result = classify_failure(429, {"Retry-After": "120"}, wire(limit=limit), now=NOW)
    assert result.kind == "quota"
    assert result.window == expected
    assert result.scope == "account"
    assert result.retry_at == NOW + 120


@pytest.mark.parametrize("code,window", [("CreditsError", "balance"),
                                        ("MonthlyLimitError", "monthly"), ("UserLimitError", "user")])
def test_401_billing_exhaustion_is_not_misclassified_as_auth(code, window):
    result = classify_failure(401, {}, wire(code), now=NOW)
    assert (result.kind, result.window, result.retry_at) == ("quota", window, None)


@pytest.mark.parametrize("code,status,kind", [
    ("AuthError", 401, "auth"), ("EntitlementError", 403, "auth"),
    ("ModelError", 401, "request"), ("RegionError", 403, "request"),
    ("DataPolicyError", 403, "request"), ("RateLimitError", 429, "rate"),
    ("FreeUsageLimitError", 429, "rate"), ("GoUsageLimitError", 500, "service"),
    ("GoUsageLimitError", 400, "request"),
])
def test_only_verified_status_type_pairs_authorize_failover(code, status, kind):
    assert classify_failure(status, {}, wire(code), now=NOW).kind == kind


@pytest.mark.parametrize("body", [
    b"quota exceeded", b'{"error":{"message":"quota exceeded"}}',
    b'{"error":{"type":"GoUsageLimitError","message":"quota"}}',
    b'{"type":"error","error":{"code":"GoUsageLimitError","message":"quota"}}',
    b'{"type":"error","error":{"type":"GoUsageLimitError"}}',
    b'{"type":"error","error":{"type":"GoUsageLimitError","type":"other","message":"x"}}',
    b'[]', b'null', b'{"type":NaN}', b'\xff', b'{broken', b'x' * 65537,
])
def test_unknown_malformed_oversized_and_prose_errors_never_authorize_failover(body):
    assert classify_failure(429, {}, body, now=NOW).kind == "rate"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "1.5", "bad-date", "1e3",
                                  "", "x" * 129, str(MAX_RETRY_SECONDS + 1)])
def test_invalid_retry_after_is_unknown(value):
    assert classify_failure(429, {"retry-after": value}, wire(), now=NOW).retry_at is None


def test_http_date_and_zero_retry_are_bounded_and_case_insensitive():
    date = format_datetime(datetime.fromtimestamp(NOW + 60, tz=timezone.utc), usegmt=True)
    assert classify_failure(429, {"RETRY-AFTER": date}, wire(), now=NOW).retry_at == NOW + 60
    assert classify_failure(429, {"retry-after": "0"}, wire(), now=NOW).retry_at == NOW + 1
    old = format_datetime(datetime.fromtimestamp(NOW - 60, tz=timezone.utc), usegmt=True)
    assert classify_failure(429, {"retry-after": old}, wire(), now=NOW).retry_at is None
    duplicate = {"Retry-After": "1", "retry-after": "20"}
    assert classify_failure(429, duplicate, wire(), now=NOW).retry_at is None


@pytest.mark.parametrize("metadata", [None, [], {"limitName": []}, {"limitName": "other"}])
def test_unknown_metadata_is_conservatively_account_scoped(metadata):
    result = classify_failure(429, {}, wire(metadata=metadata), now=NOW)
    assert (result.kind, result.window, result.scope) == ("quota", "unknown", "account")


def test_classification_never_returns_provider_message_or_workspace():
    body = wire(error={"type": "GoUsageLimitError", "message": "provider-secret-123"})
    result = classify_failure(429, {}, body, now=NOW)
    assert "provider-secret" not in repr(result)
    assert "wrk_fixture" not in repr(result)
