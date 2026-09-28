"""Declarative inputs must be bounded before YAML aliases are constructed."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

from pbatch import cli
from pbatch.config_document import load_config_yaml
from pbatch.pipeline import load_pipeline
from pbatch.task_loading import load_tasks
from pbatch.text_io import parse_yaml_bounded


def merge_bomb() -> str:
    raw, previous = "seed: &seed {prompt: private-input-value}\n", "seed"
    for number in range(23):
        name = f"m{number}"
        raw += f"{name}: &{name} {{<<: [*{previous}, *{previous}]}}\n"
        previous = name
    return raw + f"tasks: [*{previous}]\n"


def _read_source(mode, path):
    if mode == "tasks":
        return load_tasks(str(path))
    if mode == "pipeline":
        return load_pipeline(str(path))
    args = SimpleNamespace(source=str(path), pipeline="", output="", prompt="")
    if mode == "auto":
        cli._auto_detect_pipeline(args)
        assert args.pipeline == ""
    else:
        assert cli._planned_outputs(args) == []


@pytest.mark.parametrize("mode", ["tasks", "pipeline", "auto", "outputs"])
def test_every_yaml_entry_rejects_alias_expansion_before_construction(
        tmp_path, monkeypatch, mode):
    path = tmp_path / "tasks.yaml"
    path.write_text(merge_bomb())
    constructed = []
    original = yaml.SafeLoader.construct_document

    def observed(loader, node):
        constructed.append(node)
        return original(loader, node)

    monkeypatch.setattr(yaml.SafeLoader, "construct_document", observed)
    if mode in {"tasks", "pipeline"}:
        with pytest.raises(SystemExit) as caught:
            _read_source(mode, path)
        assert caught.value.code == 1
    else:
        _read_source(mode, path)
    assert constructed == [], "alias expansion reached SafeLoader construction"


@pytest.mark.parametrize("raw,reason", [
    ("memory: &memory {again: *memory}", "cyclic"),
    ("memory: " + "[" * 100 + "null" + "]" * 100, "depth"),
    ("memory: !!set {a: null}", "unsupported YAML type"),
    ("memory: .nan", "finite"),
    ("memory: {17: value}", "keys must be strings"),
    ("memory: " + "9" * 5000, "numeric literal"),
])
def test_yaml_graph_and_scalar_budgets(raw, reason):
    with pytest.raises(ValueError, match=reason):
        parse_yaml_bounded(raw, yaml, "task source")


def test_expanded_text_budget_applies_even_to_reused_scalar():
    value = "x" * (1024 * 1024)
    raw = f"seed: &text {value}\nvalues: [" + ", ".join(["*text"] * 9) + "]\n"
    with pytest.raises(ValueError, match="expanded byte"):
        parse_yaml_bounded(raw, yaml)


def test_parser_node_budget_stops_composition_before_building_the_whole_tree(monkeypatch):
    from pbatch import config_document
    monkeypatch.setattr(config_document, "MAX_CONFIG_EXPANDED_NODES", 24)
    original = yaml.SafeLoader.compose_node
    visited = []

    def count(loader, parent, index):
        visited.append(index)
        return original(loader, parent, index)

    monkeypatch.setattr(yaml.SafeLoader, "compose_node", count)
    with pytest.raises(ValueError, match="node count"):
        parse_yaml_bounded("[" + ",".join(["{}"] * 200) + "]", yaml)
    assert len(visited) <= 24, "parser built the whole tree before enforcing its node budget"


@pytest.mark.parametrize("raw", [
    "tasks: [private-input-value", "tasks: [!!int private-input-value]",
    "tasks: [!!unknown private-input-value]",
])
@pytest.mark.parametrize("mode", ["tasks", "pipeline", "auto", "outputs"])
def test_yaml_parser_diagnostics_do_not_echo_input(tmp_path, caplog, raw, mode):
    path = tmp_path / "input.yaml"
    path.write_text(raw)
    if mode in {"tasks", "pipeline"}:
        with pytest.raises(SystemExit):
            _read_source(mode, path)
    else:
        _read_source(mode, path)
    assert "private-input-value" not in caplog.text


def test_plain_task_list_with_anchors_and_merges_remains_supported(tmp_path):
    path = tmp_path / "tasks.yaml"
    path.write_text("- &base {prompt: 中文, output: first.md}\n"
                    "- {<<: *base, output: second.md}\n", encoding="utf-8")
    tasks = load_tasks(str(path))
    assert [(task.prompt, task.output) for task in tasks] == [
        ("中文", "first.md"), ("中文", "second.md")]


def test_pipeline_shared_task_template_remains_supported(tmp_path):
    path = tmp_path / "pipeline.yaml"
    path.write_text("""stages:
  - name: first
    from_prompt: plan
    output: first.md
    tasks:
      - &task {prompt: 'review {input}'}
  - name: second
    from_outputs: first
    tasks:
      - {<<: *task, output: second.md}
""")
    pipeline = load_pipeline(str(path))
    assert len(pipeline.stages) == 2
    assert pipeline.stages[1].tasks[0]["prompt"] == "review {input}"


def test_config_still_requires_mapping_and_preserves_empty_defaults():
    assert load_config_yaml("", yaml) == load_config_yaml("null", yaml) == {}
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config_yaml("[task]", yaml)


@pytest.mark.skipif(sys.platform != "linux", reason="bounded Linux resource regression")
@pytest.mark.parametrize("mode", ["auto", "pipeline"])
def test_tiny_merge_bomb_is_rejected_by_real_cli_with_a_memory_ceiling(tmp_path, mode):
    source = tmp_path / "bomb.yaml"
    source.write_text(merge_bomb())
    code = """
import resource, sys
resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
from pbatch.cli import main
sys.argv = ['pi-batch', *sys.argv[1:]]
main()
"""
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("PBATCH_")}
    environment.update(PBATCH_HOME=str(tmp_path / "home"),
                       PBATCH_RUNTIME_HOME=str(tmp_path / "runtime"))
    source_args = [str(source)] if mode == "auto" else ["--pipeline", str(source)]
    result = subprocess.run([sys.executable, "-c", code, *source_args, "--dry-run"],
                            env=environment, capture_output=True, timeout=10)
    assert result.returncode == 1
    assert b"expanded node count" in result.stderr
    assert b"private-input-value" not in result.stderr and b"Traceback" not in result.stderr
    assert len(source.read_bytes()) < 1024
