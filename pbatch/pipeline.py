"""Pipeline execution: stage task builders, meta role orchestration,
verdict gates, decision logs, archiving, and task-source loading."""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from . import config
from . import metering
from .config import AGENT_DEFAULT_TIMEOUT, log, yaml
from .context_inject import prepare_task_context
from .models import Pipeline, Stage, Task, TaskResult
from .pipeline_archive import (archive_output_pairs,
                               archive_outputs as _archive_outputs_impl)
from .pipeline_decisions import (append_decision_log as _append_decision_log,
                                 extract_decisions as _extract_decisions)
from .pipeline_schema import validate_pipeline
from .pipeline_loader import parse_stage_def as _parse_stage_def
from .pipeline_resources import normalize_bundled_resources as _normalize_bundled_resources
from . import pipeline_receipts as _receipts
from .evidence import combine_outputs, source_manifest
from .gate_contract import evaluate_gate_artifacts, gate_verdict as _gate_verdict
from .meta import run_meta_stage
from .repo_gates import (capture_stage_baseline,
                         capture_stage_dirty_paths,  # re-exported for callers
                         commit_stage_outputs as _git_commit_stage,
                         read_only_violations,
                         run_stage_repo_validators)
from .reuse import fingerprint as reuse_fp
from .reuse import parallel_output_conflict, reuse_decision, write_sidecar
from .runner import (_save_validated, print_summary, run_parallel,
                     run_serial, run_task, run_validation)
from .text_io import read_text_bounded, read_yaml_bounded
from .task_loading import (load_tasks, load_tasks_from_dir, _task_data_error,
                           _task_string_error, _tasks_from_data)
from .pipeline_native import execute_stage_tasks as _execute_stage_tasks_native
from .tui_native import current_native_approval, current_native_observer
def load_pipeline(path: str) -> Pipeline:
    """Load pipeline definition from YAML file."""
    if not yaml:
        log.error("PyYAML not installed. Install the project: uv sync (or pip install pyyaml)")
        sys.exit(1)
    
    fpath = Path(path)
    if not fpath.exists():
        log.error("Pipeline file not found: %s", path)
        sys.exit(1)
    
    try:
        data = read_yaml_bounded(fpath, config.INPUT_MAX_BYTES, yaml, "pipeline file")
    except Exception as exc:
        log.error("Invalid pipeline YAML: %s", exc)
        sys.exit(1)
    _normalize_bundled_resources(data)
    errors = validate_pipeline(data)
    errors.extend(_pipeline_resource_errors(data))
    if errors:
        for error in errors:
            log.error("Invalid pipeline: %s", error)
        sys.exit(1)
    
    # Global settings (applied to all stages that don't set it explicitly)
    global_git_commit = data.get("git_commit", False)

    stages = [_parse_stage_def(s, global_git_commit) for s in data["stages"]]
    
    return Pipeline(stages=stages, decision_log=data.get("decision_log", ""),
                    archive_dir=data.get("archive_dir", ""), name=Path(path).stem)

def _pipeline_resource_errors(data: dict) -> list[str]:
    errors = []
    stages = data.get("stages", []) if isinstance(data, dict) else []
    for stage in stages if isinstance(stages, list) else []:
        if not isinstance(stage, dict):
            continue
        tasks = stage.get("tasks", [])
        for index, task in enumerate(tasks if isinstance(tasks, list) else [], 1):
            if not isinstance(task, dict):
                continue
            value = task.get("prompt_template")
            if not value or not isinstance(value, str):
                continue
            path = Path(value)
            if not path.is_file() or path.is_symlink():
                errors.append(
                    f"stage '{stage.get('name', '')}'.tasks[{index}].prompt_template is missing or unsafe: {value}")
            else:
                try:
                    oversized = path.stat().st_size > config.INPUT_MAX_BYTES
                except OSError:
                    oversized = True
                if oversized:
                    errors.append(
                        f"stage '{stage.get('name', '')}'.tasks[{index}].prompt_template exceeds "
                        f"{config.INPUT_MAX_BYTES} bytes: {value}")
    return errors
def _task_prompt(task_def: dict, input_content: str, input_stem: str, input_path: str,
                original_prompt: str = "") -> str:
    """Build the prompt for a task definition: a literal 'prompt' string or
    a 'prompt_template' file, with {input_content}/{input_stem}/{input_path}
    placeholders plus {prompt} (the original task prompt that launched the
    pipeline, e.g. via -p --classify routing; empty when launched without
    one — aero-id maintenance round R5 finding). Returns '' when neither
    is present (caller skips it)."""
    prompt = task_def.get("prompt", "")
    prompt_template_path = Path(task_def.get("prompt_template", ""))
    if task_def.get("prompt_template") and not prompt_template_path.exists():
        prompt_template_path = Path(config.resolve_asset_path(
            str(task_def["prompt_template"])))
    if task_def.get("prompt_template") and prompt_template_path.exists():
        prompt = read_text_bounded(prompt_template_path, config.INPUT_MAX_BYTES,
                                   "prompt template")
    if not prompt:
        log.error("Task in stage needs 'prompt' or an existing 'prompt_template' file")
        return ""
    return (prompt.replace("{input_content}", input_content)
                 .replace("{input_stem}", input_stem)
                 .replace("{input_path}", input_path)
                 .replace("{prompt}", original_prompt))
