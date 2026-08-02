"""Pipeline execution: stage task builders, meta role orchestration,
verdict gates, decision logs, archiving, and task-source loading."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from .config import AGENT_DEFAULT_TIMEOUT, AGENT_DEFAULT_WORKERS, log, yaml
from .models import Pipeline, Stage, Task
from .meta import combine_outputs, run_meta_stage
from .reuse import fingerprint as reuse_fp
from .reuse import reuse_decision, write_sidecar
from .runner import _save_validated, print_summary, run_parallel, run_serial, run_task

def load_pipeline(path: str) -> Pipeline:
    """Load pipeline definition from YAML file."""
    if not yaml:
        log.error("PyYAML not installed. Install the project: uv sync (or pip install pyyaml)")
        sys.exit(1)
    
    fpath = Path(path)
    if not fpath.exists():
        log.error("Pipeline file not found: %s", path)
        sys.exit(1)
    
    raw = fpath.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    
    if not isinstance(data, dict) or "stages" not in data:
        log.error("Invalid pipeline format. Expected 'stages' key.")
        sys.exit(1)
    
    # Global settings (applied to all stages that don't set it explicitly)
    global_git_commit = data.get("git_commit", False)

    stages = [_parse_stage_def(s, global_git_commit) for s in data["stages"]]
    
    return Pipeline(stages=stages, decision_log=data.get("decision_log", ""),
                    archive_dir=data.get("archive_dir", ""), name=Path(path).stem)


def _parse_stage_def(s: dict, global_git_commit: bool) -> Stage:
    """Map one YAML stage definition onto a Stage (untrusted keys ignored;
    unknown keys simply do not exist on the dataclass)."""
    return Stage(
        name=s.get("name", ""),
        model=s.get("model", ""),
        provider=s.get("provider", ""),
        from_dir=s.get("from_dir", ""),
        from_outputs=s.get("from_outputs", ""),
        suffix=s.get("suffix", ".md"),
        output_suffix=s.get("output_suffix", ".out.md"),
        mode=s.get("mode", "serial"),
        workers=s.get("workers", AGENT_DEFAULT_WORKERS),
        aggregate=s.get("aggregate", False),
        validate_cmd=s.get("validate_cmd"),
        meta=s.get("meta", False),
        meta_prompt=s.get("meta_prompt", ""),
        role_dir=s.get("role_dir", ""),
        output_dir=s.get("output_dir", ""),
        max_iterations=s.get("max_iterations", 3),
        gate=s.get("gate", False),
        approval=s.get("approval", False),
        from_prompt=s.get("from_prompt", ""),
        output=s.get("output", ""),
        tasks=s.get("tasks", []),
        commands=s.get("commands", []),
        commands_parallel=s.get("commands_parallel", False),
        cwd=s.get("cwd", ""),
        git_commit=s.get("git_commit", global_git_commit),
        commit_message=s.get("commit_message", ""),
    )


def _task_prompt(task_def: dict, input_content: str, input_stem: str, input_path: str) -> str:
    """Build the prompt for a task definition: a literal 'prompt' string or
    a 'prompt_template' file, both with {input_content}/{input_stem}/{input_path}
    placeholders. Returns '' when neither is present (caller skips it)."""
    prompt = task_def.get("prompt", "")
    prompt_template_path = Path(task_def.get("prompt_template", ""))
    if task_def.get("prompt_template") and prompt_template_path.exists():
        prompt = prompt_template_path.read_text(encoding="utf-8")
    if not prompt:
        log.error("Task in stage needs 'prompt' or an existing 'prompt_template' file")
        return ""
    return (prompt.replace("{input_content}", input_content)
                 .replace("{input_stem}", input_stem)
                 .replace("{input_path}", input_path))


def _expected_fp(prompt: str, out: str, validate_cmd: str, model: str, provider: str, fp_mode: bool) -> Optional[str]:
    """T9: fingerprint for a reuse candidate when --reuse-fingerprint is on
    (None otherwise); a mismatch regenerates the artifact. model/provider
    are the TASK's EFFECTIVE values (overrides included) — hashing the raw
    stage values instead made the fingerprint never match the sidecar that
    records the executed task's values (round-4 finding M6)."""
    if not fp_mode:
        return None
    return reuse_fp(prompt, out, validate_cmd or "", model, provider)


