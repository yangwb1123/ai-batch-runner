"""Meta-stage dynamic role orchestration (extracted from pipeline.py when it
hit the 1000-line quality budget — round-4 governance close-out).

The orchestrator analyses the current deliverables and picks the review
roles that still add value: named roles resolve against role_dir templates,
ad-hoc roles carry their own task description. Role tasks run concurrently
in their own agent sessions, their deliverables fold back into the evidence,
and the loop repeats until the orchestrator reports no more roles or
max_iterations is reached. Orchestrator output is untrusted: role names are
sanitized, template lookups stay inside role_dir, and the plan parser is
bracket- and string-aware (a ']' inside a quoted string does not close the
array).
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from .config import AGENT_DEFAULT_WORKERS, _resolve_validators, log
from .models import Stage, Task
from .runner import _save_validated, run_task


_DEFAULT_META_PROMPT = """Analyze the deliverables below and decide which expert review roles are still needed to harden them.

Available roles: {roles}

Rules:
- Only choose roles that add real value for this deliverable set.
- Prefer a role name from the list above.
- When no listed role fits, define an ad-hoc role as a JSON object
  {{\"role\": \"<name>\", \"task\": \"<assignment for this reviewer>\"}}.
- Output ONLY a JSON array of role names and/or role objects, e.g.
  ["security_engineer", {{\"role\": \"perf_reviewer\", \"task\": \"Analyze performance bottlenecks\"}}].
- Output [] when the deliverables are complete.

Deliverables:
{input_content}
"""


def _available_roles(role_dir: str) -> list:
    """List role template names (file stems) inside role_dir; empty when the
    dir is missing (meta stages may still run with ad-hoc roles only)."""
    base = Path(role_dir).resolve()
    if not base.is_dir():
        return []
    return sorted(p.stem for p in base.glob("*.md") if p.stem != "README")


def _load_role_template(role_dir: str, role: str) -> Optional[str]:
    """Load a role template by name, refusing anything outside role_dir (the
    orchestrator output is untrusted input). Confinement is enforced with
    relative_to() — a string-prefix check let a sibling dir whose name
    shares role_dir's prefix escape the boundary (round-4 finding H2) — and
    resolve() refuses symlinks that point outside role_dir."""
    base = Path(role_dir).resolve()
    if not base.is_dir():
        return None
    target = (base / (role + ".md")).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        log.warning("Unknown role template: %s (must be a .md file inside %s)", role, role_dir)
        return None
    if not target.is_file():
        log.warning("Unknown role template: %s (must be a .md file inside %s)", role, role_dir)
        return None
    return target.read_text(encoding="utf-8")


def _extract_json_array(text: str) -> str:
    """Return the first JSON array literal in *text* (from its '[' to the
    MATCHING ']'), tolerating prose before it. The scan is bracket- and
    string-aware: a ']' inside a quoted string does not close the array —
    the old regex truncated ad-hoc role task text at the first ']'
    (round-4 finding M5/META1)."""
    start = (text or "").find("[")
    if start == -1:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _parse_role_plan(stdout: str) -> list:
    """Parse the orchestrator plan from its JSON output, tolerating prose and
    markdown fences. Each plan item is either a role name (string, resolved
    against role_dir) or an ad-hoc role {"role": ..., "task": ...}. Unparseable
    output -> [] (treat as 'no more roles needed')."""
    arr = _extract_json_array(stdout)
    if arr:
        try:
            data = json.loads(arr)
            if isinstance(data, list):
                plan = []
                for item in data:
                    if isinstance(item, str) and item.strip():
                        plan.append({"role": item.strip(), "task": ""})
                    elif isinstance(item, dict) and str(item.get("role", "")).strip():
                        plan.append({"role": str(item["role"]).strip(),
                                     "task": str(item.get("task", "")).strip()})
                return plan
        except json.JSONDecodeError:
            pass
    log.warning("META orchestrator output did not contain a JSON role plan; treating as complete")
    return []


def _run_meta_stage(stage: Stage, stage_outputs: dict, model_override: str = "", timeout_override: int = 0,
                    validate_cmd: str = "", session_name: str = "") -> tuple[list[TaskResult], bool]:
    """Dynamic role orchestration: ask the agent which roles the current
    deliverables still need, execute each chosen role against the aggregated
    inputs, fold the role deliverables back into the evidence, and iterate
    until the orchestrator reports no more roles or max_iterations is
    reached (self-optimizing: the role set is discovered at run time)."""
    if stage.from_outputs not in stage_outputs:
        log.error("Previous stage '%s' not found", stage.from_outputs)
        return [], False
    if not stage.output_dir:
        log.error("Meta stage '%s' requires output_dir for role deliverables", stage.name)
        return [], False

    combined = combine_outputs(stage_outputs[stage.from_outputs])
    if not combined:
        log.warning("No upstream outputs available for meta stage '%s'", stage.name)
        return [], True

    role_names = _available_roles(stage.role_dir)
    out_dir = Path(stage.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_results: list[TaskResult] = []
    role_outputs: list[str] = []
    for iteration in range(1, stage.max_iterations + 1):
        done, ok, combined = _run_meta_iteration(stage, combined, role_names, model_override, timeout_override,
                                                 validate_cmd, iteration, all_results, role_outputs, session_name)
        if done or not ok:
            break

    stage_outputs[stage.name] = role_outputs
    return all_results, all(r.success for r in all_results)



def combine_outputs(prev_outputs: list) -> str:
    """Concatenate upstream artifacts into one evidence block (T12d: fenced
    as untrusted evidence so downstream roles treat it as claims to verify,
    not ground truth)."""
    from .config import log as _log
    parts = []
    for out_path_str in prev_outputs:
        out_path = Path(out_path_str)
        if not out_path.exists():
            _log.warning("Output file not found: %s", out_path)
            continue
        parts.append("<evidence source=\"%s\">\n%s\n</evidence>" %
                     (out_path.stem, out_path.read_text(encoding="utf-8")))
    return "\n\n".join(parts)


def _run_meta_iteration(stage: Stage, combined: str, role_names: list, model_override: str, timeout_override: int,
                        validate_cmd: str, iteration: int, all_results: list, role_outputs: list,
                        session_name: str = "") -> tuple[bool, bool, str]:
    """One orchestrator round: ask which roles are needed, run them
    concurrently (each in its own session), fold deliverables back into the
    evidence (returned as the new combined context). Returns (done, ok,
    combined): done=True stops the loop (orchestrator failure, no roles
    left, or role failures)."""
    log.info("META iteration %d/%d for stage '%s' (roles available: %s)",
             iteration, stage.max_iterations, stage.name, ", ".join(role_names) or "(none)")
    meta_task = _build_meta_prompt_task(stage, role_names, combined, model_override, timeout_override)
    meta_result = run_task(meta_task, session_name=session_name)
    if not meta_result.success:
        log.warning("META orchestrator call failed: %s; stopping role expansion", meta_result.reason)
        return True, False, combined
    roles = _parse_role_plan(meta_result.stdout)
    if not roles:
        log.info("META orchestrator: no more roles needed (iteration %d)", iteration)
        return True, True, combined
    log.info("META orchestrator selected %d role(s)", len(roles))

    role_tasks, iteration_ok = _build_role_tasks(stage, roles, combined, model_override, timeout_override)

    def _run_role(item):
        role, task = item
        result = run_task(task, parallel=True, session_name=session_name)
        if result.success:
            result.success = _save_validated(task, result, validate_cmd)
            if not result.success:
                result.reason = "validation failed"
        return role, result

    with ThreadPoolExecutor(max_workers=max(1, stage.workers or config.AGENT_DEFAULT_WORKERS)) as pool:
        futures = [pool.submit(_run_role, item) for item in role_tasks]
        for fut in as_completed(futures):
            role, result = fut.result()
            all_results.append(result)
            if not result.success:
                iteration_ok = False
            if result.task.output:
                role_outputs.append(str(result.task.output_path()))
                # fold the role deliverable back into the evidence for
                # the next orchestrator round (self-optimization loop)
                out_path = Path(result.task.output)
                if out_path.exists():
                    combined += f"\n\n--- {role} deliverable ---\n" + out_path.read_text(encoding="utf-8")
    if not iteration_ok:
        log.warning("META stage '%s': some role tasks failed in iteration %d", stage.name, iteration)
        return True, False, combined
    return False, True, combined


def _build_meta_prompt_task(stage: Stage, role_names: list, combined: str, model_override: str, timeout_override: int) -> Task:
    """Assemble the orchestrator prompt (custom meta_prompt or the default,
    with {roles}/{input_content} placeholders) as a task."""
    meta_prompt = (stage.meta_prompt or _DEFAULT_META_PROMPT)
    meta_prompt = meta_prompt.replace("{roles}", ", ".join(role_names) or "(none - define ad-hoc roles)")
    meta_prompt = meta_prompt.replace("{input_content}", combined)
    task = Task(prompt=meta_prompt)
    task.model = stage.model or task.model
    task.provider = stage.provider or task.provider
    if model_override:
        task.model = model_override
    if timeout_override:
        task.timeout = timeout_override
    return task


def _build_role_tasks(stage: Stage, roles: list, combined: str, model_override: str, timeout_override: int) -> tuple[list, bool]:
    """Turn the orchestrator plan into tasks: ad-hoc roles ({"role",
    "task"}) get their task description plus the current context; named
    roles load the role_dir template. Output names are sanitized (the
    orchestrator output is untrusted), and sanitization collisions are
    disambiguated with a short hash so one role cannot silently overwrite
    another's deliverable (round-4 finding DS-7). Returns ([(role, Task)],
    ok); ok is False when a named role had no template."""
    role_tasks = []
    ok = True
    used_names: set = set()
    for item in roles:
        role = item["role"]
        task_desc = item["task"]
        if task_desc:
            prompt = f"{task_desc}\n\nContext (current deliverables):\n{combined}"
        else:
            template = _load_role_template(stage.role_dir, role)
            if template is None:
                ok = False
                continue
            prompt = template.replace("{input_content}", combined).replace("{input_stem}", stage.name)
        safe_name = re.sub(r"[^A-Za-z0-9_-]", "_", role) or "role"
        if safe_name in used_names:
            # security/engineer vs security.engineer both sanitize to
            # security_engineer: disambiguate instead of last-writer-wins
            suffix = hashlib.sha1(role.encode("utf-8", errors="replace")).hexdigest()[:8]
            safe_name = f"{safe_name}-{suffix}"
        used_names.add(safe_name)
        out_path = Path(stage.output_dir) / f"{safe_name}.md"
        task = Task(prompt=prompt, output=str(out_path))
        if model_override:
            task.model = model_override
        if timeout_override:
            task.timeout = timeout_override
        role_tasks.append((role, task))
    return role_tasks, ok



def run_meta_stage(stage: Stage, stage_outputs: dict, model_override: str = "", timeout_override: int = 0,
                   validate_cmd: str = "", session_name: str = "") -> tuple[list, bool]:
    """Public entry: dynamic role orchestration (see module docstring)."""
    return _run_meta_stage(stage, stage_outputs, model_override, timeout_override, validate_cmd, session_name)
