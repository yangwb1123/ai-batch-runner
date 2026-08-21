"""Parallel batch collision preflight and wave-barrier integration tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pbatch import config
from pbatch.models import Task
from pbatch.parallel_plan import (OutputCollisionError, detect_collisions,
                                  plan_waves_for)
from pbatch.runner import run_parallel

ROOT = Path(__file__).resolve().parents[1]
PI_BATCH = ROOT / "pi-batch.py"


@pytest.fixture
def fake_agent(tmp_path, monkeypatch):
    agent = tmp_path / "parallel-agent.sh"
    agent.write_text(
        "#!/bin/sh\n"
        "if [ -n \"$COUNTER\" ]; then echo invoked >> \"$COUNTER\"; fi\n"
        "if [ -n \"$LOG\" ]; then\n"
        "  echo \"start:$LABEL\" >> \"$LOG\"\n"
        "  sleep \"${DELAY:-0}\"\n"
        "  echo \"end:$LABEL\" >> \"$LOG\"\n"
        "fi\n"
        "printf '%s\\n' \"${LABEL:-ok}\"\n", encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setattr(config, "AGENT_BIN", str(agent))
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "data"))
    return agent


def _task(cwd: Path, output: str, label: str = "ok", **env) -> Task:
    values = {"LABEL": label}
    values.update({key: str(value) for key, value in env.items()})
    return Task(prompt=label, cwd=str(cwd), output=output, env=values)


def test_parallel_collision_fails_before_agent_spawn(tmp_path, fake_agent):
    counter = tmp_path / "counter"
    tasks = [
        _task(tmp_path, "shared.md", "first", COUNTER=counter),
        _task(tmp_path, str(tmp_path / "shared.md"), "second", COUNTER=counter),
    ]
    with pytest.raises(OutputCollisionError) as caught:
        run_parallel(tasks, workers=2)
    assert not counter.exists()
    assert not (tmp_path / "shared.md").exists()
    assert "task[0]" in str(caught.value) and "task[1]" in str(caught.value)
    assert str((tmp_path / "shared.md").resolve()) in str(caught.value)
    assert caught.value.groups[0]["indices"] == [0, 1]


def test_parallel_collision_rules_and_stdout_only_tasks(tmp_path, fake_agent):
    root = tmp_path / "root"
    child = root / "child"
    root.mkdir()
    child.mkdir()
    inside = [_task(root, str(root / "root.md"), "root"),
              _task(child, "inside.md", "inside")]
    assert detect_collisions(inside)

    nested = [_task(root / "a", str(root / "dir"), "parent"),
              _task(root / "b", str(root / "dir" / "x.md"), "nested")]
    (root / "a").mkdir()
    (root / "b").mkdir()
    assert detect_collisions(nested)

    clean = [_task(root / "a", str(root / "out-a.md"), "a"),
             _task(root / "b", str(root / "out-b.md"), "b"),
             _task(root / "c", "", "stdout")]
    for directory in (root / "a", root / "b", root / "c"):
        directory.mkdir(exist_ok=True)
    results = run_parallel(clean, workers=2)
    assert all(result.success for result in results)
    assert (root / "out-a.md").read_text(encoding="utf-8").strip() == "a"
    assert (root / "out-b.md").read_text(encoding="utf-8").strip() == "b"


def test_parallel_wave_plan_barrier_preserves_last_writer(tmp_path, fake_agent):
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    cwd_dirs = [tmp_path / f"cwd-{index}" for index in range(3)]
    for directory in cwd_dirs:
        directory.mkdir()
    log_path = tmp_path / "events.log"
    tasks = [
        _task(cwd_dirs[0], str(output_dir / "a.md"), "0", LOG=log_path, DELAY="0.15"),
        _task(cwd_dirs[1], str(output_dir / "b.md"), "1", LOG=log_path, DELAY="0.15"),
        _task(cwd_dirs[2], str(output_dir / "a.md"), "2", LOG=log_path, DELAY="0.05"),
    ]
    plan = plan_waves_for(tasks)
    assert plan == [["0", "1"], ["2"]]
    results = run_parallel(tasks, workers=2, waves=plan)
    assert all(result.success for result in results)
    assert (output_dir / "a.md").read_text(encoding="utf-8").strip() == "2"
    assert (output_dir / "b.md").read_text(encoding="utf-8").strip() == "1"
    events = log_path.read_text(encoding="utf-8").splitlines()
    start_two = events.index("start:2")
    assert start_two > events.index("end:0")
    assert start_two > events.index("end:1")


@pytest.mark.parametrize("plan", [
    [["0", "2"], ["1"]],
    [["0", "1"]],
    [["0", "1"], ["1", "2"]],
])
def test_invalid_wave_plan_fails_before_agent_spawn(tmp_path, fake_agent, plan):
    counter = tmp_path / "counter"
    tasks = [_task(tmp_path / f"cwd-{index}",
                   str(tmp_path / ("o0.md" if index == 2 else f"o{index}.md")),
                   str(index), COUNTER=counter) for index in range(3)]
    for index in range(3):
        (tmp_path / f"cwd-{index}").mkdir()
    with pytest.raises(OutputCollisionError):
        run_parallel(tasks, workers=2, waves=plan)
    assert not counter.exists()


def test_empty_wave_plan_is_noop(tmp_path, fake_agent):
    counter = tmp_path / "counter"
    tasks = [_task(tmp_path, str(tmp_path / "output.md"), COUNTER=counter)]
    assert run_parallel(tasks, waves=[]) == []
    assert not counter.exists()


def test_cli_parallel_collision_exits_two_without_agent(tmp_path, fake_agent):
    counter = tmp_path / "counter"
    shared = tmp_path / "shared.md"
    source = tmp_path / "tasks.yaml"
    source.write_text(
        "tasks:\n"
        f"  - prompt: first\n    output: {shared}\n"
        f"    env:\n      COUNTER: {counter}\n"
        f"  - prompt: second\n    output: {shared}\n"
        f"    env:\n      COUNTER: {counter}\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(PI_BATCH), str(source), "--mode", "parallel",
         "--workers", "2", "--agent-bin", str(fake_agent), "--no-lock"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    assert result.returncode == 2
    assert "PARALLEL REJECTED" in result.stderr
    assert "task[0]" in result.stderr and "task[1]" in result.stderr
    assert not counter.exists()
    assert not shared.exists()