def _task_from_def(task_def: dict, prompt: str, output_path: str, model_override: str = "", timeout_override: int = 0) -> Task:
    """Build a task from a pipeline template definition, applying CLI-level
    model and timeout overrides on top of per-task values."""
    task = Task(
        prompt=prompt,
        output=output_path,
        model=task_def.get("model", ""),
        provider=task_def.get("provider", ""),
        thinking=task_def.get("thinking", ""),
        tools=task_def.get("tools", ""),
        exclude_tools=task_def.get("exclude_tools", ""),
        cwd=task_def.get("cwd", ""),
        timeout=task_def.get("timeout", AGENT_DEFAULT_TIMEOUT),
    )
    if model_override:
        task.model = model_override
    if timeout_override:
        task.timeout = timeout_override
    return task


def _aggregate_tasks(stage: Stage, prev_outputs: list, model_override: str = "", timeout_override: int = 0, reuse: bool = False,
                     validate_cmd: str = "", reuse_legacy: bool = False,
                     fingerprint_mode: bool = False) -> tuple[list[Task], list[str]]:
    """Combine every upstream artifact into one prompt per task template so
    downstream roles see all evidence (input_stem becomes 'combined') instead
    of fanning each artifact into an independent task.

    Returns (new_tasks, reused_output_paths). With reuse=True, a template
    whose combined output file already exists is skipped and its path is
    returned as reused so downstream stages still see it.
    """
    combined = combine_outputs(prev_outputs)
    if not combined:
        log.warning("No upstream outputs available for aggregate stage '%s'", stage.name)
        return [], []

    tasks = []
    reused = []
    for task_def in stage.tasks:
        prompt = _task_prompt(task_def, combined, "combined", ", ".join(prev_outputs))
        if not prompt:
            continue
        output_path = task_def.get("output", "").replace("{input_stem}", "combined")
        task = _task_from_def(task_def, prompt, output_path, model_override, timeout_override)
        # T9: fingerprint the task's EFFECTIVE inputs (resolved output path
        # + task-level model/provider) so the sidecar written after success
        # matches what the reuse check hashed (round-4 finding M6/P1-3).
        out_resolved = str(task.output_path()) if task.output else ""
        fp = _expected_fp(prompt, out_resolved, validate_cmd, task.model, task.provider, fingerprint_mode)
        if reuse and task.output and reuse_decision(out_resolved, validate_cmd, legacy=reuse_legacy, expected_fp=fp):
            log.info("REUSE: %s (reused+validated)", output_path)
            reused.append(output_path)
            continue
        tasks.append(task)
    return tasks, reused




def _stage_from_prompt_tasks(stage: Stage, reuse: bool, model_override: str, timeout_override: int,
                             validate_cmd: str = "", reuse_legacy: bool = False,
                             fingerprint_mode: bool = False) -> tuple[list[Task], list[str]]:
    """One-sentence starting point: a single task whose output feeds
    downstream from_outputs stages (no input file needed)."""
    if not stage.output:
        log.error("Stage '%s': from_prompt requires output", stage.name)
        return [], []
    task = Task(prompt=stage.from_prompt, output=stage.output)
    if model_override:
        task.model = model_override
    if timeout_override:
        task.timeout = timeout_override
    # T9 (round-4 M6): hash the task's effective inputs so the sidecar
    # written after success matches the reuse-check fingerprint.
    out_resolved = str(task.output_path()) if task.output else ""
    fp = _expected_fp(stage.from_prompt, out_resolved, validate_cmd, task.model, task.provider, fingerprint_mode)
    if reuse and reuse_decision(out_resolved, validate_cmd, legacy=reuse_legacy, expected_fp=fp):
        log.info("REUSE: %s (reused+validated)", stage.output)
        return [], [stage.output]
    log.info("Loaded 1 task from from_prompt for stage '%s'", stage.name)
    return [task], []


