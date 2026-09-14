"""Workspace transport limits and session binding through CLI, broker and TUI."""

import asyncio
import json
import shlex
from types import SimpleNamespace

import pytest
from textual.widgets import Select

from pbatch.hub_client import HubError
from pbatch.hub_compute_broker import broker_request, turn_tools
from pbatch.hub_compute_cli import parser
from pbatch.hub_compute_tui import ComputeTUI
from pbatch.hub_workspace_cli import run
from pbatch.workspace_bundle import bundle_bytes, bundle_digest
from test_hub_compute_transport import transport  # noqa: F401
from test_hub_compute_tui import _Compute
from test_hub_workspace import bundle, vault  # noqa: F401


def _running(value, session_id, key):
    turn = value.user.send_prompt(session_id, "compute", key)["turn_id"]
    value.hub.append_event("team", "source", 1, {"event_id": key, "session_id": session_id,
                          "turn_id": turn, "kind": "turn.running", "payload": {}})
    return turn


def _capability(prompt):
    args = shlex.split(next(line for line in prompt.splitlines() if "--via" in line))
    return args[args.index("--via") + 1]


def test_large_delegated_upload_default_keys_are_scoped_to_real_session(transport, vault, tmp_path):
    value = transport
    value.compute.connections["team"].archive = vault[0]
    source = tmp_path / "bundle.json"
    content = bundle({f"data-{index}.bin": b"a" * 65536 for index in range(4)})
    source.write_bytes(bundle_bytes(content))
    gateway = SimpleNamespace(options=SimpleNamespace(compute_tools=True), instance_id="source",
                              generation=1, client=value.gateway)
    second = value.gateway.register_session(instance_id="source", generation=1,
                local_session_id="second", project_id="project", name="second", controllable=True)
    snapshots = []
    for index, session_id in enumerate([value.session, second["session_id"]]):
        turn = _running(value, session_id, f"turn-{index}")
        with turn_tools(gateway, SimpleNamespace(hub_id=session_id), turn, "prompt") as prompt:
            path = _capability(prompt)
            args = parser().parse_args(["--via", path, "snapshot-upload", "--bundle-file", str(source)])
            snapshot = run(args)
            assert run(args) == snapshot
            assert snapshot["sha256"] == bundle_digest(content)
            snapshots.append(snapshot["snapshot_id"])
            assert broker_request(path, "snapshots", {}) == [snapshot]
            value.hub.cancel_turn("team", turn, "user")
            with pytest.raises(HubError, match="active running turn"):
                run(args)
    assert snapshots[0] != snapshots[1]


def test_tui_snapshot_choice_binds_output_paths_and_clears_on_session_change():
    async def scenario():
        client = _Compute()
        client.snapshots = lambda session_id, **kwargs: [{
            "snapshot_id": "ws-" + session_id, "file_count": 1, "sha256": "a" * 64}]
        app = ComputeTUI(client)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause(0.1)
            app.query_one("#workspace-snapshot", Select).value = "ws-s1"
            app.query_one("#workspace-paths").value = '["result.txt"]'
            await app._submit_task()
            assert client.submissions[0][1]["workspace"] == {
                "snapshot_id": "ws-s1", "outputs": ["result.txt"]}
            app.query_one("#session", Select).value = "s2"
            await pilot.pause(0.2)
            assert app.query_one("#workspace-snapshot", Select).value == ""

    asyncio.run(scenario())


def test_cli_requires_explicit_files_and_preserves_binary_bytes(tmp_path):
    from pbatch.hub_workspace_cli import _upload_bundle
    data = bytes(range(256))
    (tmp_path / "selected.bin").write_bytes(data)
    (tmp_path / "secret.key").write_text("must not be selected implicitly")
    args = parser().parse_args(["snapshot-upload", "--directory", str(tmp_path)])
    with pytest.raises(ValueError, match="explicit"):
        _upload_bundle(args)
    args.file = ["selected.bin"]
    assert _upload_bundle(args) == bundle({"selected.bin": data})
