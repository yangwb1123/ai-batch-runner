"""Safe parsing helpers for meta-stage role plans."""

from __future__ import annotations

import json
import re

from .config import log

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MAX_TASK_BYTES = 8192
_MAX_DAG_TASKS = 64


def _extract_json_array(text: str) -> str:
    """Return the first JSON array literal in *text* using a
    bracket/string-aware scan."""
    start = (text or "").find("[")
    if start == -1:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        char = text[i]
        if in_str:
            if esc:
                esc = False
            elif char == "\\":
                esc = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def parse_task_dag(stdout: str, limit: int) -> tuple[list[dict], str]:
    """Parse a bounded, dependency-checked planner task DAG."""
    array = _extract_json_array(stdout)
    if not array:
        return [], "planner output is not a JSON task array"
    try:
        values = json.loads(array)
    except json.JSONDecodeError:
        return [], "planner task DAG is malformed JSON"
    if (not isinstance(values, list)
            or len(values) > min(_MAX_DAG_TASKS, max(1, limit))):
        return [], "planner task DAG exceeds the fan-out limit"
    plan = []
    seen = set()
    for value in values:
        item, error = _parse_dag_item(value)
        if error:
            return [], error
        key = item["id"].casefold()
        if key in seen:
            return [], "planner task IDs must be unique"
        seen.add(key)
        plan.append(item)
    error = validate_task_dag(plan)
    return ([], error) if error else (plan, "")


def _parse_dag_item(value: object) -> tuple[dict, str]:
    if not isinstance(value, dict) or set(value) - {
            "id", "role", "task", "depends_on"}:
        return {}, "each DAG item must contain only id, role, task, and depends_on"
    identity, role = value.get("id"), value.get("role")
    task, dependencies = value.get("task", ""), value.get("depends_on", [])
    if not isinstance(identity, str) or not _TASK_ID.fullmatch(identity):
        return {}, "DAG task id is invalid"
    if not isinstance(role, str) or not role.strip():
        return {}, "DAG task role is invalid"
    if not isinstance(task, str):
        return {}, "DAG task description is invalid or oversized"
    try:
        role_size = len(role.encode("utf-8"))
        task_size = len(task.encode("utf-8"))
    except UnicodeEncodeError:
        return {}, "DAG task text is not valid UTF-8"
    if role_size > 256 or task_size > _MAX_TASK_BYTES:
        return {}, "DAG task description is invalid or oversized"
    if (not isinstance(dependencies, list)
            or not all(isinstance(item, str) and _TASK_ID.fullmatch(item)
                       for item in dependencies)
            or len(set(dependencies)) != len(dependencies)):
        return {}, "DAG dependencies must be unique task IDs"
    return ({"id": identity, "role": role.strip(), "task": task.strip(),
             "depends_on": dependencies}, "")


def validate_task_dag(plan: list[dict]) -> str:
    """Validate references and cycles; return the first fail-closed reason."""
    ids = {item["id"] for item in plan}
    if any(item["id"] in item["depends_on"] for item in plan):
        return "DAG task cannot depend on itself"
    if any(set(item["depends_on"]) - ids for item in plan):
        return "DAG dependency references an unknown task"
    completed = set()
    remaining = list(plan)
    while remaining:
        ready = [item for item in remaining
                 if set(item["depends_on"]) <= completed]
        if not ready:
            return "planner task DAG contains a dependency cycle"
        ready_ids = {item["id"] for item in ready}
        completed.update(ready_ids)
        remaining = [item for item in remaining if item["id"] not in ready_ids]
    return ""


def task_dag_waves(plan: list[dict]) -> list[list[dict]]:
    """Return deterministic declaration-order waves for a validated DAG."""
    waves = []
    complete = set()
    remaining = list(plan)
    while remaining:
        ready = [item for item in remaining
                 if set(item["depends_on"]) <= complete]
        if not ready:
            return []
        waves.append(ready)
        complete.update(item["id"] for item in ready)
        ready_ids = {item["id"] for item in ready}
        remaining = [item for item in remaining if item["id"] not in ready_ids]
    return waves


def _parse_role_plan(stdout: str) -> list:
    """Parse the orchestrator's JSON role array; malformed output is empty."""
    arr = _extract_json_array(stdout)
    plan = []
    if arr:
        try:
            data = json.loads(arr)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str) and item.strip():
                        plan.append({"role": item.strip(), "task": ""})
                    elif isinstance(item, dict) and str(item.get("role", "")).strip():
                        plan.append({"role": str(item["role"]).strip(),
                                     "task": str(item.get("task", "")).strip()})
                return plan
        except json.JSONDecodeError:
            pass
    if not plan:
        log.warning("META orchestrator output did not contain a JSON role plan; treating as complete")
    return []
