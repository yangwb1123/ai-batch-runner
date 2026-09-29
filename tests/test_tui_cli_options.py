"""Pure CLI option validation for TUI scheduling modes."""

from __future__ import annotations

import pytest

from pbatch.tui_cli_options import build_parser, options_from_args


def test_detached_pipeline_flag_requires_tool_free_pi_controller():
    parser = build_parser()
    options = options_from_args(parser, parser.parse_args([
        "--orchestrator-only", "--persistent-pipelines",
        "--session-name", "controller-a"]))
    assert options.persistent_pipelines is True
    assert options.orchestrator_only is True
    assert options.session_name == "controller-a"

    with pytest.raises(SystemExit) as error:
        options_from_args(parser, parser.parse_args(["--persistent-pipelines"]))
    assert error.value.code == 2


def test_detached_pipeline_cannot_use_native_session_replay():
    parser = build_parser()
    with pytest.raises(SystemExit) as error:
        options_from_args(parser, parser.parse_args([
            "--orchestrator-only", "--persistent-pipelines",
            "--native-session", "session-id"]))
    assert error.value.code == 2