def _expected_fp(prompt: str, out: str, validate_cmd: str, model: str,
                 provider: str, fp_mode: bool, workdir: str = "",
                 agent: str = "", thinking: str = "", tools: str = "",
                 exclude_tools: str = "", no_tools: bool = False) -> Optional[str]:
    """T9: fingerprint for a reuse candidate when --reuse-fingerprint is on
    (None otherwise); a mismatch regenerates the artifact. model/provider
    are the TASK's EFFECTIVE values (overrides included) — hashing the raw
    stage values instead made the fingerprint never match the sidecar that
    records the executed task's values (round-4 finding M6)."""
    if not fp_mode:
        return None
    return reuse_fp(prompt, out, validate_cmd or "", model, provider,
                    workdir, agent, thinking, tools, exclude_tools, no_tools)

def _task_model_route_source(task_def: dict, stage: Optional[Stage],
                             model_override: str) -> str:
    if model_override:
        return "cli_override"
    if task_def.get("model"):
        return "task_model"
    if stage and stage.model:
        return "stage_model"
    return "agent_default"


def _task_from_def(task_def: dict, prompt: str, output_path: str, model_override: str = "",
                   timeout_override: int = 0, stage: Optional[Stage] = None) -> Task:
    """Build a task from a pipeline template definition, applying CLI-level
    model and timeout overrides on top of per-task values."""
    task = Task(
        prompt=prompt,
        output=output_path,
        agent=task_def.get("agent") or (stage.agent if stage else ""),
        model=(task_def.get("model") or (stage.model if stage else "")
               or config.AGENT_DEFAULT_MODEL),
        provider=task_def.get("provider") or (stage.provider if stage else ""),
        thinking=task_def.get("thinking") or "",
        tools=task_def.get("tools") or "",
        exclude_tools=task_def.get("exclude_tools") or "",
        no_tools=task_def.get("no_tools", False),
        cwd=task_def.get("cwd", stage.cwd if stage else ""),
        timeout=task_def.get("timeout", AGENT_DEFAULT_TIMEOUT),
        env=dict(task_def.get("env", {})),
        # Public task YAML uses `validate`; retain the historical
        # `validate_cmd` spelling with the same explicit-key precedence as a
        # stage definition.
        validate=task_def.get("validate", task_def.get("validate_cmd")),
    )
    task.memory["model_route_source"] = _task_model_route_source(
        task_def, stage, model_override)
    if task_def.get("role"):
        task.memory["role"] = task_def["role"]
    if model_override:
        task.model = model_override
    if timeout_override:
        task.timeout = timeout_override
    return task


def _stage_from_prompt_tasks(stage: Stage, reuse: bool, model_override: str, timeout_override: int,
                             validate_cmd: str = "", reuse_legacy: bool = False,
                             fingerprint_mode: bool = False,
                             original_prompt: str = "") -> tuple[list[Task], list[str]]:
    """One-sentence starting point: a single task whose output feeds
    downstream from_outputs stages (no input file needed). {prompt} in
    the stage's from_prompt is replaced with the original task prompt
    that launched the pipeline (empty when none)."""
    if not stage.output:
        log.error("Stage '%s': from_prompt requires output", stage.name)
        return [], []
    task = Task(prompt=stage.from_prompt.replace("{prompt}", original_prompt),
                output=stage.output, agent=stage.agent,
                model=stage.model or config.AGENT_DEFAULT_MODEL,
                provider=stage.provider, cwd=stage.cwd)
    # from_prompt supports @file references (same semantics as task files):
    # the pipeline can inject an external requirement without YAML editing.
    try:
        task.prompt = task.resolve_prompt(task.workdir())
    except ValueError as exc:
        log.error("Stage '%s': prompt reference rejected: %s", stage.name, exc)
        return [], []
    if model_override:
        task.model = model_override
    if timeout_override:
        task.timeout = timeout_override
    prepare_task_context(task)
    # T9 (round-4 M6): hash the task's effective inputs so the sidecar
    # written after success matches the reuse-check fingerprint.
    out_resolved = str(task.output_path_unresolved()) if task.output else ""
    fp = _expected_fp(task.prompt, out_resolved, validate_cmd, task.model,
                      task.provider, fingerprint_mode, task.workdir(),
                      task.effective_agent(), task.thinking, task.tools,
                      task.exclude_tools, task.no_tools)
    if reuse and reuse_decision(out_resolved, validate_cmd, task.workdir(),
                                reuse_legacy, fp):
        log.info("REUSE: %s (reused+validated)", stage.output)
        return [], [out_resolved]
    log.info("Loaded 1 task from from_prompt for stage '%s'", stage.name)
    return [task], []

