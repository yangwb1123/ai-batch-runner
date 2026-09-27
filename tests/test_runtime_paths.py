"""Runtime artifacts default to the user's private Codex namespace."""

from __future__ import annotations

from pathlib import Path

from pbatch import config
from pbatch.campaign_models import CampaignSettings
from pbatch.user_dirs import project_runtime_dir, pbatch_runtime_home


def test_default_runtime_dir_is_private_and_workspace_scoped(tmp_path, monkeypatch):
    monkeypatch.delenv("PBATCH_DATA_DIR", raising=False)
    monkeypatch.delenv("PBATCH_RUNTIME_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("PBATCH_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config.set_data_dir_override("")

    runtime = config.data_dir(str(tmp_path / "project"))
    assert runtime == project_runtime_dir(str(tmp_path / "project"))
    assert runtime == (tmp_path / "home" / ".codex" / "pbatch" /
                       "projects" / runtime.name)
    assert not runtime.is_relative_to(tmp_path / "project")


def test_runtime_home_and_data_dir_overrides_are_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_HOME", str(tmp_path / "unified-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
    monkeypatch.delenv("PBATCH_DATA_DIR", raising=False)
    config.set_data_dir_override("")
    assert pbatch_runtime_home() == tmp_path / "runtime"
    assert config.data_dir(str(tmp_path / "project")).parent == (
        tmp_path / "runtime" / "projects")

    explicit = tmp_path / "explicit"
    monkeypatch.setenv("PBATCH_DATA_DIR", str(explicit))
    assert config.data_dir(str(tmp_path / "project")) == explicit.resolve()


def test_default_lock_path_is_external(tmp_path, monkeypatch):
    from pbatch.lock import LOCK_NAME, _lock_path

    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    path = _lock_path(str(tmp_path / "project"))
    assert path == tmp_path / "runtime" / "locks" / LOCK_NAME
    assert not path.parent.parent.is_relative_to(tmp_path / "project")


def test_project_memory_defaults_to_private_runtime(tmp_path, monkeypatch):
    from pbatch import project_memory

    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.chdir(project)
    monkeypatch.setenv("PBATCH_DATA_DIR", str(runtime))
    monkeypatch.setattr(project_memory, "MEMORY_DIR",
                        project_memory._LEGACY_MEMORY_DIR)
    created = project_memory.init_memory()
    assert len(created) == 4
    assert all(Path(path).is_relative_to(runtime) for path in created)
    assert not (project / ".ai").exists()


def test_campaign_defaults_are_external_but_explicit_paths_remain_supported(tmp_path):
    settings = CampaignSettings()
    assert settings.output_path(tmp_path) == settings.runtime_path(tmp_path)
    assert settings.state_path(tmp_path) == settings.runtime_path(tmp_path) / "state.jsonl"
    assert settings.summary_path(tmp_path) == settings.runtime_path(tmp_path) / "SUMMARY.md"

    explicit = CampaignSettings(output_dir="reports", state_file="state.jsonl",
                                summary_file="summary.md")
    assert explicit.output_path(tmp_path) == (tmp_path / "reports").resolve()
    assert explicit.state_path(tmp_path) == (tmp_path / "state.jsonl").resolve()
    assert explicit.summary_path(tmp_path) == (tmp_path / "summary.md").resolve()
