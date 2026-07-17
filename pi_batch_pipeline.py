"""Multi-stage pipeline engine for pi-batch.py's --pipeline mode.

Split out of pi-batch.py (was 918 lines, over the 500-line code-file gate)
per CLAUDE.md's "先拆分,再继续" rule -- pure extraction, no behavior change.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pi_batch_config as cfg
from pi_batch_task import Task, TaskResult, run_parallel, run_serial

log = cfg.log


# -- Pipeline data structures -------------------------------------------
@dataclass
class Stage:
    """One stage in a pipeline."""
    name: str
    from_dir: str = ""
    from_outputs: str = ""  # name of previous stage
    suffix: str = ".md"
    output_suffix: str = ".out.md"
    mode: str = "serial"
    workers: int = cfg.AGENT_DEFAULT_WORKERS
    tasks: list = field(default_factory=list)
    commands: list = field(default_factory=list)
    commands_parallel: bool = False  # if True, run commands concurrently
    cwd: str = ""
    git_commit: bool = False
    commit_message: str = ""

    def to_dict(self):
        return {
            "name": self.name,
            "from_dir": self.from_dir,
            "from_outputs": self.from_outputs,
            "suffix": self.suffix,
            "output_suffix": self.output_suffix,
            "mode": self.mode,
            "workers": self.workers,
            "tasks": self.tasks,
            "commands": self.commands,
            "commands_parallel": self.commands_parallel,
            "cwd": self.cwd,
            "git_commit": self.git_commit,
            "commit_message": self.commit_message,
        }


@dataclass
class Pipeline:
    """Multi-stage pipeline definition."""
    stages: list[Stage] = field(default_factory=list)

    def to_dict(self):
        return {"stages": [s.to_dict() for s in self.stages]}


def load_pipeline(path: str) -> Pipeline:
    """Load pipeline definition from YAML file."""
    if not cfg.yaml:
        log.error("PyYAML not installed. Run: pip install pyyaml")
        sys.exit(1)

    fpath = Path(path)
    if not fpath.exists():
        log.error("Pipeline file not found: %s", path)
        sys.exit(1)

    raw = fpath.read_text(encoding="utf-8")
    data = cfg.yaml.safe_load(raw)

    if not isinstance(data, dict) or "stages" not in data:
        log.error("Invalid pipeline format. Expected 'stages' key.")
        sys.exit(1)

    # Global settings (applied to all stages that don't set it explicitly)
    global_git_commit = data.get("git_commit", False)

    stages = []
    for s in data["stages"]:
        stage = Stage(
            name=s.get("name", ""),
            from_dir=s.get("from_dir", ""),
            from_outputs=s.get("from_outputs", ""),
            suffix=s.get("suffix", ".md"),
            output_suffix=s.get("output_suffix", ".out.md"),
            mode=s.get("mode", "serial"),
            workers=s.get("workers", cfg.AGENT_DEFAULT_WORKERS),
            tasks=s.get("tasks", []),
            commands=s.get("commands", []),
            commands_parallel=s.get("commands_parallel", False),
            cwd=s.get("cwd", ""),
            git_commit=s.get("git_commit", global_git_commit),
            commit_message=s.get("commit_message", ""),
        )
        stages.append(stage)

    return Pipeline(stages=stages)


def _tasks_from_dir(stage: Stage, stage_outputs: dict[str, list[str]], model_override: str, reuse: bool) -> Optional[list[Task]]:
    """Build tasks for a from_dir stage. Returns None if the directory doesn't exist."""
    dir_path = Path(stage.from_dir)
    if not dir_path.is_dir():
        log.error("Directory not found: %s", stage.from_dir)
        return None

    tasks: list[Task] = []
    for fpath in sorted(dir_path.glob(f"*{stage.suffix}")):
        if fpath.name.endswith(stage.output_suffix):
            continue
        prompt = fpath.read_text(encoding="utf-8")
        out_path = fpath.parent / (fpath.stem + stage.output_suffix)

        # Check if output already exists and reuse flag is set
        if reuse and out_path.exists():
            log.info("REUSE: %s (output exists: %s)", fpath.name, out_path.name)
            stage_outputs.setdefault(stage.name, []).append(str(out_path))
            continue

        task = Task(
            prompt=prompt,
            output=str(out_path),
            cwd=str(fpath.parent),
        )
        if model_override:
            task.model = model_override
        tasks.append(task)

    log.info("Loaded %d tasks from %s", len(tasks), stage.from_dir)
    return tasks


