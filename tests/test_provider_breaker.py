"""T17a: provider-level circuit breaker tests (retro finding)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch import provider_breaker as breaker


@pytest.fixture(autouse=True)
def reset_breaker(monkeypatch, tmp_path):
    state_file = tmp_path / "provider-breaker.json"
    monkeypatch.setenv("PBATCH_PROVIDER_BREAKER_FILE", str(state_file))
    monkeypatch.setattr(breaker, "_settings", lambda: {
        "threshold": 3, "base_cooldown": 60.0, "max_cooldown": 60.0})
    breaker.register_success()  # fresh shared state
    yield
    breaker.register_success()


def test_trips_after_threshold_and_recovers_on_success():
    assert not breaker.register_provider_failure("quota")
    assert not breaker.register_provider_failure("quota")
    assert breaker.register_provider_failure("quota")  # 3rd trips cooldown
    assert breaker.cooldown_remaining() > 0
    breaker.register_success()
    assert breaker.cooldown_remaining() == 0
    assert breaker.breaker_state()["streak"] == 0
    assert not breaker.register_provider_failure("x")  # fresh window


def test_windows_state_write_has_no_posix_descriptor_requirements(
        monkeypatch, tmp_path):
    platform_os = SimpleNamespace(**vars(os))
    platform_os.name = "nt"
    platform_os.__dict__.pop("fchmod", None)
    original_open = os.open

    def regular_open(path, flags, *args):
        assert not Path(path).is_dir(), "CRT cannot open Windows directories"
        return original_open(path, flags, *args)

    platform_os.open = regular_open
    monkeypatch.setattr(breaker, "os", platform_os)
    breaker.register_provider_failure("quota", data_dir=tmp_path)
    breaker.register_success(data_dir=tmp_path)
    state = json.loads((tmp_path / "provider-breaker.json").read_text())
    assert state["version"] == 2
    assert breaker.breaker_state(data_dir=tmp_path)["state_error"] == ""


def test_wait_or_fail_waits_then_passes(monkeypatch):
    monkeypatch.setattr(breaker, "_settings", lambda: {
        "threshold": 3, "base_cooldown": 0.1, "max_cooldown": 2.0})
    breaker.register_provider_failure("quota")
    breaker.register_provider_failure("quota")
    breaker.register_provider_failure("quota")  # cooldown 0.2s
    started = time.monotonic()
    assert breaker.wait_or_fail(0.3) is None  # waited through the cooldown
    assert time.monotonic() - started < 0.5
    assert breaker.cooldown_remaining() == 0


def test_wait_budget_exceeded_returns_failure_reason():
    import json
    from pathlib import Path
    state = {"streak": 0, "step": 1,
             "cooldown_until": time.monotonic() + 30.0}
    Path(breaker._state_path()).write_text(json.dumps(state), encoding="utf-8")
    reason = breaker.wait_or_fail(0.05)
    assert reason == "provider breaker cooldown exceeded wait budget"


def test_disabled_when_threshold_zero(monkeypatch):
    monkeypatch.setattr(breaker, "_settings", lambda: {"threshold": 0})
    assert not breaker.enabled()
    assert not breaker.register_provider_failure("quota")
    assert breaker.wait_or_fail(0.0) is None


@pytest.mark.parametrize("value", [
    True, False, None, "bad", float("nan"), float("inf"), float("-inf"),
])
def test_malformed_threshold_uses_safe_default(monkeypatch, value):
    monkeypatch.setattr(breaker, "_settings", lambda: {"threshold": value})
    assert breaker.threshold() == breaker._DEFAULT_THRESHOLD


@pytest.mark.parametrize("value", [0, -1])
def test_explicit_non_positive_threshold_disables(monkeypatch, value):
    monkeypatch.setattr(breaker, "_settings", lambda: {"threshold": value})
    assert breaker.threshold() == 0


def test_corrupt_shared_state_is_recovered_fail_closed(tmp_path):
    state_file = tmp_path / "provider-breaker.json"
    state_file.write_text('{not-json', encoding="utf-8")
    assert not breaker.register_provider_failure("quota")
    state = breaker.breaker_state()
    assert state["active"] is True
    assert state["state_error"] == "malformed"
    breaker.register_success()
    assert breaker.breaker_state()["state_error"] == ""


def test_empty_legacy_state_is_not_treated_as_a_clean_breaker(tmp_path):
    state_file = tmp_path / "provider-breaker.json"
    state_file.write_text("", encoding="utf-8")

    state = breaker.breaker_state()

    assert state["active"] is True
    assert state["state_error"] == "empty"


def test_recursive_shared_state_is_malformed_fail_closed(tmp_path):
    state_file = tmp_path / "provider-breaker.json"
    state_file.write_text("[" * 2000 + "0" + "]" * 2000,
                          encoding="utf-8")

    state = breaker.breaker_state()

    assert state["active"] is True
    assert state["state_error"] == "malformed"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
@pytest.mark.parametrize("target", ["state", "lock"])
def test_special_shared_paths_fail_closed_without_blocking(target):
    state_path = Path(breaker._state_path())
    special = (state_path if target == "state"
               else breaker._lock_path(state_path))
    special.unlink(missing_ok=True)
    os.mkfifo(special)

    state = breaker.breaker_state()

    assert state["active"] is True
    assert state["state_error"] == "unreadable"


def test_state_publish_is_atomic_when_replace_fails(monkeypatch):
    breaker.register_provider_failure("first")
    path = Path(breaker._state_path())
    before = path.read_bytes()
    original_replace = breaker.os.replace

    def crash(*_args):
        raise OSError("injected crash")

    monkeypatch.setattr(breaker.os, "replace", crash)

    assert not breaker.register_provider_failure("second")

    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    monkeypatch.setattr(breaker.os, "replace", original_replace)


def test_state_symlink_is_not_followed_and_global_reset_repairs_it(tmp_path):
    path = Path(breaker._state_path())
    target = tmp_path / "outside.json"
    target.write_text("outside", encoding="utf-8")
    path.unlink()
    path.symlink_to(target)

    assert not breaker.register_provider_failure("quota")
    assert target.read_text(encoding="utf-8") == "outside"
    assert breaker.breaker_state()["state_error"] == "symlink"

    breaker.register_success()
    assert not path.is_symlink()
    assert target.read_text(encoding="utf-8") == "outside"


def test_provider_map_is_bounded_and_oversize_state_fails_closed():
    for index in range(breaker._MAX_PROVIDER_ENTRIES + 20):
        breaker.register_provider_failure(
            "quota", provider=f"provider:{index:04d}")
    path = Path(breaker._state_path())
    state = breaker.breaker_state()
    assert state["provider_count"] <= breaker._MAX_PROVIDER_ENTRIES
    assert path.stat().st_size <= breaker._STATE_MAX_BYTES

    path.write_text("{" + "x" * breaker._STATE_MAX_BYTES + "}",
                    encoding="utf-8")
    broken = breaker.breaker_state()
    assert broken["active"] is True
    assert broken["state_error"] == "oversized"
    assert broken["provider_count"] > 0


def test_success_recovers_identities_from_bounded_overflow(monkeypatch):
    monkeypatch.setattr(breaker, "_settings", lambda: {
        "threshold": 1, "base_cooldown": 1.0, "max_cooldown": 2.0})
    monkeypatch.setattr(breaker, "_now", lambda: 2_000_000_000.0)
    for index in range(breaker._MAX_PROVIDER_ENTRIES + 20):
        breaker.register_provider_failure(
            "quota", provider=f"provider:{index:04d}")
    other = "provider:still-in-overflow"
    assert breaker.cooldown_remaining(provider=other) > 0

    # More than one map's worth of successful unknown identities can each
    # recover at the time evidence arrives, without growing the state.
    for index in range(breaker._MAX_PROVIDER_ENTRIES + 5):
        recovered = f"provider:recovered-{index:04d}"
        assert breaker.cooldown_remaining(provider=recovered) > 0
        breaker.register_success(provider=recovered)
        assert breaker.cooldown_remaining(provider=recovered) == 0

    state = breaker.breaker_state()
    assert state["provider_count"] <= breaker._MAX_PROVIDER_ENTRIES
    # Recovery is identity-specific: the aggregate remains conservative for
    # other identities whose original entries were coalesced.
    assert breaker.cooldown_remaining(provider=other) > 0


def test_legacy_monotonic_deadline_is_clamped_on_clock_epoch_change(
        monkeypatch):
    monkeypatch.setattr(breaker.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(breaker.time, "time", lambda: 2_000_000_000.0)
    Path(breaker._state_path()).write_text(json.dumps({
        "streak": 0, "step": 1, "cooldown_until": 100_000.0,
    }), encoding="utf-8")

    remaining = breaker.cooldown_remaining()

    assert 0 < remaining <= breaker._max_cooldown()
    breaker.register_provider_failure("quota")
    persisted = json.loads(Path(breaker._state_path()).read_text())
    assert persisted["version"] == 2 and persisted["clock"] == "unix"


def test_provider_partitions_do_not_trip_or_reset_each_other():
    for _ in range(3):
        breaker.register_provider_failure("quota", provider="provider:a")
    assert breaker.cooldown_remaining(provider="provider:a") > 0
    assert breaker.cooldown_remaining(provider="provider:b") == 0

    breaker.register_success(provider="provider:b")
    assert breaker.cooldown_remaining(provider="provider:a") > 0
    state = breaker.breaker_state()
    assert state["active_providers"] == ["provider:a"]
    assert state["provider_count"] == 1

    breaker.register_success(provider="provider:a")
    assert breaker.cooldown_remaining(provider="provider:a") == 0


def test_campaign_admission_checks_only_selected_provider():
    from pbatch.campaign_preflight import _provider_breaker_admission

    for _ in range(3):
        breaker.register_provider_failure("quota", provider="provider:cloud-a")
    assert _provider_breaker_admission("cloud-b", "pi") == ("", [])
    reason, evidence = _provider_breaker_admission("cloud-a", "pi")
    assert "provider:cloud-a" in reason
    assert evidence == [breaker._state_path()]


def test_identity_sanitization_does_not_merge_distinct_providers():
    assert breaker.breaker_identity("cloud/a") != breaker.breaker_identity("cloud_a")


def test_legacy_custom_error_patterns_still_classify_provider_health(
        monkeypatch):
    monkeypatch.setattr(breaker.config, "_AGENTS_CFG", {
        "vendor-cli": {"error_patterns": ["vendor_capacity_exhausted"]},
    })
    monkeypatch.setattr(breaker.config, "_AGENT_CFG", {})

    reason = breaker.provider_failure_reason(
        "vendor_capacity_exhausted", bin_name="vendor-cli")

    assert "vendor_capacity_exhausted" in reason


def test_explicit_provider_patterns_win_and_artifact_only_defaults_are_filtered(
        monkeypatch):
    monkeypatch.setattr(breaker.config, "_AGENTS_CFG", {
        "explicit": {
            "error_patterns": ["legacy_vendor_error"],
            "provider_error_patterns": ["health_only_error"],
        },
        "legacy": {"error_patterns": [
            r"context_?length_?exceeded",
            r"(?im)^\[?error\]?:",
            r"(?im)^fatal:",
        ]},
    })
    monkeypatch.setattr(breaker.config, "_AGENT_CFG", {})

    assert breaker.provider_failure_reason(
        "health_only_error", bin_name="explicit")
    assert not breaker.provider_failure_reason(
        "legacy_vendor_error", bin_name="explicit")
    assert not breaker.provider_failure_reason(
        "context_length_exceeded\nERROR: local flag\nfatal: parse",
        bin_name="legacy")


def test_codex_broad_artifact_banner_is_not_a_breaker_signature():
    assert not breaker.provider_failure_reason(
        "codex: local command parsing failed", bin_name="codex")
    assert breaker.provider_failure_reason(
        "insufficient_quota", bin_name="codex")


def test_runner_only_counts_real_provider_failures(monkeypatch):
    from types import SimpleNamespace
    from pbatch import runner
    from pbatch.models import Task

    monkeypatch.setattr(breaker, "_settings", lambda: {
        "threshold": 1, "base_cooldown": 60.0, "max_cooldown": 60.0})
    task = Task(prompt="p", provider="cloud-a")
    identity = breaker.provider_identity(task)

    generic = runner._result_from_proc(
        task, SimpleNamespace(returncode=7), [], ["ordinary crash\n"], 0.1)
    assert not generic.success
    assert breaker.cooldown_remaining(provider=identity) == 0

    context_error = runner._result_from_proc(
        task, SimpleNamespace(returncode=0), ["ordinary answer\n"],
        ["context_length_exceeded\n"], 0.1)
    assert not context_error.success
    assert breaker.cooldown_remaining(provider=identity) == 0

    cli_error = runner._result_from_proc(
        task, SimpleNamespace(returncode=0), ["ordinary answer\n"],
        ["ERROR: bad local flag\n"], 0.1)
    assert not cli_error.success
    assert breaker.cooldown_remaining(provider=identity) == 0

    provider_failure = runner._result_from_proc(
        task, SimpleNamespace(returncode=7), [],
        ["ERROR: insufficient_quota\n"], 0.1)
    assert not provider_failure.success
    assert breaker.cooldown_remaining(provider=identity) > 0


def test_runner_wires_breaker_into_result_path(monkeypatch, tmp_path):
    """A provider-failure rejection registers; success resets; the next
    task in cooldown fails with an explicit reason."""
    from pbatch import runner
    from pbatch.models import Task
    calls = []

    monkeypatch.setattr(breaker, "_settings", lambda: {
        "threshold": 2, "base_cooldown": 60.0, "max_cooldown": 60.0})
    reason = runner.agent_failure_reason(0, "ERROR: insufficient_quota")
    assert reason
    runner._pb_register_fail(reason)
    assert breaker.cooldown_remaining() == 0  # below threshold
    runner._pb_register_fail(reason)  # trips
    assert breaker.cooldown_remaining() > 0

    task = Task(prompt="p")
    breaker.register_success()  # clear shared state
    assert runner._pb_cooldown(task) is None  # no cooldown: run
    # cooldown LONGER than the runner wait budget -> fails fast with reason
    Path(breaker._state_path()).write_text(json.dumps({
        "version": 2, "clock": "unix",
        "streak": 0, "step": 1,
        "cooldown_until": time.time() + 400.0}), encoding="utf-8")
    result = runner._pb_cooldown(task)
    assert result is not None and not result.success
    assert result.reason == "provider breaker cooldown exceeded wait budget"


def test_runner_checks_breaker_before_budget_and_rate(monkeypatch, tmp_path):
    """A synthetic cooldown is a traced failure without paid admission."""
    from pbatch import agent_ledger, config, metering, ratelimit, runner
    from pbatch.models import Task, TaskResult

    task = Task(prompt="blocked", cwd=str(tmp_path), provider="cloud-a")
    monkeypatch.setattr(
        runner, "_task_invocation",
        lambda *_args: (["agent", "blocked"], str(tmp_path), "", "logical"))
    monkeypatch.setattr(
        runner, "_pb_cooldown",
        lambda _task: TaskResult(
            _task, False, returncode=-1, reason="breaker is cooling down"))
    monkeypatch.setattr(runner, "write_marker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner, "clear_marker", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner, "_spawn_agent",
        lambda *_args, **_kwargs: pytest.fail("cooldown must prevent spawn"))

    rate_calls = []
    monkeypatch.setattr(ratelimit, "RATE_PER_SECOND", 1.0)
    monkeypatch.setattr(ratelimit, "PROVIDER_RATES", {})
    monkeypatch.setattr(
        ratelimit, "acquire",
        lambda *_args, **_kwargs: rate_calls.append(True) or True)
    monkeypatch.setattr(metering, "BUDGET_MAX", 0)
    monkeypatch.setattr(metering, "DAILY_BUDGET", 0)
    monkeypatch.setattr(metering, "INVOCATIONS", 0)

    events, ledger = [], []
    monkeypatch.setattr(
        metering, "record_event",
        lambda kind, *_args, **_kwargs: events.append(kind))
    monkeypatch.setattr(config, "AGENT_LEDGER_ENABLED", True)
    monkeypatch.setattr(
        agent_ledger, "record_result",
        lambda result, *_args: ledger.append(result) or True)

    result = runner.run_task(task)

    assert not result.success and result.reason == "breaker is cooling down"
    assert metering.INVOCATIONS == 0
    assert rate_calls == []
    assert events == ["task_fail"]
    assert ledger == [result]
    assert result.task_id and result.attempt == 1


def test_go_gateway_usage_limit_is_a_shipped_default_signature(monkeypatch):
    """opencode-go / Go gateways report quota exhaustion as
    ``GoUsageLimitError`` with a SPACE-separated "Go usage limit exceeded".

    Neither shape matched the original underscore-oriented defaults
    (``quota_?exceeded`` / ``rate_?limit_?error``), so the shared provider
    breaker stayed closed while the 2026-09-19 iris-ui campaign burned ~8h and
    1412 provider errors on consecutive zero-progress rounds. The signature
    must therefore work from the BUILT-IN defaults, with no per-agent config.
    """
    monkeypatch.setattr(breaker.config, "_AGENTS_CFG", {})
    monkeypatch.setattr(breaker.config, "_AGENT_CFG", {})

    stderr = (
        "Warning: No project session found with id 'pb-1'; creating a new "
        "session with that id.\n"
        '429: {"type":"GoUsageLimitError","message":"Go usage limit exceeded"}'
    )
    reason = breaker.provider_failure_reason(stderr, "", "/usr/local/bin/pi")

    assert reason
    assert breaker.is_provider_failure_reason(reason)


def test_go_gateway_session_banner_alone_is_not_a_provider_failure(monkeypatch):
    """The session-recovery warning that precedes the quota line is CLI noise,
    not provider health — it must not trip the shared breaker on its own."""
    monkeypatch.setattr(breaker.config, "_AGENTS_CFG", {})
    monkeypatch.setattr(breaker.config, "_AGENT_CFG", {})

    assert not breaker.provider_failure_reason(
        "Warning: No project session found with id 'pb-1'; creating a new "
        "session with that id.",
        "",
        "/usr/local/bin/pi",
    )