def _stage_from_dir_tasks(stage: Stage, reuse: bool, model_override: str, timeout_override: int,
                          validate_cmd: str = "", reuse_legacy: bool = False,
                          fingerprint_mode: bool = False) -> tuple[list[Task], list[str]]:
    """Read .md files from a directory; one task per file, output next to
    the input with the stage's output suffix."""
    dir_path = Path(stage.from_dir)
    if not dir_path.is_dir():
        log.error("Directory not found: %s", stage.from_dir)
        return [], []
    tasks: list[Task] = []
    reused: list[str] = []
    for fpath in sorted(dir_path.glob(f"*{stage.suffix}")):
        if fpath.name.endswith(stage.output_suffix):
            continue
        # Local fix (self-iteration round 2, F1): resolve the output path
        # absolutely here. The task's cwd is the input dir, so a relative
        # output would be double-prefixed by output_path()'s cwd-aware
        # resolution (docs/inputs/docs/inputs/...).
        out_path = (fpath.parent / (fpath.stem + stage.output_suffix)).resolve()
        prompt = fpath.read_text(encoding="utf-8")
        task = Task(prompt=prompt, output=str(out_path), cwd=str(fpath.parent))
        task.model = stage.model or task.model
        task.provider = stage.provider or task.provider
        if model_override:
            task.model = model_override
        if timeout_override:
            task.timeout = timeout_override
        fp = _expected_fp(prompt, str(out_path), validate_cmd, task.model, task.provider, fingerprint_mode)
        if reuse and reuse_decision(str(out_path), validate_cmd, str(fpath.parent), reuse_legacy, fp):
            log.info("REUSE: %s (reused+validated)", out_path.name)
            reused.append(str(out_path))
            continue
        tasks.append(task)
    log.info("Loaded %d tasks from %s", len(tasks), stage.from_dir)
    return tasks, reused


def _stage_from_outputs_tasks(stage: Stage, stage_outputs: dict, reuse: bool, model_override: str, timeout_override: int,
                              validate_cmd: str = "", reuse_legacy: bool = False,
                              fingerprint_mode: bool = False) -> tuple[list[Task], list[str]]:
    """Tasks fed by the previous stage's artifacts: one combined task per
    template (aggregate) or one task per artifact per template."""
    if stage.from_outputs not in stage_outputs:
        log.error("Previous stage '%s' not found", stage.from_outputs)
        return [], []
    prev_outputs = stage_outputs[stage.from_outputs]
    tasks: list[Task] = []
    reused: list[str] = []
    if stage.aggregate:
        # Merge every upstream artifact into one combined prompt per
        # template so downstream roles see all evidence, instead of
        # fanning each artifact into independent (and conflicting) tasks.
        agg_tasks, agg_reused = _aggregate_tasks(stage, prev_outputs, model_override, timeout_override, reuse,
                                                 validate_cmd, reuse_legacy, fingerprint_mode)
        return agg_tasks, agg_reused
    for out_path_str in prev_outputs:
        out_path = Path(out_path_str)
        if not out_path.exists():
            log.warning("Output file not found: %s", out_path)
            continue
        input_content = out_path.read_text(encoding="utf-8")
        input_stem = out_path.stem
        # Create tasks from templates (prompt string or template file,
        # both with {input_content}/{input_stem} placeholders)
        for task_def in stage.tasks:
            prompt = _task_prompt(task_def, input_content, input_stem, str(out_path))
            if not prompt:
                continue
            output_path = task_def.get("output", "").replace("{input_stem}", input_stem)
            task = _task_from_def(task_def, prompt, output_path, model_override, timeout_override)
            # T9 (round-4 M6): fingerprint the task's effective inputs so
            # the sidecar written after success matches this hash.
            out_resolved = str(task.output_path()) if task.output else ""
            fp = _expected_fp(prompt, out_resolved, validate_cmd, task.model, task.provider, fingerprint_mode)
            if reuse and task.output and reuse_decision(out_resolved, validate_cmd, legacy=reuse_legacy, expected_fp=fp):
                log.info("REUSE: %s (reused+validated)", output_path)
                reused.append(output_path)
                continue
            tasks.append(task)
    log.info("Loaded %d tasks from %d outputs of stage '%s'",
             len(tasks), len(prev_outputs), stage.from_outputs)
    return tasks, reused