def _stage_from_dir_tasks(stage: Stage, reuse: bool, model_override: str, timeout_override: int,
                          validate_cmd: str = "", reuse_legacy: bool = False,
                          fingerprint_mode: bool = False) -> tuple[list[Task], list[str]]:
    """Read .md files from a directory; one task per file (default), or a
    single merged task when the stage sets `aggregate: true` (evidence
    normalization without one LLM call per file)."""
    dir_path = Path(stage.from_dir)
    if not dir_path.is_dir():
        log.error("Directory not found: %s", stage.from_dir)
        return [], []
    files = sorted(
        fpath for fpath in dir_path.glob(f"*{stage.suffix}")
        if not fpath.name.endswith(stage.output_suffix)
    )
    if stage.aggregate:
        from .from_dir import _stage_from_dir_aggregate
        return _stage_from_dir_aggregate(
            stage, files, reuse, model_override, timeout_override,
            validate_cmd, reuse_legacy, fingerprint_mode,
        )
    tasks: list[Task] = []
    reused: list[str] = []
    for fpath in files:
        # Resolve absolutely: task cwd is the input dir (relative outputs
        # would double-prefix under output_path()).
        out_path = Path(os.path.abspath(
            fpath.parent / (fpath.stem + stage.output_suffix)))
        prompt = read_text_bounded(fpath, config.INPUT_MAX_BYTES,
                                   "stage input")
        task = Task(prompt=prompt, output=str(out_path), cwd=str(fpath.parent))
        task.agent = stage.agent or task.agent
        task.model = stage.model or task.model
        task.provider = stage.provider or task.provider
        if model_override:
            task.model = model_override
        if timeout_override:
            task.timeout = timeout_override
        prepare_task_context(task, [fpath.resolve()], strict=False)
        fp = _expected_fp(task.prompt, str(out_path), validate_cmd, task.model,
                          task.provider, fingerprint_mode, task.workdir(),
                          task.effective_agent(), task.thinking, task.tools,
                          task.exclude_tools, task.no_tools)
        if reuse and reuse_decision(str(out_path), validate_cmd, str(fpath.parent), reuse_legacy, fp):
            log.info("REUSE: %s (reused+validated)", out_path.name)
            reused.append(str(out_path))
            continue
        tasks.append(task)
    log.info("Loaded %d tasks from %s", len(tasks), stage.from_dir)
    return tasks, reused





def _collect_upstream(from_outputs, stage_outputs: dict) -> tuple[list[str], list[str], list[str]]:
    """Resolve one or many upstream stage names into one ordered evidence list."""
    raw = from_outputs if isinstance(from_outputs, (list, tuple)) else [from_outputs]
    names = [str(name).strip() for name in raw if str(name).strip()]
    missing = [name for name in names if name not in stage_outputs]
    outputs = []
    for name in names:
        outputs.extend(stage_outputs.get(name, []))
    return names, outputs, missing

def _parallel_session_rejected(stage: Stage, session_mode: str,
                               session_name: str) -> bool:
    """Shared conversations cannot safely interleave parallel stage tasks."""
    if session_mode == "new" or stage.mode != "parallel":
        return False
    log.error("Stage '%s': --session-mode %s requires serial execution "
              "(parallel would interleave one session)", stage.name, session_mode)
    from .memory import record_stage
    record_stage(stage.name, "FAILED", "shared session cannot run in parallel",
                 [], stage.cwd or os.getcwd(), session_name)
    return True

def execute_stage(stage: Stage, stage_outputs: dict[str, list[str]], model_override: str = "", reuse: bool = False, timeout_override: int = 0,
                  session_mode: str = "new", session_name: str = "", validate_cmd: str = "",
                  reuse_legacy: bool = False, fingerprint_mode: bool = False,
                  approval_file: str = "", original_prompt: str = "",
                  extra_evidence: Optional[list[str]] = None,
                  retries: int = 0, retry_delay: float = 10.0,
                  backoff: float = 2.0,
                  git_baseline=None,
                  session_state: Optional[set] = None) -> tuple[list[TaskResult], bool]:
    """Execute one stage; extra_evidence folds gate-fix reports into it."""
    # Reset per-execution so a multi-round run cannot carry a previous round's
    # citation count onto a stage that never cited anything.
    stage.knowledge_cited = 0
    _log_stage_header(stage, reuse)
    stage_validate = stage.validate_cmd if stage.validate_cmd is not None else validate_cmd
    tasks, reused_outputs, early = _build_stage_tasks(stage, stage_outputs, reuse, model_override, timeout_override,
                                                      stage_validate, reuse_legacy, fingerprint_mode, session_name,
                                                      original_prompt, extra_evidence)
    if early is not None:
        results, stage_ok = early
        outputs = stage_outputs.get(stage.name, [])
        return _finalize_stage(stage, results, outputs, stage_ok,
                               approval_file, stage_validate, git_baseline)
    duplicate = parallel_output_conflict(stage.mode, tasks, reused_outputs)
    if duplicate:
        _reject_duplicate_outputs(stage, duplicate, session_name)
        return [], False
    if not tasks:
        results, stage_ok = _empty_stage_result(stage, stage_outputs, reused_outputs)
        outputs = stage_outputs.get(stage.name, reused_outputs)
        return _finalize_stage(stage, results, outputs, stage_ok,
                               approval_file, stage_validate, git_baseline)
    for task in tasks:
        task.memory.setdefault("stage", stage.name)
    if _parallel_session_rejected(stage, session_mode, session_name):
        return [], False
    stage_session_id = f"{session_name}-{stage.name}" if session_mode == "per-stage" else session_name
    results, outputs = _execute_stage_tasks(stage, tasks, reused_outputs, stage_validate,
                                            session_mode, session_name, stage_session_id,
                                            retries, retry_delay, backoff,
                                            session_state)
    stage_outputs[stage.name] = outputs
    _log_stage_completion(stage, len(outputs), len(tasks))
    if fingerprint_mode:
        _write_stage_sidecars(results, stage_validate)
    return _finalize_stage(stage, results, outputs,
                           all(r.success for r in results), approval_file,
                           stage_validate, git_baseline)

