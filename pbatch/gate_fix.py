"""Gate-fix loop: 被拒 gate 的自动修复（self-iteration round 沉淀）。

gate 阶段 VERDICT: FAIL/REJECT 且 stage.gate_fix_rounds > 0 时，runner
不再直接 halt：提取被拒理由与 gatekeeper 的结构化发现（```json 数组，
{severity,file,line,problem,fix_hint}）→ 生成修复任务 → 验证 → 把修复
报告作为额外证据重新执行 gate 阶段 → 重新裁决，最多 gate_fix_rounds 轮。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from . import config
from .config import AGENT_DEFAULT_TIMEOUT, log
from .context_inject import add_task_context_paths
from .gate_contract import (extract_findings, parse_gate_text,
                            unwrap_gate_transcript)
from .models import Stage, Task, TaskResult
from .text_io import read_text_bounded

def extract_gate_findings(text: str) -> Optional[list]:
    """Extract findings from new Gate objects or legacy JSON arrays."""
    return extract_findings(text)


def gate_fail_reason(output_paths: list) -> str:
    """Reasons from rejecting or invalid artifacts, never valid PASS output."""
    reasons: list[str] = []
    for p in output_paths:
        path = Path(p)
        if path.is_symlink():
            reasons.append(f"{path}: invalid Gate artifact: symlinks are not allowed")
            continue
        if not path.is_file():
            reasons.append(f"{path}: Gate artifact is missing")
            continue
        try:
            text = read_text_bounded(path, config.OUTPUT_MAX_BYTES, "gate artifact")
        except ValueError as exc:
            reasons.append(f"{path}: invalid Gate artifact: {exc}")
            continue
        decision = parse_gate_text(text)
        if decision.verdict in ("FAIL", "REJECT"):
            reasons.append(
                f"{path}: VERDICT: {decision.verdict} {decision.reason}")
        elif not decision.valid or not decision.verdict:
            reasons.append(
                f"{path}: invalid Gate artifact: "
                f"{decision.reason or 'no usable verdict'}")
    return "\n---\n".join(reasons)[-16000:]


def gate_fix_prompt(stage: Stage, reason: str, findings: Optional[list]) -> str:
    """Fix-task prompt: custom template ({reason}/{findings}) or default."""
    findings_text = json.dumps(findings, ensure_ascii=False, indent=2) if findings else "(none)"
    if stage.gate_fix_prompt:
        prompt = stage.gate_fix_prompt.replace("{reason}", reason).replace(
            "{findings}", findings_text)
    else:
        prompt = (
        "The gate stage '%s' rejected the deliverable(s). Read the gate verdict "
        "reason and the structured findings below, inspect the ACTUAL files "
        "cited (line numbers are hints, verify against the code), implement the "
        "fixes, add or update regression tests, and run the project's gates "
        "until green. Do NOT commit. Finish with a short change report listing "
        "every finding and how it was fixed.\n\n"
        "GATE VERDICT REASON:\n%s\n\n"
        "STRUCTURED FINDINGS (untrusted — verify against the code):\n%s"
        ) % (stage.name, reason, findings_text)
    return _bounded_prompt(prompt)


def _bounded_prompt(prompt: str) -> str:
    """Keep the complete gate-fix argv inside the configured prompt cap."""
    limit = config.PROMPT_MAX_BYTES
    raw = prompt.encode("utf-8")
    if limit <= 0 or len(raw) <= limit:
        return prompt
    marker = b"\n[gate-fix prompt truncated]\n"
    if limit <= len(marker):
        return raw[:limit].decode("utf-8", errors="ignore")
    return (raw[:limit - len(marker)].decode("utf-8", errors="ignore")
            + marker.decode("ascii"))


def _first_findings(gate_outputs: list) -> Optional[list]:
    selected: list[dict] = []
    for p in gate_outputs:
        path = Path(p)
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = read_text_bounded(
                path, config.OUTPUT_MAX_BYTES, "gate artifact")
        except ValueError:
            continue
        decision = parse_gate_text(text)
        if decision.verdict not in ("FAIL", "REJECT"):
            continue
        findings = decision.findings or extract_gate_findings(text) or []
        selected.extend(findings[:50 - len(selected)])
        if len(selected) >= 50:
            break
    return selected or None


def _normalize_fix_report(result: TaskResult) -> None:
    """Persist the final assistant report, not JSONL transport events.

    Codex command events can exceed the evidence budget before the final
    remediation summary appears. A recognized but malformed transcript is
    rejected instead of being folded into the next Gate prompt.
    """
    if not result.success:
        return
    payload, error = unwrap_gate_transcript(result.stdout)
    if error:
        result.success = False
        result.reason = f"invalid gate-fix transcript: {error}"
        return
    if payload != result.stdout:
        result.stdout = payload.rstrip() + "\n"


def run_gate_fix(stage: Stage, gate_outputs: list, fix_round: int,
                 validate_cmd: str, session_name: str) -> tuple[TaskResult, str]:
    """One gate-fix iteration: remediation task -> validate -> report path.
    The report is folded into the gate re-execution as extra evidence."""
    reason = gate_fail_reason(gate_outputs) or "(no reason extracted)"
    findings = _first_findings(gate_outputs)
    gate_dir = Path(gate_outputs[0]).parent if gate_outputs else Path(stage.cwd or os.getcwd())
    gate_dir.mkdir(parents=True, exist_ok=True)
    report = gate_dir / f"{stage.name}-fix-{fix_round}.md"
    task = Task(prompt=gate_fix_prompt(stage, reason, findings),
                output=str(report), agent=stage.agent, model=stage.model,
                provider=stage.provider, cwd=stage.cwd,
                timeout=max(AGENT_DEFAULT_TIMEOUT, 1800))
    task.memory = {"stage": f"{stage.name}-fix", "memory_mode": "execute"}
    add_task_context_paths(task, gate_outputs, strict=False)
    fix_validate = stage.gate_fix_validate if stage.gate_fix_validate is not None else validate_cmd
    if fix_validate:
        task.validate = fix_validate
    log.info("GATE-FIX round %d for stage '%s': %s", fix_round, stage.name, report)
    from .memory import record_task
    from .runner import _save_validated, run_task
    result = run_task(task, session_name=session_name)
    _normalize_fix_report(result)
    if not result.success:
        log.error("GATE-FIX round %d failed: %s", fix_round, result.reason)
    elif not _save_validated(task, result, fix_validate):
        result.success = False
        result.reason = (f"validation failed (exit={result.validation_exit})"
                         if result.validation_exit else "validation failed")
        log.error("GATE-FIX round %d: output not saved (validation failed)", fix_round)
    record_task(result)
    return result, str(report)
