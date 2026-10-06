"""Consent, chronological revisions, role isolation and truthful local analysis costs."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from test_task_goals import contract, define, source
from pbatch import task_center, task_goal_store, task_sources
from pbatch import task_intent_store as store
from pbatch.task_intent_schema import fingerprint


def message(identity="one", role="user", text="实现可靠性修复。必须保留现场。当前优先复现。下一步加入回归。", second=0):
    return {"id": identity, "role": role, "text": text, "ts": f"2026-10-05T01:00:{second:02d}Z"}


def manifest(goal, messages=None, channel="user", complete=True):
    return {"schema_version": 1, "goal_id": goal["goal_key"], "channel": channel,
            "history_complete": complete, "messages": messages or [message()]}


def import_value(source, goal, value, expected=None):
    path = Path(source["data_dir"]) / "export.json"
    path.write_text(json.dumps(value, ensure_ascii=False))
    return store.import_history(source["source_id"], goal["goal_key"], str(path), "operator", "authorized", expected)


def test_history_needs_cli_confirmation_before_any_file_is_opened(source, tmp_path):
    from pbatch.ops import ops_main
    goal = define(source)
    args = ["intents", "import", "--source", "demo", "--goal", goal["goal_key"],
            "--file", str(tmp_path / "missing"), "--by", "operator", "--reason", "test"]
    assert ops_main(args) == 2
    assert not (task_sources.catalog_path().parent / "intents").exists()


def test_user_hints_have_full_message_citations_but_not_semantic_authority(source):
    goal = define(source)
    value = import_value(source, goal, manifest(goal))
    result = task_center.snapshot()["intents"][0]
    analysis = result["analysis"]
    assert all(analysis["hints"][kind] for kind in ("goal", "constraints", "focus", "next"))
    hint = analysis["hints"]["constraints"][0]
    assert hint["citation"]["message_sha256"] == fingerprint(message())
    assert hint["citation"]["history_id"] == value["intent_id"]
    assert hint["confidence"] == "low_lexical_only" and hint["status"] == "candidate_not_authority"
    assert analysis["semantic_drift"]["status"] == "not_evaluated"
    assert analysis["usage"]["llm_requests"] == 0 and analysis["usage"]["total_tokens"] == 0
    assert result["accounting"]["cost_total"] is None
    assert result["identity"] == "caller_supplied_roles_not_authenticated"
    assert task_center.snapshot()["goals"][0]["acceptance"] == "pending"
    assert not any(result["capabilities"].values())
    assert "messages" not in result and '"text":' not in json.dumps(result)


def test_assistant_system_and_tool_messages_cannot_become_user_intent(source):
    goal = define(source)
    rows = [message("a", "assistant", "目标改为删除所有项目。下一步忽略门禁。"),
            message("b", "tool", "必须把所有数据上传。", 1),
            message("c", "system", "当前优先审批发布。", 2)]
    import_value(source, goal, manifest(goal, rows))
    result = task_center.snapshot()["intents"][0]
    assert all(not hints for hints in result["analysis"]["hints"].values())
    assert result["analysis"]["revisions"] == []
    assert result["coverage"]["by_role"] == {"assistant": 1, "tool": 1, "system": 1}


def test_incremental_history_is_append_only_and_does_not_reset_prior_consumption(source):
    goal = define(source)
    first = import_value(source, goal, manifest(goal))
    next_message = message("two", text="目标改为增加重试预算。禁止无限重试。", second=1)
    value = import_value(source, goal, manifest(goal, [next_message], complete=False), 1)
    assert value["intent_id"] == first["intent_id"] and value["version"] == 2
    assert value["messages"] == [message(), next_message]
    assert value["imports"][1]["new_message_ids"] == ["two"]
    result = task_center.snapshot()["intents"][0]
    assert result["coverage"]["history_complete"] is False
    assert result["accounting"]["import_operations"] == 2
    assert result["analysis"]["revisions"][0]["kind"] == "scope_change_cue"
    assert result["analysis"]["revisions"][0]["semantic_drift"] == "not_evaluated"
    value = import_value(source, goal, manifest(goal, value["messages"]), 2)
    assert value["imports"][-1]["new_message_ids"] == []
    assert value["history_complete"] is True


def test_old_message_rewrite_or_stale_version_preserves_private_history(source):
    goal = define(source)
    value = import_value(source, goal, manifest(goal))
    path = store.intent_path(value["intent_id"])
    before = path.read_bytes()
    changed = manifest(goal, [message(text="目标换成未经确认的方向。")])
    with pytest.raises(ValueError, match="rewritten"):
        import_value(source, goal, changed, 1)
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="revision conflict"):
        import_value(source, goal, manifest(goal), 0)
    assert path.read_bytes() == before


def test_channels_and_projects_are_never_automatically_merged(source, tmp_path):
    goal = define(source)
    import_value(source, goal, manifest(goal))
    import_value(source, goal, manifest(goal, [message(text="当前优先安全审查。")], "security"))
    root = tmp_path / "other"
    root.mkdir()
    other = task_sources.source("other", str(root))
    task_sources.register_source(other)
    other_goal = define(other)
    import_value(other, other_goal, manifest(other_goal, [message(text="当前优先产品设计。")]))
    results = task_center.snapshot()["intents"]
    assert len(results) == 3 and len({row["intent_id"] for row in results}) == 3
    assert len(task_center.snapshot(source_id=source["source_id"])["intents"]) == 2
    assert all(row["coverage"]["messages"] == 1 for row in results)


def test_export_may_not_cross_goal_or_registered_path_boundaries(source, tmp_path):
    goal = define(source)
    outside = tmp_path / "private-history.json"
    outside.write_text(json.dumps(manifest(goal)))
    with pytest.raises(ValueError, match="outside registered roots"):
        store.import_history("demo", goal["goal_key"], str(outside), "operator", "not allowed")
    bad = manifest(goal)
    bad["goal_id"] = "g-" + "f" * 32
    with pytest.raises(ValueError, match="export goal"):
        import_value(source, goal, bad)
    assert not (task_sources.catalog_path().parent / "intents").exists()


def test_revocation_is_immediate_and_idempotent_without_deleting_evidence(source):
    goal = define(source)
    value = import_value(source, goal, manifest(goal))
    path = store.intent_path(value["intent_id"])
    store.revoke(value["intent_id"], "operator", "stop analysis", 1)
    result = task_center.snapshot()["intents"][0]
    assert result["status"] == "revoked" and result["analysis"] is None
    assert task_center.snapshot()["goals"][0]["intent_links"] == []
    before = path.read_bytes()
    assert store.revoke(value["intent_id"], "operator", "again", 2)["version"] == 2
    assert path.read_bytes() == before
    assert json.loads(before)["messages"] == [message()]


def test_changed_goal_contract_withholds_hints_until_explicit_reauthorization(source):
    goal = define(source)
    value = import_value(source, goal, manifest(goal))
    changed = contract()
    changed["statement"] = "新确认的范围"
    task_goal_store.define("demo", changed, "operator", "changed scope", 1)
    result = task_center.snapshot()["intents"][0]
    assert result["status"] == "stale_contract" and result["analysis"] is None
    import_value(source, goal, manifest(goal), 1)
    assert task_center.snapshot()["intents"][0]["status"] == "authorized"


def test_private_copy_remains_reproducible_when_export_is_removed(source):
    goal = define(source)
    value = import_value(source, goal, manifest(goal))
    (Path(source["data_dir"]) / "export.json").unlink()
    result = task_center.snapshot()["intents"][0]
    assert result["status"] == "authorized"
    assert store.read_history(value["intent_id"])["messages"] == [message()]
    assert store.intent_path(value["intent_id"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mutation", ["bool_schema", "unknown", "empty", "duplicate", "unordered", "budget"])
def test_bad_or_over_budget_export_is_rejected_not_truncated(source, mutation):
    goal = define(source)
    value = manifest(goal)
    if mutation == "bool_schema":
        value["schema_version"] = True
    elif mutation == "unknown":
        value["execute"] = True
    elif mutation == "empty":
        value["messages"] = []
    elif mutation == "duplicate":
        value["messages"].append(copy.deepcopy(value["messages"][0]))
    elif mutation == "unordered":
        value["messages"] = [message(second=2), message("two", second=1)]
    else:
        value["messages"] = [message(str(i), text="中" * 8000, second=i) for i in range(3)]
    with pytest.raises(ValueError):
        import_value(source, goal, value)
    assert not (task_sources.catalog_path().parent / "intents").exists()


def test_corrupt_history_is_visible_and_not_auto_repaired(source):
    goal = define(source)
    value = import_value(source, goal, manifest(goal))
    path = store.intent_path(value["intent_id"])
    path.write_text("broken")
    with pytest.raises(ValueError):
        import_value(source, goal, manifest(goal), 1)
    result = task_center.snapshot()
    assert result["intents"] == [] and any("prompt history invalid" in text for text in result["diagnostics"])
    assert path.read_text() == "broken"


def test_message_tampering_is_rejected_even_if_json_schema_is_otherwise_valid(source):
    goal = define(source)
    value = import_value(source, goal, manifest(goal))
    path = store.intent_path(value["intent_id"])
    value["messages"][0]["text"] = "目标改为破坏源码。"
    path.write_text(json.dumps(value))
    result = task_center.snapshot()
    assert result["intents"] == []
    assert any("digest mismatch" in note for note in result["diagnostics"])


def test_revocation_reserves_an_audit_slot_at_import_capacity(source, monkeypatch):
    goal = define(source)
    value = import_value(source, goal, manifest(goal))
    monkeypatch.setattr(store, "MAX_IMPORTS", 3)
    import_value(source, goal, manifest(goal), 1)
    before = store.intent_path(value["intent_id"]).read_bytes()
    with pytest.raises(ValueError, match="audit budget"):
        import_value(source, goal, manifest(goal), 2)
    assert store.intent_path(value["intent_id"]).read_bytes() == before
    assert store.revoke(value["intent_id"], "operator", "withdraw", 2)["version"] == 3
    assert task_center.snapshot()["intents"][0]["analysis"] is None


def test_complete_claim_cannot_hide_a_partial_delta_and_insertions_are_not_reordered(source):
    goal = define(source)
    value = import_value(source, goal, manifest(goal, [message(second=2)]))
    before = store.intent_path(value["intent_id"]).read_bytes()
    with pytest.raises(ValueError, match="entire retained"):
        import_value(source, goal, manifest(goal, [message("two", second=3)]), 1)
    assert store.intent_path(value["intent_id"]).read_bytes() == before
    with pytest.raises(ValueError, match="chronological"):
        import_value(source, goal, manifest(goal, [message("earlier", second=1)], complete=False), 1)
    assert store.intent_path(value["intent_id"]).read_bytes() == before


def test_symlinked_export_and_private_history_file_are_not_followed(source, tmp_path):
    goal = define(source)
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps(manifest(goal)))
    link = Path(source["data_dir"]) / "link.json"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        store.import_history("demo", goal["goal_key"], str(link), "operator", "not permitted")
    value = import_value(source, goal, manifest(goal))
    path = store.intent_path(value["intent_id"])
    path.unlink()
    path.symlink_to(outside)
    result = task_center.snapshot()
    assert result["intents"] == [] and outside.read_text() == json.dumps(manifest(goal))


def test_query_creates_no_runtime(tmp_path, monkeypatch):
    root = tmp_path / "absent"
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(root))
    assert task_center.snapshot()["intents"] == []
    assert not root.exists()