def _reject_duplicate_outputs(stage: Stage, duplicate: str,
                             session_name: str) -> None:
    """Two parallel tasks writing one path would race; refuse before any spawn."""
    log.error("Stage '%s': parallel tasks share output %s; failing closed",
              stage.name, duplicate)
    from .memory import record_stage
    record_stage(stage.name, "FAILED", "duplicate parallel output",
                 [], stage.cwd or os.getcwd(), session_name)


def _finalize_stage(stage: Stage, results: list[TaskResult], outputs: list[str],
                    stage_ok: bool, approval_file: str,
                    effective_validate=None,
                    git_baseline=None) -> tuple[list[TaskResult], bool]:
    """Apply stage-wide gates even when every artifact was reused."""
    if getattr(current_native_approval(), "cancelled", False):
        _record_stage_failure(stage, results, outputs, False, False, False, "", False)
        return results, False
    task_ok = stage_ok
    approved = _apply_approval(stage, task_ok, outputs, approval_file)
    commands_ok = _run_stage_commands(stage) if approved else False
    repo_ok, repo_failed = ((run_stage_repo_validators(
        stage, results, outputs, effective_validate))
        if commands_ok else (False, ""))
    stage_ok = task_ok and approved and commands_ok and repo_ok
    commit_failed = bool(
        stage_ok and not stage.gate
        and not _git_commit_stage(stage, outputs, git_baseline))
    stage_ok = stage_ok and not commit_failed
    if not stage_ok:
        _record_stage_failure(stage, results, outputs, task_ok, approved,
                              commands_ok, repo_failed, commit_failed)
    return results, stage_ok

def _record_stage_failure(stage: Stage, results: list, outputs: list,
                          task_ok: bool, approved: bool, commands_ok: bool,
                          repo_failed: str, commit_failed: bool) -> None:
    from .memory import record_stage
    reason = next((str(result.reason).strip() for result in results
                   if not result.success and str(result.reason).strip()), "")
    reason = reason or ("task execution failed" if not task_ok else "stage failed")
    if task_ok and stage.approval and not approved:
        reason += "; approval rejected or missing"
    if approved and not commands_ok:
        reason += "; stage command failed"
    if repo_failed:
        reason += f"; repo validator failed: {repo_failed}"
    if commit_failed:
        reason += "; git commit failed"
    record_stage(stage.name, "FAILED", reason[:500], outputs,
                 stage.cwd or os.getcwd())

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
            write_sidecar(str(r.task.output_path_unresolved()), r.task.prompt, eff,
                          r.task.model, r.task.provider, r.task.workdir(),
                          r.task.effective_agent(), r.task.thinking, r.task.tools,
                          r.task.exclude_tools, r.task.no_tools)

def _log_stage_completion(stage: Stage, done: int, total_tasks: int) -> None:
    log.info("")
    log.info("Stage '%s' completed: %d/%d tasks succeeded", stage.name, done, total_tasks)