def execute_stage(stage: Stage, stage_outputs: dict[str, list[str]], model_override: str = "", reuse: bool = False, timeout_override: int = 0,
                  session_mode: str = "new", session_name: str = "", validate_cmd: str = "",
                  reuse_legacy: bool = False, fingerprint_mode: bool = False,
                  approval_file: str = "") -> tuple[list[TaskResult], bool]:
    """Execute one stage and return (task_results, stage_ok). stage_ok is
    False when any task or configured post-stage command failed, so command
    hooks act as a failure gate for the pipeline."""
    _log_stage_header(stage, reuse)
    
    # Per-stage engineering gate: the stage's own validate_cmd wins over the
    # CLI default ("" disables validation for this stage), and per-task
    # validate fields override both inside run_serial/run_parallel.
    stage_validate = stage.validate_cmd if stage.validate_cmd is not None else validate_cmd

    tasks, reused_outputs, early = _build_stage_tasks(stage, stage_outputs, reuse, model_override, timeout_override,
                                                      stage_validate, reuse_legacy, fingerprint_mode, session_name)
    if early is not None:
        return early

    if not tasks:
        return _empty_stage_result(stage, stage_outputs, reused_outputs)

    # Shared sessions must not run in parallel: interleaved calls would
    # corrupt the conversation order inside one session.
    if session_mode != "new" and stage.mode == "parallel":
        log.error("Stage '%s': --session-mode %s requires serial execution (parallel would interleave one session)",
                  stage.name, session_mode)
        return [], False

    stage_session_id = f"{session_name}-{stage.name}" if session_mode == "per-stage" else session_name

    results, outputs = _execute_stage_tasks(stage, tasks, reused_outputs, stage_validate,
                                            session_mode, session_name, stage_session_id)
    stage_outputs[stage.name] = outputs
    _log_stage_completion(stage, len(outputs), len(tasks))

    # T9 (round-4 finding P1-3): sidecars on the pipeline save path too —
    # previously single-batch only, so --reuse-fingerprint pipelines deleted
    # and regenerated artifacts every run.
    if fingerprint_mode:
        _write_stage_sidecars(results, stage_validate)

    stage_ok = all(r.success for r in results)
    stage_ok = _apply_approval(stage, stage_ok, outputs, approval_file)
    if not _run_stage_commands(stage):
        stage_ok = False

    _git_commit_stage(stage, outputs)  # failures are advisory, not a stage failure

    return results, stage_ok


def _apply_approval(stage: Stage, stage_ok: bool, outputs: list, approval_file: str) -> bool:
    """T12c (D5): a human approval point pauses the pipeline after the
    stage's deliverables exist (env / approve-file / TTY channels)."""
    if not (stage_ok and stage.approval and not _check_approval(stage, outputs, approval_file)):
        return stage_ok
    log.error("APPROVAL: stage '%s' not approved; pipeline halts", stage.name)
    return False


def _write_stage_sidecars(results: list, stage_validate: str) -> None:
    """T9: persist input fingerprints for a stage's successful artifacts so
    a later --reuse --reuse-fingerprint run can skip them. Sidecars are
    written on the pipeline save path (single-batch writes them in
    cli._write_sidecars); the fingerprint hashes the task's effective
    inputs, matching what the reuse check computed (round-4 P1-3/M6)."""
    for r in results:
        if r.success and r.task.output:
            eff = r.task.validate if r.task.validate is not None else stage_validate
            write_sidecar(str(r.task.output_path()), r.task.prompt, eff, r.task.model, r.task.provider)


def _log_stage_completion(stage: Stage, done: int, total_tasks: int) -> None:
    log.info("")
    log.info("Stage '%s' completed: %d/%d tasks succeeded", stage.name, done, total_tasks)


def _check_approval(stage: Stage, outputs: list, approval_file: str) -> bool:
    """T12c (D5): human-in-the-loop gate. Channels, in order:
    PBATCH_APPROVE=1 env -> approved; --approve-file exists -> approved;
    interactive y/n on a real TTY; anything else fails closed."""
    import os as _os
    if _os.environ.get("PBATCH_APPROVE", "").lower() in ("1", "true", "yes"):
        log.info("APPROVAL: stage '%s' approved via PBATCH_APPROVE env", stage.name)
        return True
    if approval_file and Path(approval_file).exists():
        log.info("APPROVAL: stage '%s' approved via %s", stage.name, approval_file)
        return True
    log.info("APPROVAL: stage '%s' deliverables: %s", stage.name, ", ".join(outputs))
    try:
        if not sys.stdin.isatty():
            log.warning("APPROVAL: stage '%s' needs human approval but stdin is not a TTY "
                        "(use --approve-file or PBATCH_APPROVE=1)", stage.name)
            return False
        reply = input(f"Approve stage '{stage.name}' outputs? (y/n): ").strip().lower()
        return reply in ("y", "yes")
    except Exception:
        return False