def _task_from_template(task_def: dict, out_path: Path, input_content: str, input_stem: str, model_override: str) -> Optional[Task]:
    """Fill one task template against one previous-stage output file. Returns
    None if the template file itself is missing."""
    prompt_template_path = Path(task_def.get("prompt_template", ""))
    if not prompt_template_path.exists():
        log.error("Prompt template not found: %s", prompt_template_path)
        return None

    template = prompt_template_path.read_text(encoding="utf-8")

    # Replace placeholders
    prompt = template.replace("{input_content}", input_content)
    prompt = prompt.replace("{input_stem}", input_stem)
    prompt = prompt.replace("{input_path}", str(out_path))

    # Resolve output path
    output_template = task_def.get("output", "")
    output_path = output_template.replace("{input_stem}", input_stem)

    task = Task(
        prompt=prompt,
        output=output_path,
        model=task_def.get("model", ""),
        cwd=task_def.get("cwd", ""),
        timeout=task_def.get("timeout", 300),
    )
    if model_override:
        task.model = model_override
    return task


def _tasks_from_outputs(stage: Stage, stage_outputs: dict[str, list[str]], model_override: str) -> Optional[list[Task]]:
    """Build tasks for a from_outputs stage. Returns None if the source stage is missing."""
    if stage.from_outputs not in stage_outputs:
        log.error("Previous stage '%s' not found", stage.from_outputs)
        return None

    prev_outputs = stage_outputs[stage.from_outputs]
    tasks: list[Task] = []

    # For each output file from previous stage, create tasks based on task templates
    for out_path_str in prev_outputs:
        out_path = Path(out_path_str)
        if not out_path.exists():
            log.warning("Output file not found: %s", out_path)
            continue

        input_content = out_path.read_text(encoding="utf-8")
        input_stem = out_path.stem

        for task_def in stage.tasks:
            task = _task_from_template(task_def, out_path, input_content, input_stem, model_override)
            if task is not None:
                tasks.append(task)

    log.info("Loaded %d tasks from %d outputs of stage '%s'",
             len(tasks), len(prev_outputs), stage.from_outputs)
    return tasks


def _run_stage_commands(stage: Stage) -> None:
    """Run a stage's post-task shell commands, serially or concurrently."""
    log.info("")
    log.info("Running %d commands for stage '%s'... (parallel=%s)",
             len(stage.commands), stage.name, stage.commands_parallel)
    cmd_cwd = stage.cwd or os.getcwd()

    def run_single_cmd(cmd: str, index: int) -> bool:
        log.info("CMD [%d/%d]: %s", index, len(stage.commands), cmd)
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=cmd_cwd,
                capture_output=True, text=True, timeout=600
            )
            if proc.returncode == 0:
                log.info("CMD OK (exit=0) [%d/%d]", index, len(stage.commands))
                if proc.stdout:
                    for line in proc.stdout.strip().split("\n")[-10:]:
                        log.info("  | %s", line)
            else:
                log.warning("CMD FAILED (exit=%d) [%d/%d]", proc.returncode, index, len(stage.commands))
                if proc.stderr:
                    for line in proc.stderr.strip().split("\n")[-10:]:
                        log.warning("  | %s", line)
                if proc.stdout:
                    for line in proc.stdout.strip().split("\n")[-5:]:
                        log.info("  | %s", line)
            return proc.returncode == 0
        except Exception as e:
            log.warning("CMD ERROR [%d/%d]: %s", index, len(stage.commands), e)
            return False

    if stage.commands_parallel:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=len(stage.commands)) as pool:
            futs = {pool.submit(run_single_cmd, cmd, i): cmd for i, cmd in enumerate(stage.commands, 1)}
            cmd_results = [f.result() for f in as_completed(futs)]
            all_cmd_ok = all(cmd_results)
    else:
        all_cmd_ok = True
        for i, cmd in enumerate(stage.commands, 1):
            if not run_single_cmd(cmd, i):
                all_cmd_ok = False

    if all_cmd_ok:
        log.info("All %d commands passed for stage '%s'", len(stage.commands), stage.name)
    else:
        log.warning("Some commands failed for stage '%s'", stage.name)