def _check_approval(stage: Stage, outputs: list, approval_file: str) -> bool:
    """T12c (D5): human-in-the-loop gate with native IPC first.
    Native TUI approval is exact-run scoped; headless channels retain the
    historical env/file/TTY behavior and fail closed without a TTY."""
    native = current_native_approval()
    if native is not None:
        return native.request(stage.name, outputs)
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
    their outputs downstream; otherwise fail closed. A configured stage that
    silently does no work must not unlock its downstream stages."""
    if reused_outputs:
        stage_outputs[stage.name] = reused_outputs
        log.info("Stage '%s' fully reused (%d outputs)", stage.name, len(reused_outputs))
        return [], True
    log.error("No tasks to execute in stage '%s'; failing closed", stage.name)
    return [], False

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
                       fingerprint_mode: bool = False, session_name: str = "",
                       original_prompt: str = "",
                       extra_evidence: Optional[list[str]] = None) -> tuple[list, list, Optional[tuple]]:
    """Build the stage's task list from its input source (from_prompt,
    from_dir, or from_outputs). Returns (tasks, reused, early_result);
    early_result is not None when the stage must stop immediately."""
    tasks: list[Task] = []
    reused_outputs: list[str] = []
    try:
        if stage.meta:
            return [], [], run_meta_stage(
                stage, stage_outputs, model_override, timeout_override,
                validate_cmd, session_name, reuse, reuse_legacy,
                fingerprint_mode, original_prompt,
                cancel_event=getattr(current_native_approval(), "cancel_event", None))
        if stage.from_prompt:
            tasks, reused_outputs = _stage_from_prompt_tasks(stage, reuse, model_override, timeout_override,
                                                             validate_cmd, reuse_legacy, fingerprint_mode,
                                                             original_prompt)
            if not tasks and not reused_outputs:
                return [], [], ([], False)
        elif stage.from_dir:
            tasks, reused_outputs = _stage_from_dir_tasks(stage, reuse, model_override, timeout_override,
                                                          validate_cmd, reuse_legacy, fingerprint_mode)
        elif stage.from_outputs:
            tasks, reused_outputs = _stage_from_outputs_tasks(stage, stage_outputs, reuse, model_override, timeout_override,
                                                              validate_cmd, reuse_legacy, fingerprint_mode,
                                                              original_prompt, extra_evidence)
            _, _, missing = _collect_upstream(stage.from_outputs, stage_outputs)
            if missing and not tasks:
                return [], [], ([], False)
        else:
            log.error("Stage '%s' must have either 'from_dir' or 'from_outputs'", stage.name)
            return [], [], ([], False)
    except ValueError as exc:
        reason = f"stage input rejected: {exc}"
        log.error("Stage '%s': %s", stage.name, reason)
        failed = TaskResult(Task("", cwd=stage.cwd), False, returncode=-1,
                            reason=reason)
        return [], [], ([failed], False)
    return tasks, reused_outputs, None

def _execute_stage_tasks(stage: Stage, tasks: list, reused_outputs: list, stage_validate: str,
                         session_mode: str, session_name: str, stage_session_id: str,
                         retries: int = 0, retry_delay: float = 10.0,
                         backoff: float = 2.0,
                         session_state: Optional[set] = None) -> tuple[list, list]:
    """Run stage tasks through the optional native observation bridge."""
    return _execute_stage_tasks_native(
        stage, tasks, reused_outputs, stage_validate, session_mode,
        session_name, stage_session_id, retries, retry_delay, backoff,
        session_state, current_native_observer(), _execute_or_tasks,
        run_parallel, run_serial)

def _execute_or_tasks(stage: Stage, tasks: list, reused_outputs: list,
                        stage_validate: str, session_mode: str,
                        session_name: str, stage_session_id: str,
                        session_state: Optional[set] = None,
                        cancel_event=None, result_callback=None) -> tuple:
    """OR 算子（AADM §6）：候选任务模板按序执行，**第一个通过验证者
    胜出**（其余候选跳过）；全部失败 → 阶段失败（fail closed）。"""
    results = []
    for index, task in enumerate(tasks, 1):
        log.info("-- OR candidate [%d/%d]: %s --", index, len(tasks),
                 " ".join(task.prompt.split())[:60])
        batch = run_serial([task], retries=0, session_mode=session_mode,
                           session_id=stage_session_id,
                           session_name=session_name,
                           validate_cmd=stage_validate,
                           session_state=session_state,
                           cancel_event=cancel_event,
                           result_callback=result_callback)
        results.extend(batch)
        if batch and batch[0].success:
            log.info("OR: candidate %d 通过，其余候选跳过", index)
            break
    outputs = list(reused_outputs)
    for result in results:
        if result.success and result.task.output:
            outputs.append(str(result.task.output_path()))
    return results, outputs


def _run_stage_commands(stage: Stage) -> bool:
    """Post-hook commands + L4 verify-experiment；任一失败即阶段失败。"""
    cmd_cwd = stage.cwd or os.getcwd()
    all_cmd_ok = _run_cmd_set(stage, stage.commands, "commands",
                              stage.commands_parallel, cmd_cwd)
    experiments = stage.verify_experiment
    if experiments:
        all_cmd_ok = _run_cmd_set(stage, experiments, "verify-experiment",
                                  False, cmd_cwd) and all_cmd_ok
    return all_cmd_ok


def _run_cmd_set(stage: Stage, cmds: list, kind: str, parallel: bool,
                 cmd_cwd: str) -> bool:
    """Run one command set (post-hooks or L4 verify-experiment) within
    the stage's timeout/output bounds; parallel when requested."""
    if not cmds:
        return True
    log.info("Running %d %s for stage '%s'... (parallel=%s)",
             len(cmds), kind, stage.name, parallel)

    if parallel:
        with ThreadPoolExecutor(max_workers=len(cmds)) as pool:
            futs = {
                pool.submit(_run_single_cmd, cmd, i, len(cmds), cmd_cwd,
                            stage.command_timeout,
                            stage.command_output_max_bytes): cmd
                for i, cmd in enumerate(cmds, 1)
            }
        all_cmd_ok = all(f.result() for f in as_completed(futs))
    else:
        all_cmd_ok = True
        for i, cmd in enumerate(cmds, 1):
            if not _run_single_cmd(cmd, i, len(cmds), cmd_cwd,
                                   stage.command_timeout,
                                   stage.command_output_max_bytes):
                all_cmd_ok = False

    if all_cmd_ok:
        log.info("All %d %s passed for stage '%s'", len(cmds), kind, stage.name)
        return True
    log.warning("Stage '%s' FAILED: some %s failed", stage.name, kind)
    return False

