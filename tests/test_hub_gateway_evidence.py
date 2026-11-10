"""Typed result evidence stays bounded and cannot infer acceptance from prose."""

import json
from types import SimpleNamespace

import pytest

from pbatch.hub_gateway_protocol import VISIBLE_EVENTS, project_event_payload
from pbatch.tui_command_artifacts import CommandOutputArtifact
from pbatch.tui_native import NativePipelineObserver
from test_hub_workbench_events import runtime
from test_hub_integration import fleet, _until


def projected(kind, payload):
    assert kind in VISIBLE_EVENTS
    rows = project_event_payload(runtime(kind, payload))
    assert len(rows) == 1
    assert len(json.dumps(rows[0], ensure_ascii=True).encode()) < 32768
    return rows[0]


def test_native_validation_projection_retains_explicit_outcome_and_exit():
    started = projected("validation.started", {"item_id": "check", "name": "tests"})
    result = projected("validation.completed", {
        "item_id": "check", "name": "tests", "success": True, "validation_exit": 0})
    assert started["state"] == "running" and result["state"] == "passed"
    assert started["item_id"] == result["item_id"]
    assert result["text"] == "tests" and result["exit_code"] == 0
    assert result["evidence_partial"] is False


def test_actual_native_observer_fields_project_without_inferred_prose(tmp_path):
    events = []
    observer = NativePipelineObserver(events.append, str(tmp_path))
    task = SimpleNamespace(validate="actual validator")
    observer.validation_started("check", task)
    observer.validation_completed("check", task, SimpleNamespace(validation_ok=True, validation_exit=0))
    observer.gate_verdict("review-stage", "PASS", "typed decision")
    rows = [project_event_payload(event)[0] for event in events]
    assert [row["state"] for row in rows] == ["running", "passed", "passed"]
    assert rows[1]["text"] == "actual validator" and rows[1]["exit_code"] == 0
    assert rows[2]["text"] == "review-stage" and rows[2]["reason"] == "typed decision"


def test_actual_command_output_artifact_metadata_preserves_digest_size_and_limits():
    artifact = CommandOutputArtifact("runtime/output.txt", 1024, 2048, "a" * 64, truncated=True)
    row = projected("artifact.created", artifact.event_payload("command", "Actual output metadata"))
    assert row["state"] == "recorded" and row["size_bytes"] == 1024
    assert row["sha256"] == "a" * 64 and row["paths"] == ["runtime/output.txt"]
    assert row["truncated"] is True and row["evidence_partial"] is False


@pytest.mark.parametrize("payload", [
    {}, {"success": "true"}, {"success": 1}, {"success": []},
    {"success": True, "validation_exit": 4}, {"success": False, "exit_code": 0},
    {"success": True, "validation_exit": True}, {"success": True, "exit_code": 2 ** 40},
    {"success": True, "passed": False}, {"success": True, "cancelled": "false"},
    {"success": True, "exit_code": 0, "validation_exit": 3},
    {"exit_code": 0}, {"text": "validation.completed PASS"},
    {"success": True, "truncated": "false"}, {"success": True, "evidence_partial": "false"},
    {"success": True, "evidence_partial": True},
])
def test_missing_invalid_or_conflicting_validation_outcome_is_unknown(payload):
    row = projected("validation.completed", {"name": "tests", **payload})
    assert row["state"] == "unknown" and row["evidence_partial"] is True


def test_validation_failure_and_cancellation_remain_explicit():
    failed = projected("validation.completed", {"name": "tests", "success": False, "validation_exit": 9})
    cancelled = projected("validation.completed", {"name": "tests", "success": True, "validation_exit": 0, "cancelled": True})
    assert failed["state"] == "failed" and failed["exit_code"] == 9
    assert cancelled["state"] == "cancelled"


@pytest.mark.parametrize("value,state", [("PASS", "passed"), ("FAIL", "failed"), ("REJECT", "rejected"),
    (True, "unknown"), (1, "unknown"), ([], "unknown"), ("looks good", "unknown"), ("", "unknown")])
def test_gate_requires_explicit_supported_verdict(value, state):
    row = projected("gate.verdict", {"gate": "review", "verdict": value, "reason": "checked evidence"})
    assert row["state"] == state and row["text"] == "review"
    assert row["evidence_partial"] is (state == "unknown")
    assert row["reason"] == "checked evidence"