def _git_commit_stage(stage: Stage, outputs: list[str]) -> None:
    """Auto git-commit a stage's output files (best-effort, never raises)."""
    try:
        import subprocess
        commit_msg = stage.commit_message or "[pi-batch] Stage: %s - %d tasks completed" % (stage.name, len(outputs))

        # Check if git repo exists
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            # Add and commit
            subprocess.run(
                ["git", "add"] + outputs,
                capture_output=True, timeout=10
            )
            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                capture_output=True, timeout=10
            )
            log.info("GIT COMMIT: %s (files: %d)", commit_msg, len(outputs))
        else:
            log.warning("Not a git repository, skipping git commit")
    except Exception as e:
        log.warning("Git commit failed: %s", e)


def _build_stage_tasks(stage: Stage, stage_outputs: dict[str, list[str]], model_override: str, reuse: bool) -> Optional[list[Task]]:
    """Dispatch to the right task-builder for this stage's declared source.
    Returns None (having already logged why) for every "nothing to run" case."""
    # Stage type 1: from_dir - read .md files from directory
    if stage.from_dir:
        tasks = _tasks_from_dir(stage, stage_outputs, model_override, reuse)
    # Stage type 2: from_outputs - use outputs from previous stage
    elif stage.from_outputs:
        tasks = _tasks_from_outputs(stage, stage_outputs, model_override)
    else:
        log.error("Stage '%s' must have either 'from_dir' or 'from_outputs'", stage.name)
        return None

    if tasks is None:
        return None
    if not tasks:
        log.warning("No tasks to execute in stage '%s'", stage.name)
        return None
    return tasks


def execute_stage(stage: Stage, stage_outputs: dict[str, list[str]], model_override: str = "", reuse: bool = False) -> list[TaskResult]:
    """Execute one stage and return results.

    Args:
        stage: Stage definition
        stage_outputs: dict mapping stage name -> list of output file paths
        model_override: override model for all tasks
        reuse: if True, skip tasks whose output files already exist
    """
    log.info("")
    log.info("=" * 60)
    log.info("STAGE: %s", stage.name)
    if reuse:
        log.info("(reusing existing outputs if available)")
    log.info("=" * 60)

    tasks = _build_stage_tasks(stage, stage_outputs, model_override, reuse)
    if tasks is None:
        return []

    # Execute tasks
    if stage.mode == "parallel":
        results = run_parallel(tasks, stage.workers)
    else:
        results = run_serial(tasks)

    # Collect output paths
    outputs = [r.task.output for r in results if r.success and r.task.output]
    stage_outputs[stage.name] = outputs

    log.info("")
    log.info("Stage '%s' completed: %d/%d tasks succeeded",
             stage.name, len(outputs), len(tasks))

    if stage.commands:
        _run_stage_commands(stage)

    if stage.git_commit and outputs:
        _git_commit_stage(stage, outputs)

    return results


def run_pipeline(pipeline: Pipeline, model_override: str = "", dry_run: bool = False, reuse: bool = False) -> list[TaskResult]:
    """Execute all stages in a pipeline sequentially.

    Args:
        pipeline: Pipeline definition
        model_override: override model for all tasks
        dry_run: if True, only print task list without executing
        reuse: if True, skip tasks whose output files already exist

    Returns:
        All task results from all stages
    """
    all_results: list[TaskResult] = []
    stage_outputs: dict[str, list[str]] = {}  # stage_name -> [output_file_paths]

    log.info("")
    log.info("=" * 60)
    log.info("PIPELINE START (%d stages)", len(pipeline.stages))
    if reuse:
        log.info("Mode: REUSE existing outputs")
    else:
        log.info("Mode: FORCE regeneration")
    log.info("=" * 60)

    for stage in pipeline.stages:
        if dry_run:
            log.info("")
            log.info("STAGE: %s (dry-run)", stage.name)
            if stage.from_dir:
                log.info("  Will read .md files from: %s", stage.from_dir)
                if reuse:
                    log.info("  Will skip files with existing outputs")
            elif stage.from_outputs:
                log.info("  Will use outputs from stage: %s", stage.from_outputs)
                log.info("  Task templates: %d", len(stage.tasks))
            if stage.commands:
                log.info("  Commands: %d (parallel=%s)", len(stage.commands), stage.commands_parallel)
                for cmd in stage.commands:
                    log.info("    | %s", cmd)
            if stage.git_commit:
                log.info("  Git commit: YES")
            log.info("  Mode: %s", stage.mode)
            continue

        results = execute_stage(stage, stage_outputs, model_override, reuse)
        all_results.extend(results)

    return all_results
