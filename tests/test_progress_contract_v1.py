"""Pure, bounded progress.v1 contract and projection regressions."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import builtins
import io
import itertools
import json
import logging
import pathlib
import random
import socket
import time

import pytest

from pbatch import progress_contract_v1 as contract
from pbatch import progress_projector_v1 as projector

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
FIELD_MAP = {
    "tenant_id": "tenant", "workspace_id": "workspace", "task_id": "task",
    "run_id": "run", "sequence": "seq", "observed_at": "at",
    "state": ("facts", "status"), "completed_units": ("facts", "done"),
    "total_units": ("facts", "total"),
    **{"usage." + key: ("meter", key) for key in (
        "input_tokens", "output_tokens", "cost_amount", "cost_currency")},
}
STATE_MAP = {"running": ["ACTIVE"], "succeeded": ["DONE"], "failed": ["BAD"]}


def row(**changes):
    value = {
        "tenant": "tenant-a", "workspace": "workspace-a", "task": "task-a",
        "run": "run-a", "seq": 1, "at": "2026-08-01T11:59:00Z",
        "facts": {"status": "ACTIVE", "done": 1, "total": 3},
        "meter": {"input_tokens": None, "output_tokens": None,
                  "cost_amount": None, "cost_currency": None},
    }
    value.update(changes)
    return value


def project(rows=None, **changes):
    options = {"now": NOW, "field_map": FIELD_MAP, "state_map": STATE_MAP}
    options.update(changes)
    return projector.project_progress([row()] if rows is None else rows, **options)


def record(**changes):
    return project([row(**changes)])["records"][0]


def test_complete_payload_and_authoritative_fields():
    payload = project()
    assert contract.validate_progress(payload) is None
    assert set(payload) == set(contract.ENVELOPE_FIELDS)
    assert contract.SCHEMA_VERSION == 1
    assert payload["schema"] == contract.SCHEMA == "progress.v1"
    assert payload["generated_at"] == "2026-08-01T12:00:00.000000Z"
    assert payload["stale_after_seconds"] == 300
    assert payload["truncated"] is False
    item = payload["records"][0]
    assert set(item) == set(contract.RECORD_FIELDS)
    assert set(item["usage"]) == set(contract.USAGE_FIELDS)
    assert item["state"] == "running" and item["percent"] == 33.33
    assert item["eta_seconds"] is None and item["stale"] is False
    assert json.loads(json.dumps(payload, allow_nan=False)) == payload


@pytest.mark.parametrize("level", ["envelope", "record", "usage"])
def test_exact_required_and_unknown_contract_keys(level):
    payload = project()
    target = {"envelope": payload, "record": payload["records"][0],
              "usage": payload["records"][0]["usage"]}[level]
    for key in tuple(target):
        saved = target.pop(key)
        with pytest.raises(contract.ProgressContractError):
            contract.validate_progress(payload)
        target[key] = saved
    target["unrecognized"] = "secret-value"
    with pytest.raises(contract.ProgressContractError):
        contract.validate_progress(payload)


@pytest.mark.parametrize("schema", ["progress.v2", "progress", 1, None, True])
def test_unsupported_schema(schema):
    payload = project()
    payload["schema"] = schema
    with pytest.raises(contract.ProgressContractError):
        contract.validate_progress(payload)


def test_explicit_maps_and_no_implicit_state_meanings():
    mappings = {name: name for name in contract.REQUIRED_FIELD_MAP_KEYS}
    source = {"tenant_id": "t", "workspace_id": "w", "task_id": "a",
              "run_id": "r", "sequence": 0, "observed_at": "2026-08-01T12:00:00Z",
              "state": "succeeded", "completed_units": 100}
    item = project([source], field_map=mappings, state_map={})["records"][0]
    assert item["state"] == "unknown" and item["completed_units"] is None
    assert item["percent"] is None and all(v is None for v in item["usage"].values())
    for state in contract.STATES:
        output = project([row(facts={"status": state, "done": 0, "total": 1})],
                         state_map={state: [state]})
        assert output["records"][0]["state"] == state


@pytest.mark.parametrize("facts", [{}, {"status": None}, None])
def test_absent_or_null_state_evidence(facts):
    assert record(facts=facts)["state"] == "unknown"


@pytest.mark.parametrize("state", ["running", "unexpected", "", 0, False, [], {}])
def test_unmapped_or_invalid_nonnull_state(state):
    with pytest.raises(contract.ProgressContractError):
        record(facts={"status": state})


@pytest.mark.parametrize("key", ["tenant", "workspace", "task", "run", "seq", "at"])
def test_required_evidence_cannot_be_absent_or_null(key):
    source = row()
    del source[key]
    with pytest.raises(contract.ProgressContractError):
        project([source])
    source[key] = None
    with pytest.raises(contract.ProgressContractError):
        project([source])


@pytest.mark.parametrize("identity", ["", "  ", 3, True, "é" * 65, "\ud800"])
def test_identity_bounds(identity):
    with pytest.raises(contract.ProgressContractError):
        record(tenant=identity)
    assert record(tenant="é" * 64)["tenant_id"] == "é" * 64


def test_usage_unknown_zero_currency_and_cumulative_snapshots():
    assert record()["usage"] == dict.fromkeys(contract.USAGE_FIELDS)
    zero = dict(input_tokens=0, output_tokens=0, cost_amount=0, cost_currency=None)
    assert record(meter=zero)["usage"] == zero
    sources = [row(seq=1, meter={**zero, "input_tokens": 5}),
               row(seq=2, meter={**zero, "input_tokens": 8, "cost_currency": "EUR"})]
    item = project(sources)["records"][0]
    assert item["usage"]["input_tokens"] == 8
    assert item["usage"]["cost_currency"] == "EUR"
    assert record(meter=None)["usage"] == dict.fromkeys(contract.USAGE_FIELDS)
    assert record(meter={"cost_amount": 2})["usage"]["cost_currency"] is None


@pytest.mark.parametrize("done,total,percent", [
    (1, 3, 33.33), (1, 32, 3.13), (0, 10, 0), (10, 10, 100),
    (0, 0, None), (None, 4, None), (4, None, None), (None, None, None),
    (contract.MAX_SAFE_INTEGER, contract.MAX_SAFE_INTEGER, 100),
])
def test_percent_eta_and_no_inferred_success(done, total, percent):
    item = record(facts={"done": done, "total": total})
    assert item["percent"] == percent and item["eta_seconds"] is None
    assert item["state"] == "unknown"


@pytest.mark.parametrize("done,total", [(1, 0), (4, 3)])
def test_inconsistent_counters(done, total):
    with pytest.raises(contract.ProgressContractError):
        record(facts={"done": done, "total": total})


@pytest.mark.parametrize("bad", [True, False, -1, 1.5, "1", float("nan"),
                                  float("inf"), -float("inf"), 2 ** 53])
def test_invalid_integer_metrics(bad):
    for field in ("completed_units", "total_units", "input_tokens", "output_tokens"):
        mapping = {**FIELD_MAP, field if "units" in field else "usage." + field: "bad"}
        with pytest.raises(contract.ProgressContractError):
            project([row(bad=bad)], field_map=mapping)
    with pytest.raises(contract.ProgressContractError):
        record(seq=bad)


@pytest.mark.parametrize("bad", [True, -1, "0", float("nan"), float("inf"),
                                  -float("inf"), contract.MAX_COST_AMOUNT + 1])
def test_invalid_cost_amount(bad):
    with pytest.raises(contract.ProgressContractError):
        record(meter={"cost_amount": bad})


@pytest.mark.parametrize("bad", ["usd", "US", "USDD", "$", " USD", "\u00c9UR", 0, False])
def test_invalid_currency(bad):
    with pytest.raises(contract.ProgressContractError):
        record(meter={"cost_currency": bad})


def test_exact_duplicates_and_greatest_sequence_not_latest_timestamp():
    older_sequence = row(seq=1, at="2026-08-01T12:00:00Z")
    latest = row(seq=2, at="2026-08-01T11:58:00+00:00")
    equivalent = deepcopy(latest)
    equivalent["at"] = "2026-08-01T11:58:00.000000Z"
    equivalent["ignored"] = "not projected evidence"
    output = project([older_sequence, latest, deepcopy(latest), equivalent])
    assert len(output["records"]) == 1
    assert output["records"][0]["sequence"] == 2 and output["truncated"] is False


@pytest.mark.parametrize("change", [
    {"facts": {"status": "DONE", "done": 1, "total": 3}},
    {"at": "2026-08-01T11:59:01Z"}, {"meter": {"input_tokens": 0}},
])
def test_conflicting_duplicates_even_when_superseded(change):
    sources = [row(), row(**change), row(seq=2)]
    for permutation in itertools.permutations(sources):
        with pytest.raises(contract.ProgressContractError):
            project(list(permutation), limit=1)


def test_tenant_workspace_isolation_sort_ties_shuffling_and_no_currency_conversion():
    sources = [row(tenant=t, workspace=w, task=k, run=r) for t, w, k, r in (
        ("b", "a", "a", "a"), ("a", "b", "a", "a"), ("a", "a", "b", "a"),
        ("a", "a", "a", "b"), ("a", "a", "a", "a"))]
    sources.append(row(tenant="z", at="2026-08-01T12:00:00Z",
                       meter={"cost_amount": 1, "cost_currency": "JPY"}))
    expected = project(sources)
    ids = [tuple(item[key] for key in contract.IDENTITY_FIELDS)
           for item in expected["records"]]
    assert ids[0][0] == "z" and ids[1:] == sorted(ids[1:])
    for seed in range(12):
        shuffled = deepcopy(sources)
        random.Random(seed).shuffle(shuffled)
        assert project(shuffled) == expected


def test_empty_limit_and_exact_truncation_after_dedupe():
    empty = project([])
    assert empty["records"] == [] and empty["truncated"] is False
    assert project(())["records"] == []
    contract.validate_progress(empty)
    sources = [row(task=str(i)) for i in range(201)]
    for limit in (1, 100, 200):
        result = project(sources + [deepcopy(sources[0])], limit=limit)
        assert len(result["records"]) == limit and result["truncated"] is True
        exact = project(sources[:limit] * 2, limit=limit)
        assert len(exact["records"]) == limit and exact["truncated"] is False
    assert project([row()] * 1000, limit=1)["truncated"] is False


@pytest.mark.parametrize("bad", [0, -1, 201, True, 1.5, None, "1"])
def test_limit_bounds(bad):
    with pytest.raises(contract.ProgressContractError):
        project([], limit=bad)


@pytest.mark.parametrize("bad", [None, {}, "[]", iter([]), (v for v in [])])
def test_unsupported_rows(bad):
    with pytest.raises(contract.ProgressContractError):
        projector.project_progress(bad, now=NOW, field_map=FIELD_MAP, state_map=STATE_MAP)


def test_pretraversal_and_raw_row_budgets():
    with pytest.raises(contract.ProgressContractError):
        project([object()] * 1001)
    bad_rows = [row(extra="x" * 4097), row(extra="é" * 2049),
                row(extra={str(i): 0 for i in range(65)}), row(extra=[0] * 65),
                row(extra={"a": {"b": {"c": {}}}}), row(extra=object()),
                row(extra={1: "non-string key"}), row(extra="\ud800"),
                row(extra=["x" * 4096] * 4)]
    for source in bad_rows:
        with pytest.raises(contract.ProgressContractError):
            project([row(task="first"), source], limit=1)
    assert record(extra={"a": {"b": {"c": 0}}})["task_id"] == "task-a"
    assert record(extra=["x" * 3000] * 4)["task_id"] == "task-a"
    assert record(extra=[0] * 64)["task_id"] == "task-a"


@pytest.mark.parametrize("kind,value", [
    ("field_map", None), ("field_map", {"typo": "source"}),
    ("field_map", {**FIELD_MAP, "percent": "bad"}),
    ("field_map", {**FIELD_MAP, "state": []}),
    ("field_map", {**FIELD_MAP, "state": ["x"] * 5}),
    ("field_map", {**FIELD_MAP, "state": ""}),
    ("field_map", {**FIELD_MAP, "state": [1]}),
    ("field_map", {**FIELD_MAP, "state": "x" * 4097}),
    ("field_map", {str(i): "x" for i in range(65)}),
    ("state_map", None), ("state_map", {"ACTIVE": "running"}),
    ("state_map", {"running": "ACTIVE"}), ("state_map", {"running": [False]}),
    ("state_map", {"running": ["x"] * 65}),
    ("state_map", {"running": ["x"], "failed": ["x"]}),
    ("state_map", {"running": ["x", "x"]}),
    ("state_map", {"running": ["x" * 129]}),
    ("state_map", {str(i): [] for i in range(8)}),
])
def test_malformed_and_oversized_mappings(kind, value):
    with pytest.raises(contract.ProgressContractError):
        project([], **{kind: value})


def test_exact_canonical_row_byte_boundary():
    source = row(extra=["x" * 4096] * 3 + [""])
    encoded = lambda: json.dumps(source, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8")
    source["extra"][-1] = "x" * (contract.MAX_ROW_BYTES - len(encoded()))
    assert len(encoded()) == contract.MAX_ROW_BYTES
    assert project([source])["records"][0]["task_id"] == "task-a"
    source["extra"][-1] += "x"
    with pytest.raises(contract.ProgressContractError):
        project([source])


def test_invalid_superseded_or_unselected_evidence_is_not_hidden():
    for invalid in (row(seq=0, meter={"input_tokens": -1}),
                    row(task="omitted", facts={"status": "not-declared"})):
        with pytest.raises(contract.ProgressContractError):
            project([row(seq=2), invalid], limit=1)


def test_mapping_total_budget_and_literal_keys():
    with pytest.raises(contract.ProgressContractError):
        project([], state_map={"running": [str(i) for i in range(64)], "failed": ["x"]})
    source = row(**{"literal.key": "ACTIVE"})
    assert project([source], field_map={**FIELD_MAP, "state": "literal.key"})["records"][0]["state"] == "running"
    with pytest.raises(contract.ProgressContractError):
        record(facts="not an object")


@pytest.mark.parametrize("fraction", ["1", "12", "123", "1234", "12345", "123456"])
def test_one_to_six_fractional_digits_work_on_python39_and_later(fraction):
    stamp = "2026-08-01T12:00:00." + fraction + "Z"
    result = project([row(at=stamp)], now=stamp)
    expected = "2026-08-01T12:00:00." + fraction.ljust(6, "0") + "Z"
    assert result["generated_at"] == result["records"][0]["observed_at"] == expected


@pytest.mark.parametrize("stamp", ["2026-08-01T12:00:00Z", "2026-08-01T12:00:00+00:00",
                                    "2026-08-01T12:00:00.000000Z"])
def test_utc_serialization_and_injected_now(stamp):
    result = project([row(at=stamp)], now=stamp)
    assert result["generated_at"] == result["records"][0]["observed_at"]
    assert result["generated_at"] == "2026-08-01T12:00:00.000000Z"


@pytest.mark.parametrize("stamp", ["2026-08-01T12:00:00", "2026-08-01T13:00:00+01:00",
                                    "2026-08-01T12:00:00-00:00", "2026-08-01", 0,
                                    "2026-08-01T12:00:00.0000001Z", "secret timestamp",
                                    "2026-02-30T12:00:00Z", None])
def test_invalid_timestamp_facts(stamp):
    for options in ({"now": stamp}, {}):
        with pytest.raises(contract.ProgressContractError):
            project([row(at=stamp)], **options)
    with pytest.raises(contract.ProgressContractError):
        project(now=datetime(2026, 8, 1, 12))


@pytest.mark.parametrize("bad", [None, True, 0, "2026-08-01T12:00:00",
                                  NOW.replace(tzinfo=None),
                                  NOW.astimezone(timezone(timedelta(hours=1)))])
def test_invalid_now_is_rejected_even_with_valid_rows(bad):
    with pytest.raises(contract.ProgressContractError):
        project(now=bad)


@pytest.mark.parametrize("seconds,stale", [(299.999999, False), (300, True), (301, True)])
def test_stale_exact_threshold_preserves_terminal_states(seconds, stale):
    observed = (NOW - timedelta(seconds=seconds)).isoformat()
    for state in ("succeeded", "failed", "cancelled"):
        item = project([row(at=observed, facts={"status": state})],
                       state_map={state: [state]})["records"][0]
        assert item["stale"] is stale and item["state"] == state


@pytest.mark.parametrize("bad", [0, -1, 86401, True, 1.5, None])
def test_stale_configuration_bounds(bad):
    with pytest.raises(contract.ProgressContractError):
        project([], stale_after_seconds=bad)


def test_future_observation_and_stale_range_endpoints():
    with pytest.raises(contract.ProgressContractError):
        record(at="2026-08-01T12:00:00.000001Z")
    for seconds in (1, 86400):
        item = project([row(at=(NOW - timedelta(seconds=seconds)).isoformat())],
                       stale_after_seconds=seconds)["records"][0]
        assert item["stale"] is True


@pytest.mark.parametrize("key,bad", [
    ("sequence", True), ("sequence", 2 ** 53), ("completed_units", -1),
    ("total_units", 0), ("percent", 33.34), ("percent", True),
    ("percent", float("nan")), ("eta_seconds", 0), ("stale", True),
    ("stale", 0), ("observed_at", "2026-08-01T11:59:00Z"), ("state", "DONE"),
])
def test_validator_independently_checks_derived_fields_and_types(key, bad):
    payload = project()
    payload["records"][0][key] = bad
    with pytest.raises(contract.ProgressContractError):
        contract.validate_progress(payload)


@pytest.mark.parametrize("key,bad", [
    ("input_tokens", True), ("output_tokens", -1), ("input_tokens", 2 ** 53),
    ("cost_amount", float("nan")), ("cost_amount", float("inf")),
    ("cost_amount", -1), ("cost_amount", True), ("cost_currency", "usd"),
])
def test_validator_independently_checks_usage(key, bad):
    payload = project()
    payload["records"][0]["usage"][key] = bad
    with pytest.raises(contract.ProgressContractError):
        contract.validate_progress(payload)


def test_validator_checks_envelope_budgets_duplicates_and_order():
    payload = project([row(task="a"), row(task="b")])
    changes = [{"generated_at": "2026-08-01T11:00:00.000000Z"},
               {"truncated": 1}, {"stale_after_seconds": True},
               {"records": tuple(payload["records"])},
               {"records": payload["records"] * 101},
               {"records": payload["records"] * 2},
               {"records": list(reversed(payload["records"]))},
               {"records": [], "truncated": True}]
    for change in changes:
        with pytest.raises(contract.ProgressContractError):
            contract.validate_progress({**payload, **change})


def test_inputs_immutable_repeatable_and_exceptions_sanitized():
    sources, fields, states = [row(), row(seq=2)], deepcopy(FIELD_MAP), deepcopy(STATE_MAP)
    before = deepcopy((sources, fields, states))
    first = project(sources, field_map=fields, state_map=states)
    first["records"][0]["usage"]["cost_currency"] = "USD"
    assert project(sources, field_map=fields, state_map=states) == project(sources)
    assert (sources, fields, states) == before
    with pytest.raises(contract.ProgressContractError) as error:
        record(facts={"status": "secret-state-value"})
    assert "secret-state-value" not in str(error.value)


def test_no_filesystem_network_clock_or_logging_side_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected side effect")

    class NoClock(datetime):
        now = utcnow = today = forbidden

    with monkeypatch.context() as guard:
        for target in (builtins, io):
            guard.setattr(target, "open", forbidden)
        for name in ("open", "read_text", "write_text", "mkdir", "unlink", "stat"):
            guard.setattr(pathlib.Path, name, forbidden)
        for name in ("time", "monotonic", "perf_counter", "sleep"):
            guard.setattr(time, name, forbidden)
        guard.setattr(socket, "socket", forbidden)
        guard.setattr(socket, "create_connection", forbidden)
        guard.setattr(socket, "getaddrinfo", forbidden)
        guard.setattr(logging.Logger, "_log", forbidden)
        guard.setattr(contract, "datetime", NoClock)
        result = project(now=NOW.isoformat())
        contract.validate_progress(result)
        with pytest.raises(contract.ProgressContractError):
            project([row(facts={"status": "bad"})], now=NOW.isoformat())
    assert result == project()
