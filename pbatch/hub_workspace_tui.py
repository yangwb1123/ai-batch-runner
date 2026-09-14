"""Snapshot controls shared with the compute TUI's existing request lifecycle."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from textual.containers import Horizontal
from textual.widgets import Button, Input, Select

from .hub_workspace import workspace_request
from .hub_workspace_cli import _upload_bundle, run
from .workspace_bundle import bundle_digest


def controls():
    with Horizontal(classes="form-row"):
        yield Select([("Device workspace", "")], value="", id="workspace-snapshot")
        yield Input(value="[]", placeholder='Output files JSON: ["result.json"]', id="workspace-paths")
    with Horizontal(classes="form-row"):
        yield Input(placeholder="Local workspace bundle JSON file", id="workspace-file")
        yield Button("Upload snapshot", id="workspace-upload")


def output_controls():
    with Horizontal(classes="form-row"):
        yield Input(placeholder="New directory for output files", id="workspace-directory")
        yield Button("Restore outputs", id="workspace-download")


def clear(app):
    app._workspace_seq += 1
    selector = app.query_one("#workspace-snapshot", Select)
    selector.set_options([("Device workspace", "")])
    selector.value = ""


async def refresh(app):
    session_id = app._selected_session
    app._workspace_seq += 1
    seq = app._workspace_seq
    if not session_id or not hasattr(app.client, "snapshots"):
        return
    try:
        rows = await asyncio.to_thread(app.client.snapshots, session_id)
    except Exception:
        if seq == app._workspace_seq and session_id == app._selected_session:
            clear(app)
        return
    if seq != app._workspace_seq or session_id != app._selected_session:
        return
    selector = app.query_one("#workspace-snapshot", Select)
    selected = selector.value
    options = [("Device workspace", "")] + [
        (f"{row['snapshot_id']} · {row['file_count']} files · {row['sha256'][:12]}", row["snapshot_id"])
        for row in rows]
    selector.set_options(options)
    selector.value = selected if selected in {value for _, value in options} else ""


def request(app, body):
    import json
    selected = app.query_one("#workspace-snapshot", Select).value
    if selected is Select.BLANK or selected == "":
        return body
    paths = json.loads(app.query_one("#workspace-paths", Input).value)
    if body["workdir"] != ".":
        raise ValueError("Snapshot tasks require workdir '.'")
    body["workspace"] = workspace_request({"snapshot_id": selected, "outputs": paths})
    return body


async def handle(app, action):
    if action not in {"workspace-upload", "workspace-download"}:
        return
    try:
        if action == "workspace-upload":
            await _upload(app)
        else:
            task_id = app._selected_task
            if not task_id:
                raise ValueError("Select a task first")
            args = SimpleNamespace(operation="workspace-output", via="", task_id=task_id,
                                   directory=app.query_one("#workspace-directory", Input).value,
                                   input_sha256="")
            if not args.directory.strip():
                raise ValueError("Specify a new output directory")
            result = await asyncio.to_thread(run, args, app.client)
            app._set_task_status("Outputs restored: " + result["directory"])
    except Exception as exc:
        code = getattr(exc, "code", "")
        app._set_task_status(f"Workspace: {code or type(exc).__name__}")


async def _upload(app):
    session_id = app._selected_session
    if not session_id:
        raise ValueError("Select a session first")
    args = SimpleNamespace(directory=None, file=[],
                           bundle_file=app.query_one("#workspace-file", Input).value)
    bundle = await asyncio.to_thread(_upload_bundle, args)
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, session_id + ":" + bundle_digest(bundle)))
    row = await asyncio.to_thread(app.client.upload_snapshot, session_id, bundle, key)
    if session_id == app._selected_session:
        await refresh(app)
        app.query_one("#workspace-snapshot", Select).value = row["snapshot_id"]
        app._set_task_status("Snapshot ready: " + row["snapshot_id"])
