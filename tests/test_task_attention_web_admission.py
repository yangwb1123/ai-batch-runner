"""Local delivered Web must refuse contradictory attention before rendering any facts."""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from pbatch import task_attention
from pbatch.task_center_report import page_html
from test_task_attention import candidate, empty
from test_task_attention_interfaces import failed_view
from test_task_center import source

NODE = shutil.which("node")
FIXTURE = Path(__file__).parent / "fixtures" / "task_attention_admission_browser.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node required for isolated delivered-script VM")
OLD = "OWNED-OLD-ATTENTION-FACT"
PRIVATE = "PRIVATE-UNSUPPORTED-ATTENTION-FIELD"


def _browser(original, changed, mode="refuse", **options):
    original = copy.deepcopy(original)
    original["attention"]["items"][0]["summary"] = OLD
    script = re.search(r"<script>(.*?)</script>", page_html(), re.S).group(1)
    result = subprocess.run([NODE, str(FIXTURE)], input=json.dumps({"script": script,
        "views": [original, changed], "mode": mode, "old_marker": OLD,
        "private_marker": PRIVATE, **options}), text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("value", [None, False, 0, "unsupported", []])
def test_explicit_invalid_attention_is_not_missing_legacy_or_zero(source, value):
    original = failed_view(source)
    changed = copy.deepcopy(original); changed["attention"] = value
    result = _browser(original, changed)
    assert result["rows"] == 0 and "数据不可用" in result["summary"]


CHANGES = [
    lambda a: a.update(schema_version=True),
    lambda a: a.update(schema_version=2),
    lambda a: a.update(method="unknown"),
    lambda a: a.update(window_only=False),
    lambda a: a.update(observed_items=True),
    lambda a: a.update(observed_items=0),
    lambda a: a.update(omitted_items=True),
    lambda a: a.update(omitted_items=-1),
    lambda a: a.update(grants_execution=True),
    lambda a: a.update(zero_items_proves_safety_or_no_missing_work=True),
    lambda a: a.update(counts_by_kind={"tasks": True}),
    lambda a: a.update(counts_by_reason={"reported_failure": 2}),
    lambda a: a.update(counts_by_reason={"unregistered_reason": 1}),
    lambda a: a.update(counts_by_kind={"unknown": 1}),
    lambda a: a.update(items=a["items"] * 129, observed_items=129),
    lambda a: a["coverage"].update(source_records=True),
    lambda a: a["coverage"].update(source_records=0),
    lambda a: a["coverage"].update(task_candidates=0),
    lambda a: a["coverage"].update(task_window_partial="unknown"),
    lambda a: a["coverage"].update(snapshot_diagnostics=1),
    lambda a: a.update(private_prompt=PRIVATE),
    lambda a: a["items"][0].update(attention_id="not-a-full-id"),
    lambda a: a["items"][0].update(source_id="src-" + "0" * 24),
    lambda a: a["items"][0].update(target={"kind": "tasks", "record_id": "/etc/passwd"}),
    lambda a: a["items"][0].update(target={"kind": "unknown", "record_id": "tc-" + "0" * 32}),
    lambda a: a["items"][0].update(target={"kind": "sources", "record_id": "src-" + "0" * 24}),
    lambda a: a["items"][0].update(target=None),
    lambda a: a["items"][0].update(root_cause="verified"),
    lambda a: a["items"][0].update(operator_action="restart_now"),
    lambda a: a["items"][0]["capabilities"].update(resume=True),
    lambda a: a["items"][0]["capabilities"].update(command=PRIVATE),
    lambda a: a["items"][0]["evidence"].update(private_body=PRIVATE),
    lambda a: a["items"][0].update(private_body=PRIVATE),
    lambda a: a["items"][0]["evidence"].update(partial_window=None),
    lambda a: a["items"][0]["evidence"].update(reported_status="succeeded"),
    lambda a: a["items"][0].update(summary="\ud800"),
    lambda a: a["items"][0].update(attention_id=a["items"][0]["attention_id"] + "\n"),
    lambda a: a["items"][0].update(summary=None),
    lambda a: a["items"][0].update(summary="x" * 1025),
    lambda a: a["items"][0]["capabilities"].pop("control"),
    lambda a: a["coverage"].update(basis="complete_process_inventory"),
]


@pytest.mark.parametrize("change", CHANGES)
def test_closed_projection_counts_scope_target_and_false_authority_refuse(source, change):
    original = failed_view(source); changed = copy.deepcopy(original)
    change(changed["attention"])
    result = _browser(original, changed)
    assert OLD not in result["all"] and PRIVATE not in result["all"]


def test_duplicate_attention_id_cannot_select_an_ambiguous_record(source):
    original = failed_view(source); changed = copy.deepcopy(original)
    a = changed["attention"]
    a.update(items=a["items"] * 2, observed_items=2,
             counts_by_reason={"reported_failure": 2}, counts_by_kind={"tasks": 2})
    _browser(original, changed)


def test_distinct_ids_cannot_relabel_one_complete_reference_as_two_facts(source):
    original = failed_view(source); changed = copy.deepcopy(original)
    a = changed["attention"]; other = copy.deepcopy(a["items"][0])
    other["attention_id"] = "at-" + "0" * 32
    a.update(items=a["items"] + [other], observed_items=2,
             counts_by_reason={"reported_failure": 2}, counts_by_kind={"tasks": 2})
    _browser(original, changed)


@pytest.mark.parametrize("mode", ["late_success", "late_error"])
def test_late_old_success_or_error_cannot_restore_facts_after_refusal(source, mode):
    original = failed_view(source); changed = copy.deepcopy(original)
    changed["attention"] = None
    result = _browser(original, changed, mode)
    assert len(result["requests"]) == 4 and OLD not in result["all"]


def test_empty_new_sources_with_old_coverage_is_refused_not_silent_legacy(source):
    original = failed_view(source); changed = copy.deepcopy(original)
    changed["sources"] = []
    _browser(original, changed)


def test_explicit_source_filter_cannot_display_other_source_refs(source):
    original = failed_view(source)
    _browser(original, original, source="src-" + "b" * 24)


def test_duplicate_admitted_sources_are_not_an_unambiguous_window(source):
    original = failed_view(source); changed = copy.deepcopy(original)
    changed["sources"] *= 2
    changed["attention"]["coverage"]["source_records"] = 2
    _browser(original, changed)


def test_missing_legacy_attention_remains_untracked_without_zero_safety_claim(source):
    original = failed_view(source); changed = copy.deepcopy(original)
    del changed["attention"]
    result = _browser(original, changed, "legacy", expected_rows=0)
    assert "未追踪" in result["attention_summary"]


def test_actual_empty_attention_is_a_bounded_zero_not_complete_or_safe(source):
    original = failed_view(source); changed = copy.deepcopy(original)
    a = changed["attention"]
    a.update(items=[], observed_items=0, omitted_items=0, counts_by_reason={}, counts_by_kind={})
    result = _browser(original, changed, "valid", expected_rows=0)
    assert "已观察 0" in result["attention_summary"] and "不证明完整覆盖" in result["attention_summary"]


def test_pretrim_counts_and_references_outside_task_display_are_still_admitted(source):
    original = failed_view(source); changed = copy.deepcopy(original)
    payload = empty(changed["sources"]); payload["diagnostics"] = changed["diagnostics"]
    candidates = [candidate(number=i, source=changed["sources"][0]) for i in range(1, 302)]
    changed["attention"] = task_attention.attention_view(payload, candidates)
    changed["observed_task_count"] = 301
    result = _browser(original, changed, "valid", expected_rows=128)
    assert "已观察 301" in result["attention_summary"] and "引用遗漏 173" in result["attention_summary"]
    assert json.loads(result["detail"])["target"]["record_id"] not in {
        task["task_id"] for task in changed["tasks"]}


@pytest.mark.parametrize("state", list(task_attention._TASK_STATES))
def test_all_registered_reported_task_states_are_observations_not_execution_grants(source, state):
    original = failed_view(source); changed = copy.deepcopy(original)
    payload = empty(changed["sources"]); payload["diagnostics"] = changed["diagnostics"]
    changed["attention"] = task_attention.attention_view(payload, [candidate(state, source=changed["sources"][0])])
    result = _browser(original, changed, "valid", expected_rows=1)
    assert json.loads(result["detail"])["evidence"]["reported_status"] == state


@pytest.mark.parametrize("kind", list(task_attention._METADATA))
def test_registered_metadata_reasons_allow_refs_outside_display_and_no_private_bodies(source, kind):
    original = failed_view(source); changed = copy.deepcopy(original)
    payload = empty(changed["sources"]); payload["diagnostics"] = changed["diagnostics"]
    field, attribute, states = task_attention._METADATA[kind]
    prefix = {"goals": "g", "intents": "ih", "repairs": "rq", "resources": "rr", "feedback": "fd"}[kind]
    for state in states:
        payload[kind] = [{"source_id": source["source_id"], "project": "owned", field: prefix + "-" + "f" * 32,
                          attribute: state, "version": 2, "private_body": PRIVATE}]
        changed["attention"] = task_attention.attention_view(payload, [])
        changed["observed_task_count"] = 0
        result = _browser(original, changed, "valid", expected_rows=1)
        assert PRIVATE not in result["all"] and json.loads(result["detail"])["target"]["kind"] == kind


def test_gap_feed_references_preserve_bounds_omissions_and_unknowns_not_liveness(source):
    original = failed_view(source); changed = copy.deepcopy(original)
    changed["sources"][0]["coverage"] = [{"feed": "synthetic-gap-" + str(n), "truncated": True,
        "incomplete": None, "malformed": None, "error": None} for n in range(12)]
    payload = empty(changed["sources"]); payload["diagnostics"] = changed["diagnostics"]
    changed["attention"] = task_attention.attention_view(payload, [])
    changed["observed_task_count"] = 0
    expected = source["name"] + "/synthetic-gap-{}: 数据不可读、损坏或窗口不完整"
    result = _browser(original, changed, "valid", expected_rows=1,
                      expected_error="；".join(expected.format(n) for n in range(12)))
    value = json.loads(result["detail"])
    assert len(value["evidence"]["feeds"]) == 8 and value["evidence"]["omitted_feed_refs"] == 4
    assert value["evidence"]["live_process_liveness"] == "not_verified"


def test_full_unicode_pairs_are_literal_supported_metadata_not_orphan_surrogates(source):
    original = failed_view(source); changed = copy.deepcopy(original)
    changed["attention"]["items"][0]["summary"] = "quoted \U0001f600 metadata"
    result = _browser(original, changed, "valid", expected_rows=1)
    assert "\U0001f600" in result["detail"]


def test_declared_stable_order_cannot_silently_become_arbitrary_or_severity_ranking(source):
    original = failed_view(source); changed = copy.deepcopy(original)
    payload = empty(changed["sources"]); payload["diagnostics"] = changed["diagnostics"]
    values = [candidate(number=i, source=changed["sources"][0]) for i in range(1, 4)]
    changed["attention"] = task_attention.attention_view(payload, values)
    changed["observed_task_count"] = 3
    changed["attention"]["items"].reverse()
    _browser(original, changed)


def test_literal_labels_are_not_dom_controls_or_followup_requests(source):
    original = failed_view(source); changed = copy.deepcopy(original)
    quote = '<img src=x><script>quoted_not_executed()</script>'
    changed["attention"]["items"][0]["summary"] = quote
    result = _browser(original, changed, "valid", expected_rows=1)
    assert quote in result["detail"] and result["tags"] == 0 and len(result["requests"]) == 2