def _run_single_cmd(cmd: str, index: int, total: int, cmd_cwd: str,
                    timeout: int, output_max_bytes: int) -> bool:
    """Run one post-stage command with a hard process-tree deadline."""
    from .runner import expand_command_placeholders
    from .triage import write_cmd_failure_diagnostics
    cmd = expand_command_placeholders(cmd, cmd_cwd, cmd_cwd)
    log.info("CMD [%d/%d]: %s", index, total, cmd)
    result = run_validation(cmd, cmd_cwd, timeout=timeout, cap=output_max_bytes)
    if result.ok:
        log.info("CMD OK (exit=0) [%d/%d]", index, total)
        _log_command_tail(result.stdout, log.info, 10)
        return True
    if result.timed_out:
        log.warning("CMD FAILED (timeout=%ss) [%d/%d]", timeout, index, total)
    else:
        log.warning("CMD FAILED (exit=%d) [%d/%d]", result.exit_code, index, total)
    _log_command_tail(result.stderr, log.warning, 10)
    _log_command_tail(result.stdout, log.info, 5)
    write_cmd_failure_diagnostics(cmd, result.exit_code, result.stderr, cmd_cwd)
    return False


def _log_command_tail(value: str, writer, lines: int) -> None:
    """Log only the already-bounded diagnostic tail for a stage command."""
    for line in (value or "").strip().splitlines()[-lines:]:
        writer("  | %s", line)

def _archive_outputs(outputs: list, archive_dir: str, label: str) -> list:
    """Compatibility wrapper around the extracted archive implementation."""
    return _archive_outputs_impl(outputs, archive_dir, label)

def _gate_reuse_blocked(output_path: str) -> bool:
    """A gate deliverable carrying VERDICT: FAIL/REJECT must NEVER be
    reused by --reuse: the verdict binds to repository state, which
    evidence fingerprints cannot see (a code fix after the FAIL does not
    change the evidence docs — aero-id maintenance round R5e finding).
    A gate artifact without a usable verdict fails closed the same way.
    Returns True when the artifact exists and must be regenerated (it is
    deleted so the caller reruns the stage); False = safe to consult
    reuse_decision for a PASS artifact."""
    path = Path(output_path)
    if not path.is_file():
        return False
    # Only a reasoned PASS is reusable. _gate_verdict returns None for a
    # missing or bare verdict, so comparing against PASS also covers malformed
    # gate artifacts instead of accidentally preserving them under --reuse.
    if _gate_verdict([str(path)]) != "PASS":
        log.warning("REUSE: %s has no usable PASS verdict; a rejected, bare, "
                    "or missing gate verdict is never reusable — regenerating", path)
        path.unlink(missing_ok=True)
        return True
    return False

def _gate_reuse_warn(output_path: str) -> None:
    """Loud reminder when a PASS gate artifact is reused: the verdict was
    bound to the repository state at verdict time; code changes since then
    invalidate it (rerun with --force to re-verify)."""
    log.warning("REUSE: %s gate verdict is evidence-bound; if repository code "
                "changed since the verdict, rerun with --force to re-verify", output_path)

def _maybe_reuse_task(stage: Stage, task: Task, out_resolved: str, output_path: str,
                      effective_validate: str, reuse_legacy: bool, fp, reuse: bool,
                      reused: list) -> bool:
    """One task's --reuse decision for from_outputs builders: gate stages
    additionally refuse FAIL/REJECT verdicts (never reusable) and warn when
    a PASS verdict is reused. Returns True when the task was reused."""
    if not (reuse and task.output):
        return False
    if not reuse_decision(out_resolved, effective_validate, task.workdir(),
                          reuse_legacy, fp):
        return False
    if stage.gate and _gate_reuse_blocked(out_resolved):
        return False
    if stage.gate:
        _gate_reuse_warn(out_resolved)
    log.info("REUSE: %s (reused+validated)", output_path)
    reused.append(out_resolved)
    return True

def _include_reused_results(results: list, outputs: list[str]) -> list:
    if results or not outputs:
        return results
    return [TaskResult(Task(prompt="", output=path), True,
                       returncode=0, reason="reused") for path in outputs]

def _mark_failed(failed_stages: list, stage_name: str) -> None:
    if stage_name not in failed_stages:
        failed_stages.append(stage_name)

def _record_pipeline_stage(stage: Stage, results: list, stage_ok: bool,
                           verdict: Optional[str], decision_log: str) -> None:
    if decision_log and results:
        _append_decision_log(
            decision_log, stage.name, results, stage_ok, verdict)
    _record_receipt(stage, results, stage_ok, verdict)