def _log_pipeline_start(pipeline: Pipeline, reuse: bool) -> None:
    log.info("")
    log.info("=" * 60)
    log.info("PIPELINE START (%d stages)", len(pipeline.stages))
    log.info("Mode: %s", "REUSE existing outputs" if reuse else "FORCE regeneration")
    log.info("=" * 60)


def _empty_stage_result(stage: Stage, stage_outputs: dict, reused_outputs: list) -> tuple[list, bool]:
    """Handle a stage with no tasks to run: fully-reused stages propagate
    their outputs downstream; otherwise warn and pass (nothing failed)."""
    if reused_outputs:
        stage_outputs[stage.name] = reused_outputs
        log.info("Stage '%s' fully reused (%d outputs)", stage.name, len(reused_outputs))
        return [], True
    log.warning("No tasks to execute in stage '%s'", stage.name)
    return [], True


def _log_stage_header(stage: Stage, reuse: bool) -> None:
    """Print the stage banner and whether reuse applies."""
    log.info("")
    log.info("=" * 60)
    log.info("STAGE: %s", stage.name)
    if reuse:
        log.info("(reusing existing outputs if available)")
    log.info("=" * 60)


def _build_stage_tasks(stage: Stage, stage_outputs: dict, reuse: bool, model_override: str, timeout_override: int,
                       validate_cmd: str, reuse_legacy: bool = False,
                       fingerprint_mode: bool = False, session_name: str = "") -> tuple[list, list, Optional[tuple]]:
    """Build the stage's task list from its input source (from_prompt,
    from_dir, or from_outputs). Returns (tasks, reused, early_result);
    early_result is not None when the stage must stop immediately."""
    tasks: list[Task] = []
    reused_outputs: list[str] = []

    # Dynamic role orchestration (meta stage) is handled entirely here.
    if stage.meta:
        return [], [], run_meta_stage(stage, stage_outputs, model_override, timeout_override, validate_cmd,
                                       session_name)

    if stage.from_prompt:
        tasks, reused_outputs = _stage_from_prompt_tasks(stage, reuse, model_override, timeout_override,
                                                         validate_cmd, reuse_legacy, fingerprint_mode)
        if not tasks and not reused_outputs:
            return [], [], ([], False)
    elif stage.from_dir:
        tasks, reused_outputs = _stage_from_dir_tasks(stage, reuse, model_override, timeout_override,
                                                      validate_cmd, reuse_legacy, fingerprint_mode)
    elif stage.from_outputs:
        tasks, reused_outputs = _stage_from_outputs_tasks(stage, stage_outputs, reuse, model_override, timeout_override,
                                                          validate_cmd, reuse_legacy, fingerprint_mode)
        if stage.from_outputs not in stage_outputs and not tasks:
            return [], [], ([], False)
    else:
        log.error("Stage '%s' must have either 'from_dir' or 'from_outputs'", stage.name)
        return [], [], ([], False)
    return tasks, reused_outputs, None


def _execute_stage_tasks(stage: Stage, tasks: list, reused_outputs: list, stage_validate: str,
                         session_mode: str, session_name: str, stage_session_id: str) -> tuple[list, list]:
    """Run the stage's tasks (serial or parallel) and collect the output
    paths that downstream stages will consume (reused outputs included)."""
    if stage.mode == "parallel":
        results = run_parallel(tasks, stage.workers, validate_cmd=stage_validate)
    else:
        results = run_serial(tasks, retries=0, session_mode=session_mode, session_id=stage_session_id, session_name=session_name,
                             validate_cmd=stage_validate)
    outputs = list(reused_outputs)
    for r in results:
        # Local fix (self-iteration round 2, P0-1): propagate the RESOLVED
        # path (output_path() honors task.cwd). Raw strings would make
        # git add/combine/archive operate on process-cwd-relative paths.
        if r.success and r.task.output:
            outputs.append(str(r.task.output_path()))
    return results, outputs


