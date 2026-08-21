"""Organization flow execution, artifacts, and lesson rendering.

This module keeps the stateful workflow/reporting concerns separate from the
organization schema and CLI adapter in :mod:`pbatch.org`.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Callable

from .impact import change_impact


# Work level -> ordered gate nodes (incremental for L0/L1, full review for
# L2/L3).  Kept public through pbatch.org for backwards compatibility.
LEVEL_NODES = {
    "L0": ["requirement", "developer", "testing"],
    "L1": ["requirement", "developer", "testing", "code_review"],
    "L2": ["requirement", "domain", "architecture", "data", "planning",
           "developer", "code_review", "security", "testing"],
    "L3": ["orchestrator", "requirement", "product", "ux_designer",
           "domain", "architecture", "data", "api", "planning",
           "developer", "code_review", "security", "testing",
           "performance", "release", "operation", "evolution"],
}


_FRONTEND_HINTS = ("前端", "页面", "按钮", "界面", "react", "vue", "flutter",
                   "tsx", "jsx", "css", "ui")


def _flow_nodes(level: str, task: str) -> list:
    """Return the level's nodes, adding UX review for frontend work.

    UX is intentionally conditional for L1/L2 so a backend change does not
    pay for an irrelevant visual gate; L3 includes every organization node.
    """
    nodes = list(LEVEL_NODES.get(level, LEVEL_NODES["L1"]))
    frontend = any(term in (task or "").casefold() for term in _FRONTEND_HINTS)
    if level in {"L1", "L2"} and frontend and "ux_designer" not in nodes:
        insert_at = nodes.index("requirement") + 1
        nodes.insert(insert_at, "ux_designer")
    return nodes


def run_flow(task: str, code_dir: str, level: str, file_path: str,
             artifact_dir: str, log_path: str,
             gate_runner: Callable[..., dict]) -> dict:
    """Route by impact and execute the ordered organization gate nodes."""
    impact = change_impact(task, code_dir)
    selected_level = level or impact["work_level"]
    nodes = _flow_nodes(selected_level, task)
    results = []
    for node_id in nodes:
        gate = gate_runner(node_id, task, code_dir, file_path, log_path)
        if "error" in gate:
            results.append(_failed_gate(node_id, gate["error"]))
            continue
        results.append(_gate_summary(node_id, gate))
        if artifact_dir:
            write_artifact(artifact_dir, node_id, gate)
    return {
        "task": impact["task"], "impact_level": impact["work_level"],
        "change_cost": impact["change_cost"],
        "run_level": selected_level,
        "required_agents": impact["required_agents"],
        "artifact_dir": artifact_dir or "",
        "nodes": results,
        "flow_passed": all(result["gate_passed"] for result in results),
    }


def _failed_gate(node_id: str, error: str) -> dict:
    return {"node": node_id, "passed": 0, "failed": 1, "skipped": 0,
            "attention": 0, "gate_passed": False, "error": error}


def _gate_summary(node_id: str, gate: dict) -> dict:
    return {"node": node_id, "passed": gate["passed"],
            "failed": gate["failed"], "skipped": gate["skipped"],
            "attention": gate["attention"],
            "gate_passed": gate["gate_passed"]}


def write_artifact(artifact_dir: str, node_id: str, gate: dict) -> None:
    """Persist a bounded evidence excerpt for a completed node gate."""
    target = Path(artifact_dir)
    target.mkdir(parents=True, exist_ok=True)
    lines = [f"# node: {node_id}", f"passed: {gate['passed']}",
             f"failed: {gate['failed']} | skipped: {gate['skipped']} | "
             f"attention: {gate['attention']}", ""]
    for result in gate["results"]:
        lines.append(f"[{result['status']}] {result['check']}")
        if result.get("evidence"):
            lines.append(f"    {result['evidence'][:150]}")
    (target / f"{node_id}.txt").write_text("\n".join(lines) + "\n",
                                           encoding="utf-8")


def save_flow(report: dict, flows_dir: Path) -> str:
    """Persist an auditable flow report below ``flows_dir``."""
    slug = re.sub(r"[^a-z0-9]+", "-", (report.get("task") or "flow")
                  .lower())[:40].strip("-") or "flow"
    path = flows_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.json"
    flows_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return str(path)


def list_flows(flows_dir: Path) -> list:
    """Read flow reports newest-first, skipping unreadable entries."""
    records = []
    if not flows_dir.exists():
        return records
    for path in sorted(flows_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data["file"] = path.name
            records.append(data)
    return records


def lesson_for(node: dict) -> str:
    """Render failure cases as actionable learn/reflection commands."""
    cases = node.get("failure_cases", [])
    if not cases:
        return f"节点 {node.get('id')} 无失败案例记录"
    lines = [f"# {node.get('name')} — 失败案例与规则沉淀", ""]
    for index, case in enumerate(cases, 1):
        lines.extend(_lesson_case(node, index, case))
    return "\n".join(lines)


def _lesson_case(node: dict, index: int, case: str) -> list[str]:
    rule_id = f"ORG-{node.get('id', '').upper()[:16]}-{index:02d}"
    return [
        f"## 案例 {index}: {case}", "",
        "### 沉淀为规则草案（learn 流程）", "```bash",
        "pi-batch learn draft "
        f"'{case}' --category backend --severity warning --rule-id {rule_id}",
        "```", "", "### 复盘（retro/reflection）", "```bash",
        "pi-batch retro --failures-only logs/ --actions",
        f"pi-batch reflect --task '{case}'", "```", "",
    ]