def _record_receipt(stage: Stage, results: list, stage_ok: bool,
                    verdict: Optional[str]) -> None:
    """Keep the machine-readable twin of the decision-log entry.

    Recorded even when no decision log was configured: the human summary is
    opt-in per pipeline, but the verified artifact record is what a later run
    reuses, so it must not depend on a document the pipeline may not produce.
    """
    if not results:
        return
    _receipts.record_stage(stage.cwd or "", stage.name, results, stage_ok,
                           verdict, getattr(stage, "knowledge_cited", 0))


def _read_only_stage_ok(stage: Stage, outputs: list,
                        git_baseline) -> bool:
    if not stage.read_only:
        return True
    violations = read_only_violations(stage, outputs, git_baseline)
    if violations:
        log.error("READ-ONLY stage '%s' changed source paths: %s",
                  stage.name, ", ".join(violations[:20]))
        return False
    return True


def _stage_git_baseline(stage: Stage):
    if stage.git_commit or stage.read_only:
        return capture_stage_baseline(stage.cwd or os.getcwd(),
                                      stage.read_only_paths,
                                      read_only=stage.read_only)
    return None


def _run_pipeline_stage(stage: Stage, stage_outputs: dict, failed_stages: list,
                        model_override: str, reuse: bool, timeout_override: int,
                        session_mode: str, session_name: str, validate_cmd: str,
                        reuse_legacy: bool, fingerprint_mode: bool, approval_file: str,
                        original_prompt: str, decision_log: str, retries: int = 0, retry_delay: float = 10.0,
                        backoff: float = 2.0, session_state: Optional[set] = None) -> tuple[bool, list]:
    """Execute, gate, optionally fix one stage; return (stop, results)."""
    git_baseline = _stage_git_baseline(stage)
    results, stage_ok = execute_stage(stage, stage_outputs, model_override, reuse,
                                      timeout_override, session_mode, session_name,
                                      validate_cmd, reuse_legacy, fingerprint_mode,
                                      approval_file, original_prompt,
                                      retries=retries, retry_delay=retry_delay,
                                      backoff=backoff, git_baseline=git_baseline,
                                      session_state=session_state)
    stage_ok = stage_ok and _read_only_stage_ok(stage, stage_outputs.get(stage.name, []), git_baseline)
    results = _include_reused_results(results, stage_outputs.get(stage.name, []))
    if not stage_ok:
        _mark_failed(failed_stages, stage.name)
        _record_pipeline_stage(stage, results, False, None, decision_log)
        log.error("Pipeline halted: %s (stage failed)", stage.name)
        return True, results
    verdict = _handle_gate(stage, stage_outputs, session_name)
    if verdict == "PASS" and not _git_commit_stage(stage, stage_outputs.get(
            stage.name, []), git_baseline):
        stage_ok = False
    _record_pipeline_stage(stage, results, stage_ok, verdict, decision_log)
    if not stage_ok:
        _mark_failed(failed_stages, stage.name)
        log.error("Pipeline halted: %s (git commit failed)", stage.name)
        return True, results
    if verdict in ("FAIL", "REJECT") and stage.gate_fix_rounds > 0:
        effective_validate = (stage.validate_cmd if stage.validate_cmd is not None
                              else validate_cmd)
        verdict, fix_results = _run_gate_fix_loop(stage, stage_outputs,
            effective_validate, session_name, reuse, model_override,
            timeout_override, session_mode, reuse_legacy, fingerprint_mode,
            approval_file, original_prompt, decision_log, git_baseline, session_state)
        results = [*results, *fix_results]
        if verdict == "PASS":
            return False, results  # gate resolved; move on
        _mark_failed(failed_stages, stage.name)
        log.error("Pipeline halted by gate: %s (after %d fix round(s))",
                  stage.name, stage.gate_fix_rounds)
        return True, results
    if verdict in ("FAIL", "REJECT"):
        _mark_failed(failed_stages, stage.name)
        log.error("Pipeline halted: %s (%s)", stage.name, verdict)
        return True, results
    return False, results

def run_pipeline(pipeline: Pipeline, model_override: str = "", dry_run: bool = False, reuse: bool = False, timeout_override: int = 0,
                 session_mode: str = "new", session_name: str = "", validate_cmd: str = "", decision_log: str = "", archive_dir: str = "",
                 reuse_legacy: bool = False, fingerprint_mode: bool = False,
                 approval_file: str = "", original_prompt: str = "",
                 retries: int = 0, retry_delay: float = 10.0,
                 backoff: float = 2.0,
                 event_sink=None, approval_channel=None) -> tuple[list[TaskResult], list[str]]:
    """Execute stages sequentially with an optional native event observer."""
    from .pipeline_native import run_pipeline_observed
    return run_pipeline_observed(
        pipeline, model_override, dry_run, reuse, timeout_override,
        session_mode, session_name, validate_cmd, decision_log, archive_dir,
        reuse_legacy, fingerprint_mode, approval_file, original_prompt,
        retries, retry_delay, backoff, event_sink, approval_channel)