def _run_stage_commands(stage: Stage) -> bool:
    """Run the stage's shell commands (post-task hooks); False when any
    command failed, making hooks act as a failure gate for the stage."""
    if not stage.commands:
        return True
    log.info("")
    log.info("Running %d commands for stage '%s'... (parallel=%s)",
             len(stage.commands), stage.name, stage.commands_parallel)
    cmd_cwd = stage.cwd or os.getcwd()

    if stage.commands_parallel:
        with ThreadPoolExecutor(max_workers=len(stage.commands)) as pool:
            futs = {pool.submit(_run_single_cmd, cmd, i, len(stage.commands), cmd_cwd): cmd for i, cmd in enumerate(stage.commands, 1)}
            cmd_results = [f.result() for f in as_completed(futs)]
        all_cmd_ok = all(cmd_results)
    else:
        all_cmd_ok = True
        for i, cmd in enumerate(stage.commands, 1):
            if not _run_single_cmd(cmd, i, len(stage.commands), cmd_cwd):
                all_cmd_ok = False

    if all_cmd_ok:
        log.info("All %d commands passed for stage '%s'", len(stage.commands), stage.name)
    else:
        log.warning("Stage '%s' FAILED: some post-stage commands failed", stage.name)
    return all_cmd_ok


def _run_single_cmd(cmd: str, index: int, total: int, cmd_cwd: str) -> bool:
    """Run one post-stage shell command and stream the tail of its output."""
    log.info("CMD [%d/%d]: %s", index, total, cmd)
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cmd_cwd,
            capture_output=True, text=True, timeout=600
        )
        if proc.returncode == 0:
            log.info("CMD OK (exit=0) [%d/%d]", index, total)
            if proc.stdout:
                for line in proc.stdout.strip().split("\n")[-10:]:
                    log.info("  | %s", line)
        else:
            log.warning("CMD FAILED (exit=%d) [%d/%d]", proc.returncode, index, total)
            if proc.stderr:
                for line in proc.stderr.strip().split("\n")[-10:]:
                    log.warning("  | %s", line)
            if proc.stdout:
                for line in proc.stdout.strip().split("\n")[-5:]:
                    log.info("  | %s", line)
        return proc.returncode == 0
    except Exception as e:
        log.warning("CMD ERROR [%d/%d]: %s", index, total, e)
        return False


def _git_commit_stage(stage: Stage, outputs: list) -> bool:
    """Commit the stage's deliverables into git; failures are advisory
    (a commit problem must not fail the stage itself)."""
    if not stage.git_commit or not outputs:
        return True
    try:
        commit_msg = stage.commit_message or "[pi-batch] Stage: %s - %d tasks completed" % (stage.name, len(outputs))
        file_list = " ".join(["\"%s\"" % o for o in outputs])

        if subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, text=True, timeout=10).returncode != 0:
            log.warning("Not a git repository, skipping git commit")
            return False
        add = subprocess.run(["git", "add"] + outputs, capture_output=True, text=True, timeout=10)
        if add.returncode != 0:
            log.warning("git add failed (exit=%d): %s", add.returncode,
                        (add.stderr or "").strip()[:500])
            return False
        commit = subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True, text=True, timeout=10)
        if commit.returncode != 0:
            log.warning("git commit failed (exit=%d): %s", commit.returncode,
                        (commit.stderr or "").strip()[:500])
            return False
        log.info("GIT COMMIT: %s (files: %d)", commit_msg, len(outputs))
    except Exception as e:
        log.warning("Git commit failed: %s", e)
        return False
    return True


_GATE_VERDICT_RE = re.compile(r"^\s*VERDICT\s*:\s*(PASS|FAIL|REJECT)\b", re.IGNORECASE | re.MULTILINE)


def _archive_outputs(outputs: list, archive_dir: str, label: str) -> list:
    """Move finished deliverables into a timestamped archive subdirectory
    once the stage/pipeline completed (all tasks succeeded, gates passed).
    The worktree stays clean; git history (committed artifacts) retains
    everything, and the decision log points at the original paths. Returns
    the moved destinations for logging."""
    if not archive_dir or not outputs:
        return []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(archive_dir) / f"{label}-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    moved = []
    for o in outputs:
        p = Path(o)
        if p.exists():
            dest = target / p.name
            shutil.move(str(p), str(dest))
            moved.append(str(dest))
    if moved:
        log.info("ARCHIVED %d deliverable(s) -> %s", len(moved), target)
    return moved


