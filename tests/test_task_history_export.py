"""Private range export/replay: no implicit consent, dropped text or invented coverage."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from test_task_goals import contract, define, source
from test_task_native_history import pi_node, pi_rows, codex_rows, codex_user, write_native
from pbatch import task_center, task_goal_store, task_intent_store as store
from pbatch import task_history_export as exporter, task_history_range as ranges, task_history_stream as stream
from pbatch.task_intent_schema import fingerprint


def export(source, goal, path, format="pi-session", leaf="two", first=1, last=2,
           name="selected.json", channel="window", goal_version=1):
    return exporter.export_history("demo", goal["goal_key"], str(path), format, channel, leaf, first, last,
                                   name, "operator", "explicit export only", goal_version)


def read_export(receipt):
    return json.loads(Path(receipt["path"]).read_bytes())


def importing(source, goal, receipt, origin_file, expected=None):
    return store.import_history("demo", goal["goal_key"], receipt["path"], "operator", "explicit replay consent",
                                expected, origin_file=str(origin_file) if origin_file else "")


@pytest.mark.parametrize("format", ["pi-session", "codex-session"])
def test_private_export_needs_separate_import_and_has_exact_range_provenance(source, format):
    goal = define(source)
    rows = pi_rows(source) if format == "pi-session" else codex_rows(source)
    path = write_native(source, rows)
    before = path.read_bytes()
    receipt = export(source, goal, path, format, "two" if format == "pi-session" else "", 2, 2)
    value = read_export(receipt)
    assert value["schema_version"] == 2 and value["history_complete"] is False
    assert value["origin"]["input_evidence"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert value["origin"]["messages_sha256"] == fingerprint(value["messages"])
    assert value["origin"]["selection"] == {"first_user": 2, "last_user": 2, "branch_user_messages": 2,
                                           "native_user_messages": 3 if format == "pi-session" else 2, "native_records": 5}
    assert len(value["messages"]) == 1 and path.read_bytes() == before
    assert Path(receipt["path"]).stat().st_mode & 0o777 == 0o600
    assert Path(receipt["path"]).parent == Path(source["data_dir"]) / "exports" / "intents"
    assert task_center.snapshot()["intents"] == []
    assert receipt["llm_requests"] == receipt["total_tokens"] == 0
    assert receipt["cost_total"] is None and receipt["grants_import_or_execution"] is False
    with pytest.raises(ValueError, match="explicit --origin-file"):
        importing(source, goal, receipt, None)
    result = importing(source, goal, receipt, path)
    public = task_center.snapshot()["intents"][0]
    assert result["messages"] == value["messages"]
    assert public["origin"] == "explicit_local_native_user_range_import"
    assert public["coverage"]["history_complete"] is False
    assert public["imports"][0]["export_origin"]["replay_basis"] == "explicit_native_file_replayed_at_import_only"
    assert str(path) not in json.dumps(public) and "private-pi-id" not in json.dumps(public)
    assert public["analysis"]["grants_execution"] is False
    assert task_center.snapshot()["goals"][0]["acceptance"] == "pending"


def large_rows(source, format):
    if format == "pi-session":
        rows = pi_rows(source)[:2]
        for index in range(20):
            rows.append(pi_node("tool-" + str(index), rows[-1]["id"], "toolResult", "PRIVATE-TOOL-" + "x" * 150000))
        rows.append(pi_node("two", rows[-1]["id"], text="当前优先可靠性。下一步回归。", second=2))
        return rows
    rows = codex_rows(source)[:2]
    rows.extend({"type": "event_msg", "payload": {"type": "agent_message", "message": "PRIVATE-TOOL-" + "x" * 150000}}
                for _ in range(20))
    return rows + [codex_user("当前优先可靠性。下一步回归。", 2)]


@pytest.mark.parametrize("format", ["pi-session", "codex-session"])
def test_native_files_over_direct_import_budget_are_streamed_without_copying_tool_bodies(source, format):
    goal = define(source)
    path = write_native(source, large_rows(source, format))
    assert path.stat().st_size > 2 * 1024 * 1024
    receipt = export(source, goal, path, format, "two" if format == "pi-session" else "")
    assert Path(receipt["path"]).stat().st_size < 4096
    assert "PRIVATE-TOOL" not in Path(receipt["path"]).read_text()
    result = importing(source, goal, receipt, path)
    assert len(result["messages"]) == 2
    assert result["imports"][0]["export_origin"]["input_evidence"]["bytes"] == path.stat().st_size


def test_long_user_history_requires_explicit_small_range_never_auto_truncates(source):
    goal = define(source)
    rows = pi_rows(source)[:1]
    rows.extend(pi_node(str(n), str(n - 1) if n else None, text=f"实现目标步骤 {n}。", second=1) for n in range(140))
    path = write_native(source, rows)
    with pytest.raises(ValueError, match="message budget"):
        export(source, goal, path, leaf="139", first=1, last=140)
    assert not (Path(source["data_dir"]) / "exports").exists()
    receipt = export(source, goal, path, leaf="139", first=100, last=102)
    value = read_export(receipt)
    assert [m["text"] for m in value["messages"]] == [f"实现目标步骤 {n}。" for n in range(99, 102)]
    assert value["origin"]["selection"]["branch_user_messages"] == 140
    assert value["history_complete"] is False


def test_cli_export_checks_confirmation_and_bounds_before_opening_any_native_file(source, monkeypatch, capsys):
    from pbatch.ops import ops_main
    goal = define(source)
    path = write_native(source, pi_rows(source))
    args = ["intents", "export", "--source", "demo", "--goal", goal["goal_key"], "--goal-version", "1",
            "--file", str(path), "--format", "pi-session", "--channel", "window", "--leaf-id", "two",
            "--first-user", "1", "--last-user", "2", "--output", "chosen.json", "--by", "op", "--reason", "test"]
    original = exporter.scan
    monkeypatch.setattr(exporter, "scan", lambda *a, **kw: pytest.fail("native input opened without consent"))
    assert ops_main(args) == 2
    assert not (Path(source["data_dir"]) / "exports").exists()
    monkeypatch.setattr(exporter, "scan", original)
    assert ops_main([*args, "--confirm"]) == 0
    capsys.readouterr()
    assert task_center.snapshot()["intents"] == []


@pytest.mark.parametrize("first,last", [(0, 1), (-1, 1), (2, 1), (1, 129), (True, 1), (1, 9), (65536, 65536)])
def test_invalid_or_absent_user_range_creates_no_export(source, first, last):
    goal = define(source)
    with pytest.raises(ValueError):
        export(source, goal, write_native(source, pi_rows(source)), first=first, last=last)
    assert not (Path(source["data_dir"]) / "exports").exists()


@pytest.mark.parametrize("name", ["../leak.json", "/tmp/leak.json", "foo/bar.json", "secret.txt", ".hidden.json"])
def test_export_output_cannot_widen_its_private_directory(source, name, monkeypatch):
    goal = define(source)
    monkeypatch.setattr(exporter, "scan", lambda *a, **kw: pytest.fail("invalid output read input"))
    with pytest.raises(ValueError, match="filename"):
        export(source, goal, Path("missing"), name=name)


def test_existing_output_and_capacity_are_never_overwritten_or_cleaned(source, monkeypatch):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    receipt = export(source, goal, path)
    before = Path(receipt["path"]).read_bytes()
    with pytest.raises(FileExistsError):
        export(source, goal, path)
    assert Path(receipt["path"]).read_bytes() == before
    monkeypatch.setattr(exporter, "MAX_EXPORTS", 1)
    with pytest.raises(ValueError, match="capacity"):
        export(source, goal, path, name="another.json")
    assert Path(receipt["path"]).read_bytes() == before
    assert not list(Path(receipt["path"]).parent.glob(".history-export-*"))


def test_selected_multimodal_or_oversize_text_refuses_while_excluded_range_is_explicit(source):
    goal = define(source)
    rows = pi_rows(source)
    rows[1]["message"]["content"].append({"type": "image", "data": "sensitive"})
    path = write_native(source, rows)
    with pytest.raises(ValueError, match="unsupported"):
        export(source, goal, path)
    receipt = export(source, goal, path, first=2, last=2)
    assert len(read_export(receipt)["messages"]) == 1
    rows[3]["message"]["content"][0]["text"] = "x" * (16 * 1024 + 1)
    path = write_native(source, rows)
    with pytest.raises(ValueError, match="complete text"):
        export(source, goal, path, first=2, last=2, name="too-large.json")


@pytest.mark.parametrize("mode", ["bytes", "line", "records", "unfinished", "malformed_tail"])
def test_stream_budgets_and_all_record_integrity_are_fail_closed(source, monkeypatch, mode):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    if mode == "bytes":
        monkeypatch.setattr(stream, "MAX_SCAN_BYTES", 32)
    elif mode == "line":
        monkeypatch.setattr(stream, "MAX_SCAN_LINE_BYTES", 32)
    elif mode == "records":
        monkeypatch.setattr(stream, "MAX_SCAN_RECORDS", 2)
    elif mode == "unfinished":
        path.write_bytes(path.read_bytes()[:-1])
    else:
        path.write_bytes(path.read_bytes() + b'{"type":"other","type":"duplicate"}\n')
    with pytest.raises(ValueError):
        export(source, goal, path)
    assert not (Path(source["data_dir"]) / "exports").exists()


def test_export_rechecks_native_bytes_between_passes(source, monkeypatch):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    original = ranges._collect
    def change(*args):
        path.write_bytes(path.read_bytes().replace(b"private-pi-id", b"private-pj-id"))
        return original(*args)
    monkeypatch.setattr(ranges, "_collect", change)
    with pytest.raises(ValueError, match="between bounded export passes"):
        export(source, goal, path)
    assert not (Path(source["data_dir"]) / "exports").exists()


def test_import_does_not_follow_provenance_paths_without_explicit_origin_consent(source, monkeypatch):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    receipt = export(source, goal, path)
    monkeypatch.setattr(exporter, "scan", lambda *a, **kw: pytest.fail("untrusted provenance selected a path"))
    with pytest.raises(ValueError, match="--origin-file"):
        importing(source, goal, receipt, None)


@pytest.mark.parametrize("mutation", ["text", "counts", "native_digest", "leaf", "source", "contract", "complete", "unknown", "bool_schema"])
def test_tampered_export_cannot_gain_hints_or_replay_authority(source, mutation):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    receipt = export(source, goal, path)
    value = read_export(receipt)
    origin = value["origin"]
    if mutation == "text":
        value["messages"][0]["text"] = "目标改为删除全部。"
        origin["messages_sha256"] = fingerprint(value["messages"])
    elif mutation == "counts":
        origin["selection"]["native_records"] += 1
    elif mutation == "native_digest":
        origin["input_evidence"]["sha256"] = "f" * 64
    elif mutation == "leaf":
        origin["leaf_id"] = "alternate"
    elif mutation == "source":
        origin["source_id"] = "src-" + "f" * 24
    elif mutation == "contract":
        origin["contract_digest"] = "f" * 64
    elif mutation == "complete":
        value["history_complete"] = True
    elif mutation == "unknown":
        origin["execute"] = True
    else:
        value["schema_version"] = True
    Path(receipt["path"]).write_text(json.dumps(value))
    with pytest.raises(ValueError):
        importing(source, goal, receipt, path)
    assert task_center.snapshot()["intents"] == []


def test_native_prefix_rewrite_in_unretained_tool_record_does_not_reset_history(source):
    goal = define(source)
    rows = codex_rows(source)
    path = write_native(source, rows)
    first = export(source, goal, path, "codex-session", "", first=1, last=1)
    value = importing(source, goal, first, path)
    target = store.intent_path(value["intent_id"])
    before = target.read_bytes()
    rows[3]["payload"]["message"] = "x" * len(rows[3]["payload"]["message"].encode("utf-8"))
    path = write_native(source, rows)
    second = export(source, goal, path, "codex-session", "", first=2, last=2, name="second.json")
    with pytest.raises(ValueError, match="previously imported byte prefix"):
        importing(source, goal, second, path, 1)
    assert target.read_bytes() == before


def test_contiguous_range_imports_and_replay_of_exact_original_prefix_after_append(source):
    goal = define(source)
    rows = codex_rows(source)
    path = write_native(source, rows)
    first = export(source, goal, path, "codex-session", "", first=1, last=1)
    value = importing(source, goal, first, path)
    rows.append(codex_user("实现同一目标的下一步。", 3))
    path = write_native(source, rows)
    assert importing(source, goal, first, path, 1)["version"] == 2
    second = export(source, goal, path, "codex-session", "", first=2, last=3, name="second.json")
    current = importing(source, goal, second, path, 2)
    assert len(current["messages"]) == 3 and len(current["imports"][-1]["new_message_ids"]) == 2
    assert store.read_history(current["intent_id"]) == current
    rows.append(codex_user("下一步继续同一目标。", 4))
    path = write_native(source, rows)
    third = export(source, goal, path, "codex-session", "", first=4, last=4, name="third.json")
    final = importing(source, goal, third, path, 3)
    assert len(final["messages"]) == 4 and final["imports"][-1]["new_message_ids"]


def test_window_gap_or_changed_pi_leaf_needs_a_separate_channel(source):
    goal = define(source)
    rows = pi_rows(source)
    rows.append(pi_node("three", "two", second=5))
    path = write_native(source, rows)
    first = export(source, goal, path, leaf="three", last=1)
    value = importing(source, goal, first, path)
    before = store.intent_path(value["intent_id"]).read_bytes()
    gap = export(source, goal, path, leaf="three", first=3, last=3, name="gap.json")
    with pytest.raises(ValueError, match="gap"):
        importing(source, goal, gap, path, 1)
    other = export(source, goal, path, leaf="alternate", first=2, last=2, name="other.json")
    with pytest.raises(ValueError, match="leaf"):
        importing(source, goal, other, path, 1)
    assert store.intent_path(value["intent_id"]).read_bytes() == before


def test_source_and_goal_version_are_checked_before_native_reads_and_before_publish(source, monkeypatch):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    with pytest.raises(ValueError, match="revision conflict"):
        export(source, goal, path, goal_version=0)
    original = exporter.scan
    def revise(*args, **kw):
        result = original(*args, **kw)
        changed = contract()
        changed["statement"] = "重新确认的目标"
        task_goal_store.define("demo", changed, "op", "changed during export", 1)
        return result
    monkeypatch.setattr(exporter, "scan", revise)
    with pytest.raises(ValueError, match="goal changed"):
        export(source, goal, path)
    assert not (Path(source["data_dir"]) / "exports" / "intents" / "selected.json").exists()


def test_corrupt_replay_metadata_is_visible_without_repair_and_revocation_still_preserves_copy(source):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    receipt = export(source, goal, path)
    value = importing(source, goal, receipt, path)
    store.revoke(value["intent_id"], "op", "withdraw", 1)
    path.unlink()
    assert task_center.snapshot()["intents"][0]["analysis"] is None
    assert store.read_history(value["intent_id"])["messages"] == read_export(receipt)["messages"]
    changed = store.read_history(value["intent_id"])
    changed["imports"][0]["export_origin"]["selection"]["first_user"] = 0
    target = store.intent_path(value["intent_id"])
    target.write_text(json.dumps(changed))
    before = target.read_bytes()
    assert task_center.snapshot()["intents"] == []
    assert target.read_bytes() == before


def test_origin_replay_symlinks_fifos_and_outside_roots_are_refused(source, tmp_path):
    goal = define(source)
    path = write_native(source, pi_rows(source))
    receipt = export(source, goal, path)
    outside = tmp_path / "outside.jsonl"
    outside.write_bytes(path.read_bytes())
    link = Path(source["data_dir"]) / "link.jsonl"
    link.symlink_to(outside)
    fifo = Path(source["data_dir"]) / "fifo.jsonl"
    os.mkfifo(fifo)
    for origin, reason in [(outside, "outside registered roots"), (link, "symlink"), (fifo, "regular file")]:
        with pytest.raises(ValueError, match=reason):
            importing(source, goal, receipt, origin)
