"""Fail-closed parallel output planning and wave scheduling adapters.

The plain parallel runner must reject output races before creating an agent
pool.  The optional wave plan reuses :mod:`pbatch.waves` while keeping the
batch ``Task`` model deliberately small: tasks declare only their resolved
runner-managed output as a write set.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .fabric_sched import TaskNode
from .models import Task
from .waves import plan_waves


class OutputCollisionError(ValueError):
    """Invalid parallel output plan with a structured collision report."""

    def __init__(self, groups: list[dict], reason: str = "parallel output collision"):
        self.groups = groups
        self.reason = reason
        super().__init__(self._render())

    def _render(self) -> str:
        lines = [self.reason]
        for group in self.groups:
            indexes = ", ".join(str(i) for i in group["indices"])
            why = "; ".join(group.get("reasons", []))
            lines.append(f"  tasks [{indexes}] {why}".rstrip())
            for item in group["tasks"]:
                lines.append(
                    f"    task[{item['index']}] prompt={item['prompt']!r} "
                    f"output={item['output'] or '(stdout)'} cwd={item['cwd']}"
                )
        return "\n".join(lines)


def _location(index: int, task: Task) -> dict:
    """Capture the stable paths and bounded prompt evidence for one task."""
    output = task.output_path()
    return {"index": index, "prompt": (task.prompt or "")[:60],
            "output": str(output) if output else "",
            "cwd": str(Path(task.workdir()).resolve())}


def _inside(path: Path, parent: Path) -> bool:
    """Whether *path* is inside *parent*, including equality."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _pair_reason(first: dict, second: dict) -> str:
    """Return the first applicable collision rule for two locations."""
    output_a, output_b = Path(first["output"]), Path(second["output"])
    if output_a == output_b:
        return "same resolved output"
    cwd_a, cwd_b = Path(first["cwd"]), Path(second["cwd"])
    if cwd_a != cwd_b and (_inside(output_a, cwd_b)
                           or _inside(output_b, cwd_a)):
        return "output inside another task cwd"
    if output_a in output_b.parents or output_b in output_a.parents:
        return "nested output paths"
    return ""


def _collision_edges(locations: list[dict]) -> tuple[dict, dict]:
    """Build an undirected collision graph and retain each pair's reason."""
    edges = {item["index"]: set() for item in locations}
    reasons = {}
    for left, first in enumerate(locations):
        for second in locations[left + 1:]:
            reason = _pair_reason(first, second)
            if reason:
                a, b = first["index"], second["index"]
                edges[a].add(b)
                edges[b].add(a)
                reasons[tuple(sorted((a, b)))] = reason
    return edges, reasons


def _groups(locations: list[dict], edges: dict, reasons: dict) -> list[dict]:
    """Turn collision edges into deterministic connected-component reports."""
    by_index = {item["index"]: item for item in locations}
    visited, groups = set(), []
    for start in sorted(edges):
        if start in visited or not edges[start]:
            continue
        stack, members = [start], []
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            members.append(current)
            stack.extend(edges[current] - visited)
        members.sort()
        pair_reasons = [reason for pair, reason in sorted(reasons.items())
                        if set(pair) <= set(members)]
        groups.append({"indices": members,
                       "reasons": sorted(set(pair_reasons)),
                       "tasks": [by_index[index] for index in members]})
    return groups


def detect_collisions(tasks: list[Task], indices: Optional[list[int]] = None) -> list[dict]:
    """Return connected collision groups using O(n²) resolved-path checks."""
    indexes = indices if indices is not None else list(range(len(tasks)))
    locations = [_location(index, task) for index, task in zip(indexes, tasks)
                 if task.output_path() is not None]
    edges, reasons = _collision_edges(locations)
    return _groups(locations, edges, reasons)


def _invalid_plan(reason: str) -> OutputCollisionError:
    return OutputCollisionError([], f"invalid parallel wave plan: {reason}")


def _validate_coverage(tasks: list[Task], waves: list) -> None:
    """Require every task index exactly once, preserving string node ids."""
    if not isinstance(waves, list):
        raise _invalid_plan("plan must be a list of waves")
    flat = []
    for wave in waves:
        if not isinstance(wave, list):
            raise _invalid_plan("each wave must be a list of string task ids")
        if any(not isinstance(node_id, str) for node_id in wave):
            raise _invalid_plan("task ids must be strings")
        flat.extend(wave)
    expected = {str(index) for index in range(len(tasks))}
    if len(flat) != len(expected) or set(flat) != expected:
        raise _invalid_plan("task ids must cover each task exactly once")
    if len(set(flat)) != len(flat):
        raise _invalid_plan("task ids must not be duplicated")


def validate_parallel_plan(tasks: list[Task], waves: Optional[list] = None):
    """Validate plain collisions or an optional intra-wave conflict plan."""
    if waves is None:
        groups = detect_collisions(tasks)
        if groups:
            raise OutputCollisionError(groups)
        return None
    if not tasks or not waves:
        return []
    _validate_coverage(tasks, waves)
    for wave in waves:
        indexes = [int(node_id) for node_id in wave]
        groups = detect_collisions([tasks[index] for index in indexes], indexes)
        if groups:
            raise OutputCollisionError(groups, "parallel wave has conflicting tasks")
    return waves


def tasks_to_nodes(tasks: list[Task]) -> list[TaskNode]:
    """Map batch tasks to the write-only nodes consumed by ``plan_waves``."""
    nodes = []
    for index, task in enumerate(tasks):
        output = task.output_path()
        nodes.append(TaskNode(
            id=str(index), goal=(task.prompt or "")[:60], argv=[],
            write_set=[str(output)] if output is not None else []))
    return nodes


def plan_waves_for(tasks: list[Task]) -> list[list[str]]:
    """Return a directly feedable wave plan for a batch task list."""
    return plan_waves(tasks_to_nodes(tasks))
