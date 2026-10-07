"""Windowed pipeline/goal consumption via explicit run lineage, never title matching.

An accounting scope can overlap another scope. Totals across such scopes must
not be added or divided into fictitious goal costs. Acceptance remains separate.
"""
from __future__ import annotations

from collections import defaultdict

from .task_billing import TERMINAL, billing_metrics, execution_rows
from .task_center_metrics import number, usage_metrics
from .task_lifecycle import RUN_PATTERN

MAX_ANCESTORS = 16
MAX_REFS = 32
MAX_STAGE_BINS = 128


def _run_key(source: str, run: str):
    return (source, run) if source and isinstance(run, str) and RUN_PATTERN.fullmatch(run) else None


class ConsumptionIndex:
    def __init__(self, candidates: list, sources: list):
        self.parents, self.agents = {}, []
        self.partial = {source["source_id"]: any(row["truncated"] or row["malformed"] or row["error"]
            or row["incomplete"] for row in source["coverage"]) for source in sources}
        for _, _, source, rows, partial in candidates:
            source_id, last = source["source_id"], rows[-1]
            self.partial[source_id] = self.partial.get(source_id, False) or partial
            if last["kind"] == "pipeline":
                self.parents[(source_id, last["run_id"])] = _run_key(
                    last.get("parent_source_id"), last.get("parent_run_id"))
            elif last["kind"] == "agent":
                self.agents.append((source_id, last["key"], rows))
        self.ancestors = {}
        for source_id, key, rows in self.agents:
            parent = _run_key(rows[-1].get("parent_source_id"), rows[-1].get("parent_run_id"))
            self.ancestors[(source_id, key)] = self._walk(parent)

    def _walk(self, parent):
        result, seen = [], set()
        while parent:
            if parent in seen or len(seen) >= MAX_ANCESTORS:
                return [], "cyclic_or_depth_limited_lineage"
            seen.add(parent)
            result.append(parent)
            if parent not in self.parents:
                return result, "ancestor_unavailable_in_window"
            parent = self.parents[parent]
        return result, "observed_lineage" if result else "untracked"

    def select(self, roots: set) -> tuple[list, list]:
        selected, issues = [], set()
        for source_id, key, rows in self.agents:
            ancestors, status = self.ancestors[(source_id, key)]
            if roots.intersection(ancestors):
                selected.append((source_id, key, rows))
                if status != "observed_lineage":
                    issues.add(status)
        return selected, sorted(issues)

    def inspection(self, roots: set) -> dict:
        """Bounded child facts, not liveness, ownership or complete resource accounting."""
        selected, issues = self.select(roots)
        sources = {source for source, _ in roots} | {source for source, _, _ in selected}
        active = sum(row["phase"] == "execution" and row["status"] in {"running", "queued", "retrying"}
                     for _, _, rows in selected for row in rows)
        refs = ["tc-" + key for _, key, _ in selected]
        return {"basis": "explicit_lineage_in_bounded_admitted_feeds_not_live_processes",
                "active_execution_observations": active, "observed_child_tasks": len(selected),
                "lineage_diagnostics": issues,
                "unattributed_lineage_observations_in_scoped_sources": self.unattributed(sources),
                "unresolved_lineage_observations_in_scoped_sources": sum(status != "observed_lineage"
                    for (source, _), (_, status) in self.ancestors.items() if source in sources),
                "child_task_refs": refs[:8], "omitted_child_refs": max(0, len(refs) - 8),
                "coverage": "partial_window" if any(self.partial.get(source, False) for source in sources)
                            else "observed_rows_only", "liveness": "not_verified"}

    def summary(self, roots: set, shared: set | None = None) -> dict:
        selected, issues = self.select(roots)
        rows = [row for _, _, records in selected for row in records]
        scoped_sources = {source for source, _ in roots} | {source for source, _, _ in selected}
        partial = any(self.partial.get(source, False) for source in scoped_sources)
        executions = execution_rows(rows)
        active = sum(row["phase"] == "execution" and row["status"] in {
            "running", "queued", "retrying"} for row in rows)
        stages = defaultdict(list)
        for row in rows:
            stages[(row["stage"], row.get("role", ""))].append(row)
        refs = ["tc-" + key for _, key, _ in selected]
        bins = sorted(stages.items())
        return {"scope": "explicit_run_lineage_in_bounded_feeds", "window_only": True,
            "lifetime_complete": False, "allocation": "inclusive_not_exclusive_or_apportioned",
            "bound_runs": len(roots), "observed_child_tasks": len(selected),
            "usage": _usage(rows, partial), "billing": billing_metrics(rows, partial),
            "outcomes": _outcomes(rows), "active_execution_observations": active,
            "shared_execution_observations": sum(row.get("record_ref") in (shared or set()) for row in executions),
            "stages": [{"stage": stage or None, "role": role or None,
                "usage": _usage(records, partial), "billing": billing_metrics(records, partial),
                "outcomes": _outcomes(records)} for (stage, role), records in bins[:MAX_STAGE_BINS]],
            "omitted_stage_bins": max(0, len(bins) - MAX_STAGE_BINS),
            "observed_work_seconds": _work(rows), "waiting_seconds": None,
            "child_task_refs": refs[:MAX_REFS], "omitted_child_refs": max(0, len(refs) - MAX_REFS),
            "lineage_diagnostics": issues,
            "unattributed_lineage_observations_in_scoped_sources": self.unattributed(scoped_sources),
            "coverage": "partial_window" if partial else "observed_rows_only",
            "note": "no child observations means unknown usage, not free work; executed is not accepted"}

    def unattributed(self, sources: set) -> int:
        return sum(status in {"cyclic_or_depth_limited_lineage", "untracked"}
                   for (source, _), (_, status) in self.ancestors.items() if source in sources)


