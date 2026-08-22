"""Stage-level agent selection: each pipeline stage may run a different
executor (T13). Per-task override, never a global swap — parallel-safe.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pbatch import pipeline as pipeline_mod
from pbatch.models import Stage, Task


def _agent(tmp_path: Path, marker: str, name: str) -> Path:
    path = tmp_path / name
    path.write_text(
        "#!/bin/sh\n"
        f"echo '{marker} $0' > \"${{2:-/dev/null}}\" 2>/dev/null || true\n"
        f"echo '{marker}' $0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_stage_agent_flows_into_task_and_argv(tmp_path):
    agent_a = _agent(tmp_path, "agent-a", "a.sh")
    agent_b = _agent(tmp_path, "agent-b", "b.sh")
    stage = Stage(name="s1", agent=str(agent_b), from_prompt="hi",
                  output=str(tmp_path / "out.md"))
    tasks, _reused, _early = pipeline_mod._build_stage_tasks(
        stage, {}, False, "", 0, "")
    assert len(tasks) == 1
    assert tasks[0].agent == str(agent_b)
    cmd = tasks[0].to_cmd()
    assert cmd[0] == str(agent_b)
    # the global default is untouched (parallel-safe)
    assert Task("x").agent == ""


def test_task_def_agent_override_wins_over_stage(tmp_path):
    upstream = tmp_path / "up.md"
    upstream.write_text("input\n", encoding="utf-8")
    stage = Stage(name="s1", agent="/bin/false", from_outputs="prev",
                  tasks=[{"prompt": "p", "output": "o.md",
                          "agent": "/bin/true"}])
    tasks, _reused, _early = pipeline_mod._build_stage_tasks(
        stage, {"prev": [str(upstream)]}, False, "", 0, "")
    assert tasks[0].agent == "/bin/true"


def test_ledger_records_stage_agent(tmp_path):
    from pbatch import agent_ledger
    import pbatch.config as config_mod
    original = config_mod.AGENT_LEDGER_ENABLED
    config_mod.AGENT_LEDGER_ENABLED = True
    try:
        result = type("R", (), {
            "run_id": "run-1", "session_id": "", "session_name": "",
            "elapsed": 1.5, "returncode": 0,
        })()
        task = Task(prompt="p", agent="/tmp/special-agent",
                    output=str(tmp_path / "r.md"))
        ok = agent_ledger.record_result(result, task, str(tmp_path))
        assert ok
        rows = agent_ledger.query(config_mod.data_dir(str(tmp_path)))
        assert rows and rows[-1]["agent"] == "/tmp/special-agent"
    finally:
        config_mod.AGENT_LEDGER_ENABLED = original


def test_pipeline_schema_allows_stage_and_task_agent():
    from pbatch.pipeline_schema import validate_pipeline
    good = {"stages": [
        {"name": "s1", "agent": "codex", "from_prompt": "x",
         "output": "a.md"},
        {"name": "s2", "from_outputs": "s1",
         "tasks": [{"prompt": "y", "output": "b.md", "agent": "pi"}]},
    ]}
    errors = validate_pipeline(good)
    assert not errors, errors


def test_end_to_end_stages_use_their_own_binaries(tmp_path):
    """CLI-level: two stages with different --agent style executors; each
    stage's artifact carries its agent's marker."""
    a = _agent(tmp_path, "STAGE_A", "agent-a.sh")
    b = _agent(tmp_path, "STAGE_B", "agent-b.sh")
    pipe = tmp_path / "pipe.yaml"
    pipe.write_text(f"""
stages:
  - name: first
    agent: {a}
    from_prompt: "task one"
    output: {tmp_path / 'first.md'}
  - name: second
    agent: {b}
    from_prompt: "task two"
    output: {tmp_path / 'second.md'}
""", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "pi-batch.py"),
         str(pipe), "--memory-mode", "off", "--no-lock"],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-800:]
    first = (tmp_path / "first.md").read_text(encoding="utf-8", errors="replace")
    second = (tmp_path / "second.md").read_text(encoding="utf-8", errors="replace")
    assert "STAGE_A" in first
    assert "STAGE_B" in second