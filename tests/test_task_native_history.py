"""Explicit native consent, source/branch isolation, immutable prefixes and bounded input."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from test_task_goals import contract, define, source
from test_task_intents import import_value, manifest
from pbatch import task_center, task_goal_store
from pbatch import task_intent_store as store
from pbatch import task_native_history as native


def pi_node(identity, parent, role="user", text="实现目标。必须保留现场。下一步回归。", second=1):
    return {"type": "message", "id": identity, "parentId": parent,
            "timestamp": f"2026-10-05T01:00:{second:02d}Z",
            "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def pi_rows(source):
    return [{"type": "session", "version": 3, "id": "private-pi-id", "cwd": source["workspace"]},
            pi_node("one", None), pi_node("reply", "one", "assistant", "不要保留。下一步删除。", 2),
            pi_node("two", "reply", text="当前优先复现。禁止上传。", second=3),
            pi_node("alternate", "reply", text="目标改为另外一个分支。", second=4)]


def codex_user(text="实现目标。必须保留。", second=1):
    return {"type": "event_msg", "timestamp": f"2026-10-05T01:00:{second:02d}Z",
            "payload": {"type": "user_message", "message": text, "images": [], "local_images": []}}


def codex_rows(source):
    return [{"type": "session_meta", "payload": {"id": "private-codex-id", "cwd": source["workspace"]}},
            codex_user(), {"type": "response_item", "payload": {"type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "DO NOT COPY THIS MIRROR"}]}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "目标改为删除全部。"}},
            codex_user("当前优先复现。下一步增加回归。", 2)]


def write_native(source, rows, name="native.jsonl"):
    path = Path(source["data_dir"]) / name
    path.write_bytes(b"".join(json.dumps(row, ensure_ascii=False).encode() + b"\n" for row in rows))
    return path


def importing(source, goal, path, format="pi-session", leaf="two", expected=None, channel="native"):
    return store.import_history(source["source_id"], goal["goal_key"], str(path), "operator", "explicit consent",
                                expected, format=format, channel=channel, leaf=leaf)


def test_pi_selected_branch_copies_only_users_and_records_exclusions(source):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    before = path.read_bytes()
    value = importing(source, goal, path)
    assert len(value["messages"]) == 2 and all(m["role"] == "user" for m in value["messages"])
    adapter = value["imports"][0]["adapter"]
    assert adapter["selection"] == "pi_branch_users" and adapter["excluded_user_messages"] == 1
    assert adapter["excluded_records"] == 3 and adapter["records"] == 5
    assert value["history_complete"] is False
    assert value["imports"][0]["evidence"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before
    public = task_center.snapshot()["intents"][0]
    assert public["origin"] == "explicit_local_native_user_import"
    assert public["coverage"]["completeness_basis"] == "selected_native_user_text_only_not_full_history"
    assert public["identity"] == "caller_supplied_roles_not_authenticated"
    assert "另外一个分支" not in json.dumps(public, ensure_ascii=False)
    assert "下一步删除" not in json.dumps(public, ensure_ascii=False)
    assert "private-pi-id" not in json.dumps(public)
    assert public["analysis"]["grants_execution"] is False
    assert task_center.snapshot()["goals"][0]["acceptance"] == "pending"


def test_codex_event_users_not_response_mirrors_or_assistant_hints(source):
    goal = define(source)
    value = importing(source, goal, write_native(source, codex_rows(source)), "codex-session", "")
    assert len(value["messages"]) == 2
    assert all(m["id"].startswith("codex-") for m in value["messages"])
    assert "DO NOT COPY" not in json.dumps(value)
    assert "删除全部" not in json.dumps(value, ensure_ascii=False)
    adapter = value["imports"][0]["adapter"]
    assert adapter["selection"] == "codex_user_events" and adapter["excluded_records"] == 3
    assert adapter["leaf_digest"] == "" and value["history_complete"] is False


@pytest.mark.parametrize("format", ["pi-session", "codex-session"])
def test_native_incremental_import_is_whole_file_append_only(source, format):
    goal = define(source)
    rows = pi_rows(source) if format == "pi-session" else codex_rows(source)
    leaf = "two" if format == "pi-session" else ""
    path = write_native(source, rows)
    first = importing(source, goal, path, format, leaf)
    rows.append(pi_node("three", "two", text="下一步只修复同一目标。", second=5) if leaf else codex_user(second=3))
    path = write_native(source, rows)
    value = importing(source, goal, path, format, "three" if leaf else "", 1)
    assert value["version"] == 2 and value["messages"][:2] == first["messages"]
    assert len(value["messages"]) == 3 and len(value["imports"][-1]["new_message_ids"]) == 1
    assert store.read_history(value["intent_id"]) == value
    duplicate = importing(source, goal, path, format, "three" if leaf else "", 2)
    assert duplicate["imports"][-1]["new_message_ids"] == []


@pytest.mark.parametrize("change", ["user", "assistant", "formatting", "removed_prefix"])
def test_rewrites_even_of_unretained_native_records_preserve_private_state(source, change):
    goal = define(source)
    rows = pi_rows(source)
    path = write_native(source, rows)
    value = importing(source, goal, path)
    target = store.intent_path(value["intent_id"])
    before = target.read_bytes()
    if change in {"user", "assistant"}:
        rows[1 if change == "user" else 2]["message"]["content"][0]["text"] = "实现一个重写后的目标。"
        write_native(source, rows)
    elif change == "formatting":
        path.write_bytes(path.read_bytes().replace(b'"type":', b'"type" :'))
    else:
        write_native(source, rows[:3])
    with pytest.raises(ValueError):
        importing(source, goal, path, leaf="reply" if change == "removed_prefix" else "two", expected=1)
    assert target.read_bytes() == before


def test_pi_switching_or_shortening_branch_requires_separate_channel(source):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    value = importing(source, goal, path)
    before = store.intent_path(value["intent_id"]).read_bytes()
    for leaf in ("alternate", "one"):
        with pytest.raises(ValueError, match="continuation"):
            importing(source, goal, path, leaf=leaf, expected=1)
        assert store.intent_path(value["intent_id"]).read_bytes() == before
    alternate = importing(source, goal, path, leaf="alternate", channel="other_branch")
    assert alternate["intent_id"] != value["intent_id"]


@pytest.mark.parametrize("format", ["pi-session", "codex-session"])
def test_native_workspace_mismatch_refuses_no_private_copy(source, tmp_path, format):
    goal = define(source)
    rows = pi_rows(source) if format == "pi-session" else codex_rows(source)
    header = rows[0] if format == "pi-session" else rows[0]["payload"]
    header["cwd"] = str(tmp_path)
    path = write_native(source, rows)
    with pytest.raises(ValueError, match="declared cwd"):
        importing(source, goal, path, format, "two" if format == "pi-session" else "")
    assert not (store.intent_path("ih-" + "a" * 32).parent).exists()


def test_native_options_are_validated_before_file_reads(source, monkeypatch):
    goal = define(source)
    monkeypatch.setattr(store, "_read_bytes", lambda *a: pytest.fail("file was opened"))
    for format, channel, leaf in [("pi-session", "native", ""), ("codex-session", "native", "leaf"),
                                  ("pi-session", "../secret", "one"), ("unknown", "native", ""),
                                  ("normalized", "native", "")]:
        with pytest.raises(ValueError):
            store.import_history("demo", goal["goal_key"], "missing", "op", "consent",
                                 format=format, channel=channel, leaf=leaf)


def test_native_cli_requires_confirmation_and_explicit_selection(source, monkeypatch, capsys):
    from pbatch.ops import ops_main
    goal = define(source)
    path = write_native(source, pi_rows(source))
    args = ["intents", "import", "--source", "demo", "--goal", goal["goal_key"], "--file", str(path),
            "--format", "pi-session", "--channel", "pi_primary", "--by", "op", "--reason", "test"]
    original = store._read_bytes
    monkeypatch.setattr(store, "_read_bytes", lambda *a: pytest.fail("file opened without consent/selection"))
    assert ops_main(args) == 2
    assert ops_main([*args, "--confirm"]) == 2
    monkeypatch.setattr(store, "_read_bytes", original)
    assert ops_main([*args, "--confirm", "--leaf-id", "two"]) == 0
    capsys.readouterr()
    assert task_center.snapshot()["intents"][0]["channel"] == "pi_primary"


@pytest.mark.parametrize("change", ["unknown_version", "bool_version", "dangling", "duplicate", "cycle", "missing_id", "no_users", "timestamps"])
def test_pi_ambiguous_or_invalid_tree_is_not_repaired(source, change):
    goal = define(source)
    rows = pi_rows(source)
    leaf = "two"
    if change in {"unknown_version", "bool_version"}:
        rows[0]["version"] = 4 if change == "unknown_version" else True
    elif change == "dangling":
        rows[3]["parentId"] = "not-present"
    elif change == "duplicate":
        rows[3]["id"] = "one"
    elif change == "cycle":
        rows[1]["parentId"] = "two"
    elif change == "missing_id":
        del rows[0]["id"]
    elif change == "no_users":
        rows[1]["message"]["role"] = "assistant"
        rows[3]["message"]["role"] = "assistant"
    else:
        rows[3]["timestamp"] = "2026-10-05T00:00:00Z"
    with pytest.raises(ValueError):
        importing(source, goal, write_native(source, rows), leaf=leaf)


@pytest.mark.parametrize("format", ["pi-session", "codex-session"])
def test_native_user_multimodal_input_refuses_instead_of_losing_evidence(source, format):
    goal = define(source)
    rows = pi_rows(source) if format == "pi-session" else codex_rows(source)
    if format == "pi-session":
        rows[1]["message"]["content"].append({"type": "image", "data": "secret"})
    else:
        rows[1]["payload"]["images"] = ["private-photo"]
    with pytest.raises(ValueError, match="unsupported"):
        importing(source, goal, write_native(source, rows), format, "two" if format == "pi-session" else "")


@pytest.mark.parametrize("format", ["pi-session", "codex-session"])
def test_unknown_native_user_fields_are_not_silently_discarded(source, format):
    goal = define(source)
    rows = pi_rows(source) if format == "pi-session" else codex_rows(source)
    user = rows[1]["message"] if format == "pi-session" else rows[1]["payload"]
    user["future_images"] = ["private-attachment"]
    with pytest.raises(ValueError, match="unsupported"):
        importing(source, goal, write_native(source, rows), format, "two" if format == "pi-session" else "")


def test_pi_content_parts_do_not_drop_unrecognized_user_annotations(source):
    goal = define(source)
    rows = pi_rows(source)
    rows[1]["message"]["content"][0]["annotation"] = "hidden instruction"
    with pytest.raises(ValueError, match="unsupported"):
        importing(source, goal, write_native(source, rows))


@pytest.mark.parametrize("raw", [b'{}', b'{}\n', b'[]\n', b'{"type":"session","type":"other"}\n',
                                  b'{"type":"session","x":NaN}\n', b'\xff\n', b'\n', b'[' * 2000 + b'0' + b']' * 2000 + b'\n'])
def test_native_malformed_records_are_not_salvaged(raw):
    with pytest.raises(ValueError):
        native.records(raw)


@pytest.mark.parametrize("budget", ["bytes", "line", "records", "text", "messages"])
def test_native_budgets_refuse_without_truncation(source, monkeypatch, budget):
    goal = define(source)
    rows = pi_rows(source)
    if budget == "bytes":
        monkeypatch.setattr(native, "MAX_NATIVE_BYTES", 40)
    elif budget == "line":
        monkeypatch.setattr(native, "MAX_NATIVE_LINE_BYTES", 40)
    elif budget == "records":
        monkeypatch.setattr(native, "MAX_NATIVE_RECORDS", 3)
    elif budget == "text":
        rows[1]["message"]["content"][0]["text"] = "x" * (16 * 1024 + 1)
    else:
        rows = rows[:1] + [pi_node(str(n), str(n - 1) if n else None, second=1) for n in range(129)]
    with pytest.raises(ValueError):
        importing(source, goal, write_native(source, rows), leaf="128" if budget == "messages" else "two")


def test_native_proof_rejects_file_change_between_authorization_and_read(source, monkeypatch):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    original = store.evidence_reference
    def race(*args):
        reference = original(*args)
        path.write_bytes(path.read_bytes().replace(b"private-pi-id", b"private-pj-id"))
        return reference
    monkeypatch.setattr(store, "evidence_reference", race)
    with pytest.raises(ValueError, match="changed during authorized import"):
        importing(source, goal, path)


def test_native_symlinks_fifos_and_outside_roots_are_not_read(source, tmp_path):
    goal = define(source)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(write_native(source, pi_rows(source)).read_bytes())
    with pytest.raises(ValueError, match="outside registered roots"):
        importing(source, goal, outside)
    link = Path(source["data_dir"]) / "link.jsonl"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        importing(source, goal, link)
    fifo = Path(source["data_dir"]) / "fifo.jsonl"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular file"):
        importing(source, goal, fifo)


def test_native_and_normalized_formats_cannot_mix_inside_one_channel(source):
    goal = define(source)
    value = import_value(source, goal, manifest(goal, channel="native"))
    before = store.intent_path(value["intent_id"]).read_bytes()
    with pytest.raises(ValueError, match="format cannot change"):
        importing(source, goal, write_native(source, pi_rows(source)), expected=1)
    assert store.intent_path(value["intent_id"]).read_bytes() == before


def test_native_revocation_and_contract_revision_keep_evidence_without_authority(source):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    value = importing(source, goal, path)
    revised = contract()
    revised["statement"] = "新确认的同一项目目标"
    task_goal_store.define("demo", revised, "operator", "revised", 1)
    public = task_center.snapshot()["intents"][0]
    assert public["status"] == "stale_contract" and public["analysis"] is None
    importing(source, goal, path, expected=1)
    store.revoke(value["intent_id"], "operator", "withdraw", 2)
    path.unlink()
    public = task_center.snapshot()["intents"][0]
    assert public["status"] == "revoked" and public["analysis"] is None
    assert store.read_history(value["intent_id"])["messages"] == value["messages"]
    assert public["accounting"]["import_operations"] == 2


@pytest.mark.parametrize("change", ["unknown", "version", "counts", "digest", "complete", "cross_session"])
def test_invalid_native_audit_metadata_is_diagnosed_not_repaired(source, change):
    goal = define(source)
    value = importing(source, goal, write_native(source, pi_rows(source)))
    changed = copy.deepcopy(value)
    adapter = changed["imports"][0]["adapter"]
    if change == "unknown":
        adapter["grant_execution"] = True
    elif change == "version":
        adapter["schema_version"] = True
    elif change == "counts":
        adapter["user_messages"] = 3
    elif change == "digest":
        adapter["session_digest"] = "raw-id"
    elif change == "complete":
        changed["history_complete"] = True
    else:
        importing(source, goal, write_native(source, pi_rows(source)), expected=1)
        changed = store.read_history(value["intent_id"])
        changed["imports"][-1]["adapter"]["session_digest"] = "f" * 64
    target = store.intent_path(value["intent_id"])
    target.write_text(json.dumps(changed))
    before = target.read_bytes()
    view = task_center.snapshot()
    assert view["intents"] == [] and any("prompt history invalid" in note for note in view["diagnostics"])
    assert target.read_bytes() == before
