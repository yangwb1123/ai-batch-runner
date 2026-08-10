"""Fail-closed prompt and task-source boundary contracts."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

from pbatch import cli, config
from pbatch.task_loading import load_tasks, task_from_prompt


def _args(**overrides):
    values = {"from_dir": "", "suffix": ".md", "prompt": "",
              "output": "", "source": ""}
    values.update(overrides)
    return SimpleNamespace(**values)


def test_inline_prompt_expands_reference_relative_to_current_directory(
        tmp_path, monkeypatch):
    (tmp_path / "requirement.md").write_text("bounded evidence", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    task = cli._load_batch_tasks(
        _args(prompt="review @requirement.md", output="out.md"), None)[0]

    assert task.prompt == "review bounded evidence"
    assert task.output == "out.md"


def test_stdin_prompt_uses_the_same_reference_expansion(tmp_path, monkeypatch):
    (tmp_path / "requirement.md").write_text("stdin evidence", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("review @requirement.md\n"))

    task = cli._load_batch_tasks(_args(), None)[0]

    assert task.prompt == "review stdin evidence"


def test_plain_text_file_resolves_reference_from_its_directory(tmp_path):
    source_dir = tmp_path / "inputs"
    source_dir.mkdir()
    (source_dir / "evidence.md").write_text("file evidence", encoding="utf-8")
    prompt = source_dir / "prompt.txt"
    prompt.write_text("review @evidence.md", encoding="utf-8")

    task = load_tasks(str(prompt))[0]

    assert task.prompt == "review file evidence"


@pytest.mark.parametrize("name", ["missing.yaml", "missing.yml", "missing.json"])
def test_missing_structured_source_is_not_treated_as_prompt(tmp_path, name):
    with pytest.raises(SystemExit) as exc:
        load_tasks(str(tmp_path / name))
    assert exc.value.code == 1


def test_structured_task_unknown_field_fails_closed(tmp_path):
    source = tmp_path / "tasks.json"
    source.write_text(
        '{"tasks":[{"prompt":"work","ouptut":"typo.md"}]}',
        encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        load_tasks(str(source))
    assert exc.value.code == 1


def test_structured_task_environment_requires_strings(tmp_path):
    source = tmp_path / "tasks.json"
    source.write_text(
        '{"tasks":[{"prompt":"work","env":{"COUNT":2}}]}',
        encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        load_tasks(str(source))
    assert exc.value.code == 1


def test_missing_oversized_and_invalid_utf8_references_fail_closed(
        tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="not found"):
        task_from_prompt("read @missing.md", str(tmp_path))

    large = tmp_path / "large.md"
    large.write_text("12345", encoding="utf-8")
    monkeypatch.setattr(config, "INPUT_MAX_BYTES", 4)
    with pytest.raises(ValueError, match="exceeds"):
        task_from_prompt("read @large.md", str(tmp_path))

    invalid = tmp_path / "invalid.md"
    invalid.write_bytes(b"\xff")
    monkeypatch.setattr(config, "INPUT_MAX_BYTES", 100)
    with pytest.raises(ValueError, match="not valid UTF-8"):
        task_from_prompt("read @invalid.md", str(tmp_path))