def _gate_verdict(output_paths: list) -> Optional[str]:
    """Read a gate stage's deliverables and return its verdict
    (PASS/FAIL/REJECT) or None when no usable VERDICT: line is present.
    T12b: a bare "VERDICT: PASS" with no reason fails closed (a verdict
    must justify itself); the verdict text is untrusted, so any other
    absence is a FAIL too. FAIL/REJECT anywhere wins over an earlier PASS
    (order-independent: a self-contradictory deliverable must not unlock
    downstream stages — round-4 finding L2/DS-6)."""
    first_pass = None
    for p in output_paths:
        path = Path(p)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for m in _GATE_VERDICT_RE.finditer(text):
            # the reason is the text between the match end and the line end
            line_end = text.find("\n", m.end())
            if line_end == -1:
                line_end = len(text)
            rest = text[m.end():line_end].strip()
            if not rest:
                log.warning("GATE: %s has a bare VERDICT with no reason; failing closed", path)
                continue
            verdict = m.group(1).upper()
            if verdict in ("FAIL", "REJECT"):
                return verdict
            if first_pass is None:
                first_pass = verdict
    return first_pass


def _extract_decisions(text: str, limit: int = 5) -> list[str]:
    """Pull decision points (markdown headings) with their first sentence
    from a deliverable: an index into the full reasoning stored in the
    artifact, so the decision log stays readable while pointing at the
    complete rationale."""
    out = []
    for i, line in enumerate((text or "").splitlines()):
        if re.match(r"^#{2,3}\s+", line):
            heading = line.lstrip("# ").strip()
            nxt = ""
            for n in (text or "").splitlines()[i + 1:i + 4]:
                if n.strip() and not n.lstrip().startswith("#"):
                    nxt = n.strip()[:120]
                    break
            out.append(f"{heading}: {nxt}" if nxt else heading)
            if len(out) >= limit:
                break
    return out


