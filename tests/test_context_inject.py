"""Coordinator-owned project-context injection contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from pbatch.context_inject import add_task_context_paths, inject_task_context
from pbatch.models import Task


def _project(tmp_path: Path, loaded: str = "docs/contract.md") -> None:
    context_dir = tmp_path / "docs" / "agent-context"
    context_dir.mkdir(parents=True)
    context_dir.joinpath("context-map.yaml").write_text(
        "routes:\n"
        "  - when:\n"
        "      task_contains: [payment]\n"
        f"    load: [{loaded}]\n",
        encoding="utf-8")


def test_injects_matched_bounded_fenced_context_once(tmp_path):
    _project(tmp_path)
    contract = tmp_path / "docs" / "contract.md"
    contract.write_text("payment contract", encoding="utf-8")
    task = Task(prompt="implement payment", cwd=str(tmp_path))

    assert inject_task_context(task) is task
    first = task.prompt
    assert '<project-context trust="untrusted-data">' in first
    assert '<evidence source="contract">' in first
    assert "payment contract" in first
    assert "untrusted evidence" in first
    inject_task_context(task)
    assert task.prompt == first


def test_discovers_project_context_from_task_subdirectory(tmp_path):
    _project(tmp_path)
    tmp_path.joinpath(".git").mkdir()
    tmp_path.joinpath("docs", "contract.md").write_text(
        "root payment contract", encoding="utf-8")
    workdir = tmp_path / "src" / "payments"
    workdir.mkdir(parents=True)

    task = Task(prompt="implement payment", cwd=str(workdir))
    inject_task_context(task)

    assert "root payment contract" in task.prompt
    assert '<evidence source="contract">' in task.prompt


def test_literal_context_marker_does_not_bypass_injection(tmp_path):
    _project(tmp_path)
    tmp_path.joinpath("docs", "contract.md").write_text(
        "payment contract", encoding="utf-8")
    marker = '<project-context trust="untrusted-data">'
    task = Task(prompt=f"implement payment; literal example: {marker}",
                cwd=str(tmp_path))

    inject_task_context(task)
    first = task.prompt

    assert "payment contract" in first
    assert first.count(marker) == 2
    inject_task_context(task)
    assert task.prompt == first


def test_adds_safe_project_relative_provenance_from_subdirectory(tmp_path):
    tmp_path.joinpath(".git").mkdir()
    workdir = tmp_path / "src" / "payments"
    workdir.mkdir(parents=True)
    local = workdir / "handler.py"
    absolute = tmp_path / "docs" / "contract.md"
    absolute.parent.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    task = Task(prompt="payment", cwd=str(workdir),
                memory={"paths": ["existing.md"]})

    assert add_task_context_paths(
        task, ["handler.py", absolute, local]) is task

    assert task.memory["paths"] == [
        "existing.md", "src/payments/handler.py", "docs/contract.md"]
    with pytest.raises(ValueError, match="provenance path escapes project root"):
        add_task_context_paths(task, [outside])


def test_no_map_or_no_match_leaves_prompt_unchanged(tmp_path):
    task = Task(prompt="plain", cwd=str(tmp_path))
    inject_task_context(task)
    assert task.prompt == "plain"

    _project(tmp_path)
    other = Task(prompt="unrelated", cwd=str(tmp_path))
    inject_task_context(other)
    assert other.prompt == "unrelated"


@pytest.mark.parametrize("loaded", ["../outside.md", "/tmp/outside.md"])
def test_unsafe_context_map_fails_before_agent(tmp_path, loaded):
    _project(tmp_path, loaded)
    task = Task(prompt="payment", cwd=str(tmp_path))
    with pytest.raises(ValueError, match="context map|project root"):
        inject_task_context(task)


def test_context_map_symlink_cannot_escape_discovered_root(tmp_path):
    tmp_path.joinpath(".git").mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-context"
    outside.mkdir()
    outside.joinpath("context-map.yaml").write_text(
        "routes: []\n", encoding="utf-8")
    tmp_path.joinpath("docs").mkdir()
    tmp_path.joinpath("docs", "agent-context").symlink_to(
        outside, target_is_directory=True)
    workdir = tmp_path / "src"
    workdir.mkdir()

    with pytest.raises(ValueError, match="context map path contains a symlink"):
        inject_task_context(Task(prompt="payment", cwd=str(workdir)))


@pytest.mark.parametrize("kind", ["absolute", "parent", "symlink"])
def test_declared_provenance_paths_cannot_escape_project(tmp_path, kind):
    tmp_path.joinpath(".git").mkdir()
    workdir = tmp_path / "src"
    workdir.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-provenance"
    outside.mkdir()
    outside.joinpath("secret.md").write_text("secret", encoding="utf-8")
    values = {
        "absolute": str(outside / "secret.md"),
        "parent": "../outside.md",
        "symlink": "external/secret.md",
    }
    if kind == "symlink":
        tmp_path.joinpath("external").symlink_to(
            outside, target_is_directory=True)
    task = Task(prompt="plain", cwd=str(workdir),
                memory={"paths": [values[kind]]})

    with pytest.raises(ValueError, match="context provenance path"):
        inject_task_context(task)
