"""Command output is an explicit Gateway publication choice, not an event side channel."""

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from pbatch import hub_cli
from pbatch.hub_gateway_config import HubGatewayConfig, normalize_config
from pbatch.hub_gateway_protocol import project_event_payload
from test_hub_workbench_events import runtime
from test_hub_gateway_observation import options as observation_options


def projected(kind="command.completed", **payload):
    event = runtime(kind, {"item_id": "private-command-id", "command": "inspect", **payload})
    return project_event_payload(event, publish_command_output=True)


@pytest.mark.parametrize("kind", ["command.started", "command.completed", "command.output"])
def test_default_never_publishes_command_output(kind):
    rows = project_event_payload(runtime(kind, {"aggregated_output": "PRIVATE OUTPUT"}))
    assert "PRIVATE" not in repr(rows)
    assert not any("output_text" in row or "output_truncated" in row for row in rows)
    if kind == "command.output":
        assert rows == []


@pytest.mark.parametrize("kind", ["command.completed", "command.output"])
def test_optin_projects_only_explicit_snapshot_fields(kind):
    row, = projected(kind, aggregated_output="build complete\n", exit_code=7,
                     access_token="PRIVATE TOKEN", unknown={"secret": "OTHER PRIVATE"})
    assert row["output_text"] == "build complete\n" and row["output_truncated"] is False
    assert "PRIVATE" not in repr(row) and "private-command-id" not in repr(row)
    assert row["item_id"].startswith("item-")
    assert row["text"] == ("inspect" if kind == "command.completed" else "")
    assert (row.get("exit_code") == 7) is (kind == "command.completed")


@pytest.mark.parametrize("payload", [{}, {"output": "PRIVATE"}, {"delta": "PRIVATE"},
                                      {"text": "PRIVATE"}, {"output": "PRIVATE", "delta": "PRIVATE"}])
def test_delta_and_non_snapshot_outputs_never_publish(payload):
    assert projected("command.output", **payload) == []
    row, = projected(**payload)
    assert "output_text" not in row and "output_truncated" not in row


def test_started_command_cannot_smuggle_output_even_when_enabled():
    row, = projected("command.started", aggregated_output="PRIVATE", exit_code=0)
    assert "PRIVATE" not in repr(row) and "output_text" not in row


def test_present_empty_snapshot_is_distinct_from_missing_and_truncated():
    row, = projected(aggregated_output="")
    assert row["output_text"] == "" and row["output_truncated"] is False
    cut, = projected(aggregated_output="", truncated=True)
    assert cut["output_text"] == "" and cut["output_truncated"] is True


@pytest.mark.parametrize("value", [None, False, 0, [], {}, 1.5])
def test_malformed_snapshot_never_becomes_complete_empty_output(value):
    row, = projected(aggregated_output=value)
    assert row["output_text"] == "" and row["output_truncated"] is True


@pytest.mark.parametrize("value", [None, 1, "false", {}, []])
def test_malformed_upstream_truncation_flag_is_conservative(value):
    row, = projected(aggregated_output="PRIVATE", truncated=value)
    assert row["output_text"] == "" and row["output_truncated"] is True


def test_snapshot_redacts_before_utf8_truncation_and_retains_upstream_cut():
    text = "猫" * 2728 + " password=SECRET-AT-BOUNDARY safe"
    row, = projected(aggregated_output=text)
    assert "SECRET" not in row["output_text"]
    assert len(row["output_text"].encode()) <= 8192 and row["output_truncated"] is True
    row, = projected(aggregated_output="safe", truncated=True)
    assert row["output_text"] == "safe" and row["output_truncated"] is True


@pytest.mark.parametrize("size", [8191, 8192, 8193])
def test_snapshot_exact_utf8_byte_boundary(size):
    row, = projected(aggregated_output="x" * size)
    assert row["output_text"] == "x" * min(size, 8192)
    assert row["output_truncated"] is (size > 8192)


def test_secret_recognition_uses_complete_input_before_shortening_output():
    # Truncating first would expose an incomplete sk- prefix below the shared
    # redactor's minimum recognized token length.
    row, = projected(aggregated_output="x" * 8180 + " " + "sk-" + "A" * 40)
    assert "sk-" not in row["output_text"]
    assert "[REDACTED]" in row["output_text"]