def _archive_pipeline_results(pipeline: Pipeline, results: list[TaskResult],
                              archive_dir: str, session_name: str) -> None:
    outputs = [result.task.output for result in results
               if result.success and result.task.output]
    moved = dict(archive_output_pairs(
        outputs, archive_dir or pipeline.archive_dir, pipeline.name))
    for result in results:
        if result.task.output in moved:
            result.task.output = moved[result.task.output]
    from .memory import record_archive
    cwd = pipeline.stages[0].cwd if pipeline.stages and pipeline.stages[0].cwd else os.getcwd()
    record_archive(list(moved.items()), cwd, session_name)

def _handle_gate(stage: Stage, stage_outputs: dict, session_name: str = "") -> Optional[str]:
    """Evaluate a gate stage's deliverables: VERDICT: PASS/FAIL/REJECT.
    A missing verdict fails closed (no explicit pass, no unlock). Returns
    the verdict or None when the stage is not a gate."""
    if not stage.gate:
        return None
    decision = evaluate_gate_artifacts(stage_outputs.get(stage.name, []))
    verdict = decision.verdict
    if verdict is None:
        verdict = "FAIL"
        if (decision.source == "none"
                or "no usable Gate verdict" in decision.reason):
            log.error("GATE stage '%s' produced no VERDICT: line; failing closed",
                      stage.name)
        else:
            log.error("GATE stage '%s' rejected: %s (failing closed)", stage.name,
                      decision.reason or "no usable verdict")
    elif verdict in ("FAIL", "REJECT"):
        log.error("GATE REJECTED at stage '%s' (verdict: %s) — later stages blocked", stage.name, verdict)
    else:
        log.info("GATE PASSED at stage '%s'", stage.name)
    observer = current_native_observer()
    if observer is not None:
        observer.gate_verdict(
            stage.name, verdict, decision.reason,
            len(stage_outputs.get(stage.name, [])))
    from .memory import record_gate
    record_gate(stage.name, verdict, stage_outputs.get(stage.name, []),
                stage.cwd or os.getcwd(), session_name)
    metering.record_event(
        "gate_pass" if verdict == "PASS" else "gate_reject",
        stage.name,
        f"verdict={verdict};source={decision.source};"
        f"findings={len(decision.findings)};reason={decision.reason[:300]}",
        stage.cwd or os.getcwd(), session_name,
        stage_id=stage.name, phase="governance")
    return verdict

def _run_gate_fix_loop(stage: Stage, stage_outputs: dict, validate_cmd: str,
                       session_name: str, reuse: bool, model_override: str,
                       timeout_override: int, session_mode: str,
                       reuse_legacy: bool, fingerprint_mode: bool,
                       approval_file: str, original_prompt: str,
                       decision_log: str,
                       git_baseline=None,
                       session_state: Optional[set] = None) -> tuple[Optional[str], list]:
    """gate FAIL/REJECT + gate_fix_rounds > 0: remediate up to N rounds."""
    from .gate_fix import run_gate_fix
    gate_outputs = list(stage_outputs.get(stage.name, []))
    results: list = []
    for fix_round in range(1, stage.gate_fix_rounds + 1):
        fix_result, fix_report = run_gate_fix(
            stage, gate_outputs, fix_round, validate_cmd, session_name)
        results.append(fix_result)
        if not fix_result.success:
            log.error("GATE-FIX loop stopped after round %d (fix task failed)", fix_round)
            return "FAIL", results
        gate_results, stage_ok = execute_stage(
            stage, stage_outputs, model_override, reuse, timeout_override,
            session_mode, session_name, validate_cmd, reuse_legacy,
            fingerprint_mode, approval_file, original_prompt,
            extra_evidence=[fix_report], git_baseline=git_baseline,
            session_state=session_state)
        results.extend(gate_results)
        if not stage_ok:
            if decision_log:
                _append_decision_log(
                    decision_log, f"{stage.name}#fix{fix_round}",
                    [fix_result, *gate_results], False, None)
            log.error("GATE-FIX round %d: gate stage re-execution failed tasks",
                      fix_round)
            return "FAIL", results
        verdict = _handle_gate(stage, stage_outputs, session_name)
        if verdict == "PASS" and not _git_commit_stage(
                stage, stage_outputs.get(stage.name, []), git_baseline):
            stage_ok = False
        if decision_log:
            _append_decision_log(decision_log, f"{stage.name}#fix{fix_round}",
                                 [fix_result, *gate_results], stage_ok, verdict)
        if verdict == "PASS" and stage_ok:
            log.info("GATE-FIX loop resolved stage '%s' in round %d", stage.name, fix_round)
            return verdict, results
        if verdict == "PASS":
            log.error("GATE-FIX round %d: git commit failed", fix_round)
            return "FAIL", results
    return "FAIL", results


# from_outputs assembly lives in pipeline_from_outputs for the size budget. It
# needs names defined above, so it is imported here rather than at the top;
# re-exported so existing callers and tests keep resolving them here.
from .pipeline_from_outputs import (  # noqa: E402
    _aggregate_tasks as _aggregate_tasks,
    _stage_from_outputs_tasks as _stage_from_outputs_tasks,
    _verified_note as _verified_note,
)