def test_artifact_is_metadata_only_and_never_reads_the_declared_path(tmp_path, monkeypatch):
    path = tmp_path / "private.txt"
    path.write_text("PRIVATE FILE CONTENT")
    def refuse_open(*args, **kwargs):
        raise AssertionError("metadata projection must not open files")

    with monkeypatch.context() as patch:
        patch.setattr("builtins.open", refuse_open)
        patch.setattr(type(path), "open", refuse_open)
        row = projected("artifact.created", {"name": "command output", "path": str(path),
            "sha256": "a" * 64, "size_bytes": 20, "media_type": "text/plain", "content": "PRIVATE BODY",
            "url": "https://untrusted.test/file", "uri": "file:///private/file"})
    assert row["state"] == "recorded" and row["paths"] == [str(path)]
    assert row["sha256"] == "a" * 64 and row["size_bytes"] == 20
    assert row["media_type"] == "text/plain" and row["evidence_partial"] is False
    assert "PRIVATE" not in repr(row) and not {"content", "url", "uri"}.intersection(row)


@pytest.mark.parametrize("changes", [{"sha256": "not-a-hash"}, {"sha256": True},
    {"size_bytes": -1}, {"size_bytes": True}, {"size_bytes": 2 ** 53},
    {"media_type": {}}, {"path": []}, {"name": False}, {"item_id": []}])
def test_invalid_artifact_metadata_cannot_be_published_as_usable(changes):
    row = projected("artifact.created", {"name": "output", "path": "out.txt", "sha256": "a" * 64,
        "size_bytes": 10, "media_type": "text/plain", **changes})
    assert row["state"] == "unknown" and row["evidence_partial"] is True


def test_incomplete_artifact_reference_is_honest():
    row = projected("artifact.created", {"name": "output", "path": "out.txt"})
    assert row["state"] == "unknown" and row["evidence_partial"] is True
    assert "sha256" not in row and "size_bytes" not in row


def test_evidence_summaries_redact_and_mark_each_cut():
    row = projected("gate.verdict", {"item_id": "token=PRIVATE-ID", "gate": "name " * 2000,
        "verdict": "PASS", "reason": "token=PRIVATE-REASON " + "猫" * 5000})
    assert "PRIVATE" not in repr(row)
    assert len(row["text"].encode()) <= 256 and len(row["reason"].encode()) <= 1024
    assert row["truncated"] is True


def test_runtime_evidence_payload_budget_is_not_relaxed():
    with pytest.raises(ValueError, match="payload exceeds"):
        runtime("gate.verdict", {"gate": "review", "verdict": "PASS", "reason": "x" * 70000})


def test_gateway_replays_typed_evidence_without_changing_turn_success(fleet):
    user, create, _ = fleet
    gateway, provider, session = create("alpha", hold=True)
    turn = user.send_prompt(session, "Inspect", "typed-evidence")["turn_id"]
    _until([gateway], lambda: bool(provider.prompts))
    provider.emit("validation.started", {"item_id": "check", "name": "tests"})
    provider.emit("validation.completed", {"item_id": "check", "name": "tests", "success": False, "validation_exit": 1})
    provider.emit("gate.verdict", {"gate": "review", "verdict": "REJECT", "reason": "token=SECRET"})
    provider.emit("artifact.created", {"name": "diagnostic", "path": "runtime/diagnostic.txt", "size_bytes": 12, "sha256": "c" * 64})
    _until([gateway], lambda: len(user.session_events(session)) == 6)
    rows = user.session_events(session)
    evidence = {row["kind"]: row["payload"] for row in rows}
    assert evidence["validation.completed"]["state"] == "failed"
    assert evidence["gate.verdict"]["state"] == "rejected"
    assert "SECRET" not in repr(rows)
    assert user.get_turn(turn)["state"] == "running"
    provider.emit("turn.completed")
    _until([gateway], lambda: user.get_turn(turn)["state"] == "completed")
    assert next(row for row in user.session_events(session) if row["kind"] == "gate.verdict")["payload"]["state"] == "rejected"
