"""from_outputs stage assembly, split from pipeline.py for the size budget.

These three functions form one unit: collect a stage's upstream artifacts,
decide what a prior run already verified, and fan or combine that evidence
into tasks. They still call helpers owned by ``pipeline`` (``_task_prompt``,
``_expected_fp``, ``_maybe_reuse_task`` and friends), so ``pipeline`` imports
this module from its bottom, after those names exist — the same arrangement
``campaign_exec`` uses for ``campaign_cli``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import pipeline_receipts as _receipts
from .config import log
from .context_inject import prepare_task_context
from .evidence import combine_outputs, source_manifest
from .models import Stage, Task
from .pipeline import (_collect_upstream, _expected_fp, _maybe_reuse_task,
                       _task_from_def, _task_prompt)


def _verified_note(stage: Stage, prev_outputs: list, reuse: bool) -> str:
    """Cite what a prior run verified, but only under --reuse.

    Gating on --reuse keeps a plain run's prompts byte-identical to before this
    existed. Under --reuse the stage is being asked to rebuild something a prior
    run already had validated, and citing those artifacts is the difference
    between re-deriving them and building on them. The block is byte-stable
    while upstream is unchanged, so it cannot invalidate the downstream reuse
    fingerprint it rides along with.
    """
    return _receipts.verified_knowledge(stage.cwd or "", prev_outputs) if reuse else ""


def _stage_from_outputs_tasks(stage: Stage, stage_outputs: dict, reuse: bool, model_override: str, timeout_override: int,
                              validate_cmd: str = "", reuse_legacy: bool = False,
                              fingerprint_mode: bool = False,
                              original_prompt: str = "",
                              extra_evidence: Optional[list[str]] = None) -> tuple[list[Task], list[str]]:
    """Tasks fed by upstream artifacts (aggregate merges them; extra_evidence
    appends gate-fix reports)."""
    names, prev_outputs, missing = _collect_upstream(stage.from_outputs, stage_outputs)
    if missing:
        log.error("Previous stage(s) not found: %s", ", ".join(missing))
        return [], []
    if extra_evidence:
        prev_outputs = list(prev_outputs) + [p for p in extra_evidence if Path(p).exists()]
    verified = _verified_note(stage, prev_outputs, reuse)
    if stage.aggregate:
        return _aggregate_tasks(stage, prev_outputs, model_override, timeout_override, reuse,
                                validate_cmd, reuse_legacy, fingerprint_mode, original_prompt,
                                verified_note=verified)
    tasks, reused = _fanned_output_tasks(
        stage, prev_outputs, verified, model_override, timeout_override,
        validate_cmd, reuse_legacy, fingerprint_mode, original_prompt, reuse)
    log.info("Loaded %d tasks from %d outputs of stage(s) '%s'",
             len(tasks), len(prev_outputs), ", ".join(names))
    return tasks, reused


def _fanned_output_tasks(stage: Stage, prev_outputs: list, verified: str,
                         model_override: str, timeout_override: int,
                         validate_cmd: str, reuse_legacy: bool,
                         fingerprint_mode: bool, original_prompt: str,
                         reuse: bool) -> tuple[list[Task], list[str]]:
    """One task set per upstream artifact, each optionally reuse-checked."""
    tasks: list[Task] = []
    reused: list[str] = []
    for out_path_str in prev_outputs:
        out_path = Path(out_path_str)
        if not out_path.exists():
            log.warning("Output file not found: %s", out_path)
            continue
        input_content = combine_outputs([str(out_path)])
        input_stem = out_path.stem
        # Tasks come from templates (prompt string or template file), both with
        # {input_content}/{input_stem} placeholders.
        for task_def in stage.tasks:
            prompt = _task_prompt(task_def, input_content, input_stem, str(out_path),
                                  original_prompt)
            if verified:
                prompt = f"{verified}\n{prompt}" if prompt else verified
            if not prompt:
                continue
            output_path = task_def.get("output", "").replace("{input_stem}", input_stem)
            task = _task_from_def(task_def, prompt, output_path, model_override,
                                  timeout_override, stage)
            prepare_task_context(task, [out_path.resolve()], strict=False)
            # T9 (round-4 M6): fingerprint the task's effective inputs so
            # the sidecar written after success matches this hash.
            out_resolved = str(task.output_path_unresolved()) if task.output else ""
            effective_validate = task.validate if task.validate is not None else validate_cmd
            fp = _expected_fp(task.prompt, out_resolved, effective_validate, task.model,
                              task.provider, fingerprint_mode, task.workdir(),
                              task.effective_agent(), task.thinking, task.tools,
                              task.exclude_tools, task.no_tools)
            if _maybe_reuse_task(stage, task, out_resolved, output_path,
                                 effective_validate, reuse_legacy, fp, reuse, reused):
                continue
            tasks.append(task)
    return tasks, reused


def _aggregate_tasks(stage: Stage, prev_outputs: list, model_override: str = "", timeout_override: int = 0, reuse: bool = False,
                     validate_cmd: str = "", reuse_legacy: bool = False,
                     fingerprint_mode: bool = False,
                     original_prompt: str = "",
                     verified_note: str = "") -> tuple[list[Task], list[str]]:
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
        prompt = _task_prompt(task_def, combined, "combined", source_manifest(prev_outputs),
                              original_prompt)
        if verified_note:
            prompt = f"{verified_note}\n{prompt}" if prompt else verified_note
        if not prompt:
            continue
        output_path = task_def.get("output", "").replace("{input_stem}", "combined")
        task = _task_from_def(task_def, prompt, output_path, model_override, timeout_override, stage)
        prepare_task_context(task, prev_outputs, strict=False)
        # T9: fingerprint effective inputs (resolved output + model).
        out_resolved = str(task.output_path_unresolved()) if task.output else ""
        effective_validate = task.validate if task.validate is not None else validate_cmd
        fp = _expected_fp(task.prompt, out_resolved, effective_validate, task.model,
                          task.provider, fingerprint_mode, task.workdir(),
                          task.effective_agent(), task.thinking, task.tools,
                          task.exclude_tools, task.no_tools)
        if _maybe_reuse_task(stage, task, out_resolved, output_path,
                              effective_validate, reuse_legacy, fp, reuse, reused):
            continue
        tasks.append(task)
    return tasks, reused
