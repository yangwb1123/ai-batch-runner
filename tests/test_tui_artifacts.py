"""ArtifactRef projection, safe preview, and headless inspector contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.widgets import Input, Markdown, OptionList, Static

from pbatch import config
from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_artifacts import (
    ARTIFACT_LIMIT,
    ArtifactReference,
    TUIArtifactScreen,
    build_artifact_catalog,
    parse_artifact_args,
    preview_artifact,
)
from pbatch.tui_controller import TUIEventController
from pbatch.tui_inspector import TUIInspectorScreen
from pbatch.tui_journal import RuntimeEventJournal
from pbatch.tui_protocol import RuntimeEvent
from pbatch.tui_shell import SLASH_HELP


def _event(seq: int, kind: str, payload=None, *, run="run_artifact",
           turn="turn_artifact") -> RuntimeEvent:
    return RuntimeEvent(
        event_id=f"evt_artifact_{seq}_{run}", seq=seq,
        occurred_at=f"2026-08-25T12:00:{seq % 60:02d}Z",
        workspace_id="ws_artifact", session_id="ses_artifact",
        run_id=run, turn_id=turn, actor_id="agent_main",
        kind=kind, payload=payload or {})


def _reference(**changes) -> ArtifactReference:
    values = {
        "event_id": "evt_ref", "seq": 1, "run_id": "run",
        "turn_id": "turn", "actor_id": "agent", "label": "artifact",
    }
    values.update(changes)
    return ArtifactReference(**values)


class ArtifactBackend:
    def __init__(self, emit, **options):
        self.emit = emit
        self.cwd = str(Path(options.get("cwd") or ".").resolve())
        self.prompts = []

    def set_emit(self, emit):
        self.emit = emit

    def new_session(self, _name=""):
        return {"session_id": "ses_artifact"}

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        return {"success": True, "stdout": "", "stderr": ""}

    def snapshot(self):
        return {"status": "idle", "session_id": "ses_artifact"}


def test_catalog_uses_exact_current_turn_events_and_is_bounded():
    events = [_event(0, "turn.started")]
    events.append(_event(1, "message.completed", {
        "text": "artifact.created path=forged.txt"}))
    events.append(_event(2, "artifact.created", {"path": "wrong.txt"},
                         run="other"))
    for index in range(ARTIFACT_LIMIT + 6):
        events.append(_event(index + 3, "artifact.created", {
            "path": f"reports/result-{index}.txt", "media_type": "text/plain",
            "size_bytes": index,
        }))

    catalog = build_artifact_catalog(
        events=events, run_id="run_artifact", turn_id="turn_artifact")

    assert len(catalog.references) == ARTIFACT_LIMIT
    assert catalog.references[0].path == "reports/result-6.txt"
    assert catalog.references[-1].declared_size == ARTIFACT_LIMIT + 5
    assert catalog.truncated and not catalog.event_window_truncated
    assert all("forged" not in item.label for item in catalog.references)
    assert all(item.run_id == "run_artifact" for item in catalog.references)


def test_catalog_keeps_malformed_reference_diagnostics_without_coercion():
    event = _event(1, "artifact.created", {
        "path": ["not", "a", "path"], "name": "report",
        "media_type": 7, "size_bytes": -1,
    })

    catalog = build_artifact_catalog(
        events=[event], run_id="run_artifact", turn_id="turn_artifact")

    reference = catalog.references[0]
    assert reference.path == "" and reference.name == "report"
    assert "path is not a string" in reference.reference_error
    assert "media_type is not a string" in reference.reference_error
    assert "size_bytes is not a non-negative integer" in reference.reference_error
    assert catalog.diagnostics
    assert catalog.event_window_truncated


def test_preview_reads_only_bounded_utf8_regular_files(tmp_path):
    report = tmp_path / "report.txt"
    report.write_text("hello 世界\nmore text", encoding="utf-8")
    reference = _reference(label="report.txt", path="report.txt")

    complete = preview_artifact(reference, str(tmp_path))
    bounded = preview_artifact(reference, str(tmp_path), max_bytes=8)

    assert complete.status == "available"
    assert complete.content == "hello 世界\nmore text"
    assert complete.display_path == "report.txt"
    assert bounded.status == "available" and bounded.truncated
    assert bounded.content == "hello "
    assert bounded.size_bytes == report.stat().st_size


def test_preview_blocks_outside_symlink_directory_and_binary(tmp_path):
    outside = tmp_path.parent / "outside-artifact.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    (tmp_path / "linked-dir").symlink_to(tmp_path.parent, target_is_directory=True)
    (tmp_path / "folder").mkdir()
    (tmp_path / "binary.bin").write_bytes(b"abc\0def")

    traversal = preview_artifact(
        _reference(label="outside", path="../outside-artifact.txt"), str(tmp_path))
    absolute = preview_artifact(
        _reference(label="outside", path=str(outside)), str(tmp_path))
    link = preview_artifact(
        _reference(label="link", path="link.txt"), str(tmp_path))
    component = preview_artifact(
        _reference(label="component", path="linked-dir/outside-artifact.txt"),
        str(tmp_path))
    directory = preview_artifact(
        _reference(label="folder", path="folder"), str(tmp_path))
    binary = preview_artifact(
        _reference(label="binary", path="binary.bin"), str(tmp_path))

    assert traversal.status == absolute.status == "blocked"
    assert "outside" in traversal.reason and "outside" in absolute.reason
    assert link.status == component.status == "blocked"
    assert "symlink" in link.reason and "symlink" in component.reason
    assert directory.status == "blocked" and "regular file" in directory.reason
    assert binary.status == "binary" and binary.content == ""


def test_preview_treats_remote_uri_as_metadata_and_local_file_uri_as_scoped(
        tmp_path):
    local = tmp_path / "encoded name.txt"
    local.write_text("local", encoding="utf-8")
    remote = preview_artifact(_reference(
        label="remote", uri="https://example.invalid/report.txt"), str(tmp_path))
    file_uri = preview_artifact(_reference(
        label="local", uri=local.as_uri()), str(tmp_path))
    missing = preview_artifact(_reference(
        label="missing", path="missing.txt"), str(tmp_path))

    assert remote.status == "metadata_only" and "remote fetch is disabled" in remote.reason
    assert file_uri.status == "available" and file_uri.content == "local"
    assert missing.status == "missing"


def test_explicit_data_scope_uses_local_config_root_only(tmp_path, monkeypatch):
    data_root = tmp_path / "private-runtime"
    artifact = data_root / "tui" / "artifacts" / "report.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("private command output", encoding="utf-8")
    monkeypatch.setenv("PBATCH_DATA_DIR", str(data_root))
    reference = _reference(
        label="Command output", scope="data",
        path="tui/artifacts/report.txt")

    preview = preview_artifact(reference, str(tmp_path))
    escaped = preview_artifact(_reference(
        label="outside", scope="data", path="../outside.txt"), str(tmp_path))
    unsupported = preview_artifact(_reference(
        label="unknown", scope="remote", path="report.txt"), str(tmp_path))

    assert preview.status == "available"
    assert preview.content == "private command output"
    assert escaped.status == "blocked" and "outside" in escaped.reason
    assert unsupported.status == "metadata_only"
    assert "not locally authorized" in unsupported.reason


def test_catalog_prefers_explicit_name_and_validates_scope():
    catalog = build_artifact_catalog(events=[
        _event(1, "artifact.created", {
            "path": "hashed/output.txt", "scope": "data",
            "name": "Command output · pytest"}),
        _event(2, "artifact.created", {
            "path": "ignored.txt", "scope": "remote"}),
    ], run_id="run_artifact", turn_id="turn_artifact")

    assert catalog.references[0].label == "Command output · pytest"
    assert catalog.references[0].scope == "data"
    assert catalog.references[1].scope == "invalid"
    assert "unsupported artifact scope" in catalog.references[1].reference_error


def test_artifact_args_are_one_based_and_fail_closed():
    assert parse_artifact_args(()) == 0
    assert parse_artifact_args(("2",)) == 1
    for args in (("0",), ("-1",), ("x",), ("1", "2")):
        try:
            parse_artifact_args(args)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid artifact args: {args}")


def test_artifacts_overlay_previews_refreshes_and_avoids_transcript_noise(
        tmp_path, monkeypatch):
    report = tmp_path / "report.txt"
    report.write_text("version one", encoding="utf-8")
    monkeypatch.setattr(config, "TUI_JOURNAL_ENABLED", False)

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)),
                         backend_factory=ArtifactBackend)
        async with app.run_test(size=(120, 38)) as pilot:
            view = _ingest_artifact_events(app, [
                _event(1, "turn.started"),
                _event(2, "artifact.created", {
                    "path": "report.txt", "media_type": "text/plain"}),
                _event(3, "artifact.created", {
                    "uri": "https://example.invalid/remote.json",
                    "media_type": "application/json"}),
            ])
            app._projection_driven = True
            app._runtime_view = view
            conversation = app.query_one("#conversation", Markdown)
            before = conversation.source
            composer = app.query_one("#composer", Input)
            composer.value = "/artifacts"
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, TUIArtifactScreen)
            options = app.screen.query_one("#artifacts-options", OptionList)
            assert options.option_count == 2
            detail = str(app.screen.query_one("#artifacts-detail", Static).content)
            assert "Preview status: available" in detail and "version one" in detail
            assert conversation.source == before and app._backend.prompts == []

            report.write_text("version two", encoding="utf-8")
            await pilot.press("r")
            await pilot.pause()
            detail = str(app.screen.query_one("#artifacts-detail", Static).content)
            assert "version two" in detail and "version one" not in detail

            await pilot.press("down", "enter")
            await pilot.pause()
            detail = str(app.screen.query_one("#artifacts-detail", Static).content)
            assert "Preview status: metadata_only" in detail
            assert "remote fetch is disabled" in detail
            assert conversation.source == before

    _run(scenario())


def test_completion_opens_artifacts_and_live_event_refreshes_catalog(
        tmp_path, monkeypatch):
    (tmp_path / "first.txt").write_text("first", encoding="utf-8")
    (tmp_path / "second.txt").write_text("second", encoding="utf-8")
    monkeypatch.setattr(config, "TUI_JOURNAL_ENABLED", False)

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path)),
                         backend_factory=ArtifactBackend)
        async with app.run_test(size=(110, 36)) as pilot:
            view = _ingest_artifact_events(app, [
                _event(1, "turn.started"),
                _event(2, "artifact.created", {"path": "first.txt"}),
            ])
            app._projection_driven = True
            app._runtime_view = view
            app._slash_completion(())
            await pilot.pause()
            assert isinstance(app.screen, TUIInspectorScreen)

            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, TUIArtifactScreen)
            assert app.screen.query_one(
                "#artifacts-options", OptionList).option_count == 1

            next_view = app._event_controller.ingest(
                _event(3, "artifact.created", {"path": "second.txt"}))
            app._apply_runtime_snapshot(next_view)
            await pilot.pause()
            assert app.screen.query_one(
                "#artifacts-options", OptionList).option_count == 2

    _run(scenario())


def test_journal_recovery_rebuilds_same_artifact_catalog(tmp_path, monkeypatch):
    data_root = tmp_path / "runtime-data"
    artifact = data_root / "tui" / "artifacts" / "private-output.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("recovered command output", encoding="utf-8")
    monkeypatch.setenv("PBATCH_DATA_DIR", str(data_root))
    journal = RuntimeEventJournal(str(tmp_path), "ses_artifact")
    controller = TUIEventController(journal=journal)
    controller.ingest(_event(1, "turn.started"))
    expected = controller.ingest(_event(2, "artifact.created", {
        "path": "tui/artifacts/private-output.txt", "scope": "data",
        "name": "Command output · pytest", "media_type": "text/plain",
    })).artifacts
    controller.close()

    recovered = TUIEventController(
        journal=RuntimeEventJournal(str(tmp_path), "ses_artifact"))
    try:
        assert recovered.snapshot().artifacts == expected
        reference = recovered.snapshot().artifacts.references[0]
        preview = preview_artifact(reference, str(tmp_path))
        assert preview.status == "available"
        assert preview.content == "recovered command output"
    finally:
        recovered.close()


def test_artifact_help_exposes_read_only_inspector():
    assert "`/artifacts [N]`" in SLASH_HELP
    assert "`/artifact [N]`" in SLASH_HELP


def _ingest_artifact_events(app, events):
    view = None
    for event in events:
        view = app._event_controller.ingest(event)
    assert view is not None
    return view


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