def _usage(rows: list, partial: bool) -> dict:
    result = usage_metrics(rows, partial)
    if not execution_rows(rows):
        result["calls"] = None
    return result


def _outcomes(rows: list) -> dict:
    executions = execution_rows(rows)
    validation = [row for row in rows if row["phase"] == "validation" and row["status"] in TERMINAL]
    return {"completed_execution_observations": len(executions) if executions else None,
        "successful_execution_observations": sum(row["status"] == "succeeded" for row in executions) if executions else None,
        "failed_execution_observations": sum(row["status"] != "succeeded" for row in executions) if executions else None,
        "retry_execution_observations": sum(row["attempt"] > 1 for row in executions) if executions else None,
        "validation_observations": len(validation) if validation else None,
        "failed_validation_observations": sum(row["status"] != "succeeded" for row in validation) if validation else None,
        "independently_accepted_goals": None, "quality_ranking": "not_performed"}


def _work(rows: list):
    values = [value for row in execution_rows(rows) if (value := number(row.get("elapsed"))) is not None]
    return sum(values) if values else None


def attach_consumption(tasks: list, goals: list, intents: list, candidates: list, sources: list) -> ConsumptionIndex:
    index = ConsumptionIndex(candidates, sources)
    goal_roots = {goal["goal_id"]: {(goal["source_id"], run["run_id"]) for run in goal["runs"]}
                  for goal in goals}
    ownership = defaultdict(set)
    for goal_id, roots in goal_roots.items():
        for _, _, rows in index.select(roots)[0]:
            for row in execution_rows(rows):
                ownership[row["record_ref"]].add(goal_id)
    shared = {reference for reference, owners in ownership.items() if len(owners) > 1}
    for goal in goals:
        summary = index.summary(goal_roots[goal["goal_id"]], shared)
        summary["contract_scope"] = "all_retained_run_bindings_across_goal_revisions"
        summary["monitoring"] = _monitoring(goal, intents)
        goal["consumption"] = summary
        _link_descendants(tasks, goal, index, goal_roots[goal["goal_id"]])
    for task in tasks:
        if task["kind"] == "pipeline":
            task["consumption"] = index.summary({(task["source_id"], task["run_id"])})
        elif task["kind"] == "direction":
            task["consumption"] = None  # campaign->pipeline attribution is not manufactured
    return index  # reuse this admitted index; inspections add no reads or feed discovery


def _link_descendants(tasks: list, goal: dict, index, roots: set) -> None:
    ids = {"tc-" + key for _, key, _ in index.select(roots)[0]}
    for task in tasks:
        if task["task_id"] in ids and not any(link["goal_id"] == goal["goal_id"] for link in task["goal_links"]):
            task["goal_links"].append({key: goal[key] for key in ("goal_id", "statement", "acceptance")})


def _monitoring(goal: dict, intents: list) -> dict:
    rows = [row for row in intents if row["goal_id"] == goal["goal_id"]
            and row["source_id"] == goal["source_id"]]
    return {"history_import_operations": sum(row["accounting"]["import_operations"] for row in rows),
        "recorded_import_seconds": sum(row["accounting"]["recorded_import_seconds"] for row in rows),
        "llm_requests": 0, "total_tokens": 0, "cost_total": None,
        "scope": "recorded_local_intent_imports_only_not_original_agent_or_all_monitoring",
        "projection_count": None, "projection_lifetime_compute_seconds": None,
        "note": "deterministic imports invoke no model; polling compute/currency not billed or allocated"}
