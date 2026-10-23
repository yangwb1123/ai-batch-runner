"""Real-provider smoke failures stay failures with bounded, useful diagnostics."""

import json
from types import SimpleNamespace

import pytest

from test_tui_integration_codex import _assert_codex_succeeded


def _backend(status="failed", reason="refresh token was already used"):
    return SimpleNamespace(snapshot=lambda: {
        "status": status,
        "last_result": {"returncode": -1, "reason": reason},
        "workspace": {"root": "/private/workspace/must-not-be-reported"},
    })


def test_auth_failure_reason_is_visible_without_private_snapshot_fields():
    with pytest.raises(AssertionError) as error:
        _assert_codex_succeeded(_backend())

    diagnostic = json.loads(str(error.value))
    assert diagnostic == {
        "status": "failed", "returncode": -1,
        "reason": "refresh token was already used",
    }
    assert "private/workspace" not in str(error.value)


def test_provider_diagnostic_is_redacted_before_truncation():
    secret = "sk-proj-fixture0123456789"
    reason = f"API_KEY={secret} expired\n" + "x" * 5000
    with pytest.raises(AssertionError) as error:
        _assert_codex_succeeded(_backend(reason=reason))

    diagnostic = json.loads(str(error.value))
    assert len(diagnostic["reason"]) == 1024
    assert "[REDACTED]" in diagnostic["reason"]
    assert secret not in str(error.value)
    assert "\n" not in str(error.value)


@pytest.mark.parametrize("status", ("failed", "cancelled", "running", "idle", "unknown"))
def test_provider_failure_cannot_become_a_skip_or_success(status):
    with pytest.raises(AssertionError) as error:
        _assert_codex_succeeded(_backend(status=status))

    assert json.loads(str(error.value))["status"] == status


def test_missing_result_remains_a_failure():
    backend = SimpleNamespace(snapshot=lambda: {
        "status": "failed", "last_result": None,
    })
    with pytest.raises(AssertionError) as error:
        _assert_codex_succeeded(backend)

    assert json.loads(str(error.value)) == {
        "status": "failed", "returncode": None, "reason": "",
    }


def test_successful_provider_result_is_still_accepted():
    _assert_codex_succeeded(_backend(status="succeeded"))
