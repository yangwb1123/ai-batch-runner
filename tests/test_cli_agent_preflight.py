"""Pipeline preflight follows actual executor precedence before side effects."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from pbatch.cli import _pipeline_agents
from pbatch.models import Pipeline, Stage


ROOT = Path(__file__).resolve().parents[1]


def _fixture(tmp_path, stages):
    executable = tmp_path / "available-agent"
    executable.write_text('#!/bin/sh\necho started > "$0.started"\necho accepted\n', encoding="utf-8")
    executable.chmod(0o755)
    pipeline = tmp_path / "pipeline.yaml"
    pipeline.write_text(json.dumps({"stages": stages(str(executable))}), encoding="utf-8")
    settings = tmp_path / "pi-batch.yaml"
    settings.write_text(json.dumps({"config_version": 1,
        "agent": {"bin": "missing-default-agent", "default_model": ""},
        "classifier": {"frontend_pipeline": str(pipeline)}}), encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PBATCH_", "PYTHON", "_PYI_"))}
    env.update(PATH="", PBATCH_PROJECT_CONFIG=str(settings),
               PBATCH_HOME=str(tmp_path / "user"), PBATCH_RUNTIME_HOME=str(tmp_path / "runtime"))
    return executable, pipeline, env


def _run(tmp_path, env, arguments):
    return subprocess.run([sys.executable, str(ROOT / "pi-batch.py"), *arguments,
        "--memory-mode", "off", "--log-file", str(tmp_path / "operation.log")],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=15)


@pytest.mark.parametrize("entry", ["detected", "explicit", "classified"])
def test_pipeline_preflight_uses_routed_stage_and_task_agents(tmp_path, entry):
    executable, pipeline, env = _fixture(tmp_path, lambda agent: [
        {"name": "first", "agent": agent, "from_prompt": "one", "output": "first.md"},
        {"name": "second", "agent": "missing-unused-stage-agent", "from_outputs": "first",
         "tasks": [{"prompt": "two", "agent": agent, "output": "second.md"}]},
    ])
    arguments = {"detected": [str(pipeline)], "explicit": ["--pipeline", str(pipeline)],
                 "classified": ["--classify", "-p", "生成一个登录页面 tsx"]}[entry]
    result = _run(tmp_path, env, arguments)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "first.md").read_text().strip() == "accepted"
    assert (tmp_path / "second.md").read_text().strip() == "accepted"
    assert executable.with_suffix(".started").exists()


@pytest.mark.parametrize("missing", ["stage", "task", "default", "meta", "gate-fix"])
def test_required_pipeline_agent_is_rejected_before_any_stage_runs(tmp_path, missing):
    def stages(agent):
        second = {"name": "second", "from_outputs": "first",
                  "tasks": [{"prompt": "two", "output": "second.md"}]}
        if missing == "stage":
            second["agent"] = "missing-stage-agent"
        elif missing == "task":
            second.update(agent=agent)
            second["tasks"][0]["agent"] = "missing-task-agent"
        elif missing == "meta":
            second.update(meta=True, output_dir="reviews")
            second["tasks"][0]["agent"] = agent
        elif missing == "gate-fix":
            second.update(gate=True, gate_fix_rounds=1)
            second["tasks"][0]["agent"] = agent
        return [{"name": "first", "agent": agent, "from_prompt": "one",
                 "output": "first.md"}, second]
    executable, pipeline, env = _fixture(tmp_path, stages)
    result = _run(tmp_path, env, [str(pipeline)])
    assert result.returncode == 2, result.stderr
    assert "agent preflight failed" in result.stderr
    assert "missing-" in result.stderr
    assert not executable.with_suffix(".started").exists()
    assert not (tmp_path / "operation.log").exists()
    assert not (tmp_path / "runtime").exists()
    assert not (tmp_path / "first.md").exists()


def test_cli_agent_override_is_the_pipeline_fallback(tmp_path):
    executable, pipeline, env = _fixture(tmp_path, lambda _agent: [
        {"name": "first", "from_prompt": "one", "output": "first.md"}])
    result = _run(tmp_path, env, [str(pipeline), "--agent-bin", str(executable)])
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "first.md").read_text().strip() == "accepted"


def test_meta_preflight_checks_stage_executor_and_ignores_task_templates():
    pipeline = Pipeline(stages=[Stage(name="review", meta=True, agent="meta-agent",
        tasks=[{"agent": "unused-agent"}], gate=True, gate_fix_rounds=1)])
    assert set(_pipeline_agents(pipeline, "unused-default")) == {("meta-agent", os.getcwd(), None)}


def test_inline_task_still_rejects_missing_default_before_side_effects(tmp_path):
    _, _, env = _fixture(tmp_path, lambda _agent: [
        {"name": "unused", "from_prompt": "one", "output": "unused.md"}])
    result = _run(tmp_path, env, ["-p", "one", "-o", "result.md"])
    assert result.returncode == 2, result.stderr
    assert "agent preflight failed" in result.stderr
    assert "missing-default-agent" in result.stderr
    assert not (tmp_path / "operation.log").exists()
    assert not (tmp_path / "runtime").exists()


def test_malformed_pipeline_cannot_bypass_schema_with_missing_default(tmp_path):
    executable, pipeline, env = _fixture(tmp_path, lambda agent: [
        {"name": "first", "agent": agent, "from_prompt": "one", "output": "first.md"},
        {"name": "second", "agent": agent, "from_outputs": "unknown-stage"},
    ])
    result = _run(tmp_path, env, [str(pipeline)])
    assert result.returncode == 1, result.stderr
    assert "Invalid pipeline" in result.stderr
    assert not executable.with_suffix(".started").exists()
    assert not (tmp_path / "operation.log").exists()
    assert not (tmp_path / "runtime").exists()


def _context_stage(tmp_path, kind, missing):
    directory = tmp_path / "task workdir"
    directory.mkdir()
    relative = "../missing-agent" if missing else "../available-agent"
    stage = {"name": "context", "agent": relative, "cwd": str(directory),
             "from_prompt": "work", "output": "result.md"}
    output = directory / "result.md"
    if kind.startswith("from-dir"):
        (directory / "input.md").write_text("work", encoding="utf-8")
        stage = {"name": "context", "agent": relative, "from_dir": str(directory),
                 "cwd": str(tmp_path / "unused-stage-cwd"), "aggregate": kind.endswith("aggregate")}
        output = directory / ("aggregate.out.md" if stage["aggregate"] else "input.out.md")
    elif kind != "stage":
        task = {"prompt": "work", "output": "result.md", "agent": relative,
                "cwd": str(directory)}
        if kind in {"path-relative", "path-empty"}:
            task.update(agent="available-agent", env={"PATH": "missing-bin" if missing else
                                                       ".." if kind == "path-relative" else ""})
        if kind in {"path-empty", "task-empty-cwd"}:
            task["cwd"] = ""
            output = tmp_path / "result.md"
            if kind == "task-empty-cwd":
                task["agent"] = "./missing-agent" if missing else "./available-agent"
        stage = {"name": "context", "cwd": str(directory), "from_outputs": "first",
                 "tasks": [task]}
    return stage, output


@pytest.mark.parametrize("kind", ["stage", "task", "task-empty-cwd", "path-relative",
                                  "path-empty", "from-dir", "from-dir-aggregate"])
@pytest.mark.parametrize("missing", [False, True])
def test_pipeline_preflight_matches_child_cwd_and_path(tmp_path, kind, missing):
    stage, output = _context_stage(tmp_path, kind, missing)
    executable, pipeline, env = _fixture(tmp_path, lambda agent: [
        {"name": "first", "agent": agent, "from_prompt": "one", "output": "first.md"}, stage])
    result = _run(tmp_path, env, [str(pipeline)])
    if missing:
        assert result.returncode == 2, result.stderr
        assert "agent preflight failed" in result.stderr
        assert not executable.with_suffix(".started").exists()
        assert not (tmp_path / "operation.log").exists()
        assert not (tmp_path / "runtime").exists()
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert output.read_text().strip() == "accepted"


def test_meta_and_gate_fix_do_not_inherit_task_environment():
    task = {"agent": "worker", "cwd": "/task", "env": {"PATH": "/task-bin"}}
    pipeline = Pipeline(stages=[
        Stage(name="meta", meta=True, agent="planner", cwd="/meta", tasks=[task]),
        Stage(name="gate", from_outputs="meta", agent="fixer", cwd="/fix",
              tasks=[task], gate=True, gate_fix_rounds=1),
    ])
    assert _pipeline_agents(pipeline, "default") == [
        ("planner", "/meta", None), ("worker", "/task", "/task-bin"), ("fixer", "/fix", None)]