def _append_decision_log(path: str, stage_name: str, results: list, stage_ok: bool, verdict: Optional[str]) -> None:
    """Append one structured decision record per finished stage: the
    stage, its status, each decision point extracted from the deliverables
    (heading + first sentence, the full reasoning lives in the artifacts),
    and the evidence file paths. Appending keeps the whole history; the
    log is a first-class deliverable of the pipeline."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as f:
        f.write(f"\n## {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — stage '{stage_name}' — "
                f"{'PASS' if stage_ok else 'FAIL'}")
        if verdict:
            f.write(f" (gate verdict: {verdict})")
        f.write("\n")
        for r in results:
            decisions = _extract_decisions(r.stdout)
            label = r.task.output or "(stdout)"
            status = "ok" if r.success else f"FAILED: {r.reason}"
            f.write(f"- task {label} [{status}]")
            if decisions:
                f.write(": " + "; ".join(decisions))
            f.write("\n")
        evidence = [r.task.output for r in results if r.task.output]
        if evidence:
            f.write(f"- evidence: {', '.join(evidence)}\n")


def run_pipeline(pipeline: Pipeline, model_override: str = "", dry_run: bool = False, reuse: bool = False, timeout_override: int = 0,
                 session_mode: str = "new", session_name: str = "", validate_cmd: str = "", decision_log: str = "", archive_dir: str = "",
                 reuse_legacy: bool = False, fingerprint_mode: bool = False,
                 approval_file: str = "") -> tuple[list[TaskResult], list[str]]:
    """Execute all stages sequentially; returns (all task results, names of
    stages that failed tasks or commands). Gates halt the pipeline, decision
    logs record every stage, and completed runs archive their deliverables."""
    all_results: list[TaskResult] = []
    failed_stages: list[str] = []
    stage_outputs: dict[str, list[str]] = {}  # stage_name -> [output_file_paths]
    
    _log_pipeline_start(pipeline, reuse)
    
    for stage in pipeline.stages:
        if dry_run:
            _describe_stage(stage, reuse)
            continue

        results, stage_ok = execute_stage(stage, stage_outputs, model_override, reuse, timeout_override,
                                          session_mode, session_name, validate_cmd, reuse_legacy,
                                          fingerprint_mode, approval_file)
        all_results.extend(results)
        if not stage_ok:
            failed_stages.append(stage.name)

        gate_verdict = _handle_gate(stage, stage_outputs)

        # Structured decision record for this stage (append-only history)
        log_path = decision_log or pipeline.decision_log
        if log_path and results:
            _append_decision_log(log_path, stage.name, results, stage_ok, gate_verdict)

        if gate_verdict in ("FAIL", "REJECT"):
            failed_stages.append(stage.name)
            log.error("Pipeline halted by gate: %s", stage.name)
            break

    # The pipeline completed (no failed stages, no gate rejection): move the
    # committed deliverables into the archive so the worktree stays clean.
    if not failed_stages and (pipeline.archive_dir or archive_dir):
        outputs = [r.task.output for r in all_results if r.success and r.task.output]
        _archive_outputs(outputs, archive_dir or pipeline.archive_dir, pipeline.name)
    
    return all_results, failed_stages


def _describe_stage(stage: Stage, reuse: bool) -> None:
    """Dry-run: print what the stage would do without executing it."""
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


def _handle_gate(stage: Stage, stage_outputs: dict) -> Optional[str]:
    """Evaluate a gate stage's deliverables: VERDICT: PASS/FAIL/REJECT.
    A missing verdict fails closed (no explicit pass, no unlock). Returns
    the verdict or None when the stage is not a gate."""
    if not stage.gate:
        return None
    verdict = _gate_verdict(stage_outputs.get(stage.name, []))
    if verdict is None:
        verdict = "FAIL"
        log.error("GATE stage '%s' produced no VERDICT: line; failing closed", stage.name)
    elif verdict in ("FAIL", "REJECT"):
        log.error("GATE REJECTED at stage '%s' (verdict: %s) — later stages blocked", stage.name, verdict)
    else:
        log.info("GATE PASSED at stage '%s'", stage.name)
    return verdict


def load_tasks(source: str) -> list[Task]:
    """Load tasks from a YAML file, JSON file, or plain text prompt.

    Supports @file.md references in the prompt field.
    """
    path = Path(source)
    if not path.exists():
        return [Task(prompt=source)]

    base_dir = str(path.parent) if path.parent else "."
    raw = path.read_text(encoding="utf-8")

    # YAML
    if source.endswith((".yaml", ".yml")) or raw.lstrip().startswith("tasks:"):
        if not yaml:
            # Local fix (self-iteration round 1): a structured task file must
            # not silently degrade into one giant raw-text prompt; fail loud
            # and consistent with load_pipeline's PyYAML gate.
            log.error("Task file '%s' requires PyYAML. Run: pip install pyyaml", source)
            sys.exit(1)
        try:
            data = yaml.safe_load(raw)
        except Exception as e:
            log.warning("Task file '%s' is not valid YAML (%s); treating it as a plain-text prompt", source, e)
            return [Task(prompt=raw.strip())]
        tasks_data = data.get("tasks", []) if isinstance(data, dict) else data
        tasks = []
        for t in tasks_data:
            task = Task(**{k: v for k, v in t.items() if k in Task.__dataclass_fields__})
            task.prompt = task.resolve_prompt(base_dir)
            tasks.append(task)
        return tasks

    # JSON
    try:
        tasks_data = json.loads(raw)
        if isinstance(tasks_data, dict):
            tasks_data = tasks_data.get("tasks", tasks_data)
        if isinstance(tasks_data, list):
            tasks = []
            for t in tasks_data:
                task = Task(**{k: v for k, v in t.items() if k in Task.__dataclass_fields__})
                task.prompt = task.resolve_prompt(base_dir)
                tasks.append(task)
            return tasks
    except json.JSONDecodeError:
        pass

    # Plain text prompt
    return [Task(prompt=raw.strip())]


def load_tasks_from_dir(directory: str, suffix: str = ".md") -> list[Task]:
    """Create one task per file in a directory.

    Each file's content becomes the prompt, and the output is saved as
    <filename>.out.md in the same directory (or specified output dir).
    """
    tasks = []
    basedir = Path(directory)
    if not basedir.is_dir():
        log.error("not a directory: %s", directory)
        return tasks

    for fpath in sorted(basedir.glob(f"*{suffix}")):
        if fpath.name.endswith(".out.md"):
            continue
        prompt = fpath.read_text(encoding="utf-8")
        out_name = fpath.stem + ".out.md"
        # Local fix (self-iteration round 2, F1): absolute output path —
        # cwd is the input dir, so relative outputs would double-prefix.
        tasks.append(Task(
            prompt=prompt,
            output=str((basedir / out_name).resolve()),
            cwd=str(basedir),
        ))
        log.info("loaded task from %s -> %s", fpath.name, out_name)

    return tasks