def test_snapshot_without_item_identity_remains_an_explicit_unlinked_record():
    row, = project_event_payload(runtime("command.output", {"aggregated_output": "orphan"}),
                                 publish_command_output=True)
    assert row["output_text"] == "orphan" and "item_id" not in row


def test_snapshot_redacts_complete_and_incomplete_private_key_blocks():
    complete, = projected(aggregated_output="before\n-----BEGIN RSA PRIVATE KEY-----\nSECRET\n"
                          "-----END RSA PRIVATE KEY-----\nafter")
    assert "SECRET" not in complete["output_text"] and "after" in complete["output_text"]
    assert complete["output_truncated"] is False
    partial, = projected(aggregated_output="before\n-----BEGIN PRIVATE KEY-----\nSECRET\nAFTER")
    assert "before" in partial["output_text"] and "SECRET" not in partial["output_text"]
    assert "AFTER" not in partial["output_text"] and partial["output_truncated"] is True


def test_output_text_is_bounded_safe_text_and_keeps_original_event_budget():
    row, = projected(aggregated_output="🧪" * 9000, command="🧪" * 4000)
    assert len(row["output_text"].encode()) <= 8192
    assert row["output_truncated"] is True and len(json.dumps(row).encode()) < 32768
    clean, = projected(aggregated_output="start\x1b[31m\x00\u202esecret\nend")
    assert not any(char in clean["output_text"] for char in ("\x1b", "\x00", "\u202e"))
    assert "\nend" in clean["output_text"]


@pytest.mark.parametrize("value", ["x" * 65537, "bad\ud800text"])
def test_hostile_unconstructable_runtime_snapshot_is_still_fail_closed(value):
    event = SimpleNamespace(kind="command.completed", payload={"aggregated_output": value})
    row, = project_event_payload(event, publish_command_output=True)
    assert row["output_text"] == "" and row["output_truncated"] is True


@pytest.mark.parametrize("value", [0, 1, "true", "false", None, {}, []])
def test_publication_optin_must_be_a_real_boolean(value):
    with pytest.raises(ValueError, match="publish_command_output"):
        normalize_config(HubGatewayConfig("https://hub.example", publish_command_output=value))
    with pytest.raises(ValueError, match="publish_command_output"):
        project_event_payload(runtime("command.completed", {}), publish_command_output=value)


def test_normalized_publication_choice_and_observation_rejection(tmp_path):
    assert normalize_config(HubGatewayConfig("https://hub.example")).publish_command_output is False
    assert normalize_config(HubGatewayConfig("https://hub.example", publish_command_output=True)).publish_command_output is True
    with pytest.raises(ValueError, match="observation"):
        normalize_config(replace(observation_options(tmp_path), publish_command_output=True))


@pytest.mark.parametrize("flag", [[], ["--publish-command-output"]])
def test_gateway_cli_passes_explicit_publication_choice(monkeypatch, flag):
    seen = []
    class Gateway:
        instance_id, generation = "alpha", 1
        def __init__(self, options, **kwargs):
            seen.append(options)
        def run_forever(self):
            pass
        def close(self):
            pass
    monkeypatch.setattr(hub_cli, "HubGateway", Gateway)
    monkeypatch.setattr(hub_cli.HubClient, "from_env", lambda *a, **kw: object())
    assert hub_cli.hub_main(["agent", "--hub", "https://hub.example", *flag]) == 0
    assert seen[0].publish_command_output is bool(flag)


def test_cli_observation_refuses_publication_before_reading_credentials(tmp_path, monkeypatch):
    options = observation_options(tmp_path)
    monkeypatch.setattr(hub_cli.HubClient, "from_env", lambda *a, **kw: pytest.fail("credentials accessed"))
    result = hub_cli.hub_main(["agent", "--observe-only", "--publish-command-output",
        "--hub", options.hub_url, "--cwd", options.cwd, "--state-dir", options.state_dir,
        "--instance-id", options.instance_id, "--project-id", options.project_id])
    assert result != 0
