"""Revocable, role-isolated intent projections; raw message bodies are never returned."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .task_center_io import checked_path, display
from .task_intent_extract import project
from .task_intent_schema import MAX_MESSAGES, validate_state
from .task_intent_store import MAX_INTENTS, intent_path
from .task_state_io import load
from .user_dirs import pbatch_runtime_home


def intent_views(sources: list, goals: list) -> tuple[list, list]:
    selected = {source["source_id"] for source in sources}
    goal_map = {goal["goal_id"]: goal for goal in goals}
    results, notes = [], []
    try:
        root = checked_path(pbatch_runtime_home() / "ops" / "intents")
        if not root.is_dir():
            return results, notes
        for index, path in enumerate(root.iterdir()):
            if index >= MAX_INTENTS * 4 or len(results) >= MAX_INTENTS:
                notes.append("prompt history observation window limited")
                break
            if path.suffix == ".json":
                _read(path, selected, goal_map, results, notes)
    except (OSError, ValueError) as exc:
        notes.append("prompt history unavailable: " + display(str(exc)))
    return results, notes


def _read(path: Path, sources: set, goals: dict, results: list, notes: list) -> None:
    try:
        value = load(path)
        if value is None:
            return
        validate_state(value)
        if path != intent_path(value["intent_id"]):
            raise ValueError("history file identity mismatch")
        if value["source_id"] in sources:
            results.append(_public(value, goals.get(value["goal_id"])))
    except (OSError, ValueError, TypeError) as exc:
        notes.append("prompt history invalid: " + display(str(exc)))


def _public(value: dict, goal: dict | None) -> dict:
    state = ("revoked" if not value["authorized"] else "goal_unavailable" if goal is None
             else "stale_contract" if goal["contract_digest"] != value["contract_digest"] else "authorized")
    analyzed = project(value) if state == "authorized" else None
    counts = dict(Counter(message["role"] for message in value["messages"]))
    return {"intent_id": value["intent_id"], "source_id": value["source_id"],
        "goal_id": value["goal_id"], "channel": display(value["channel"], 80), "version": value["version"],
        "status": state, "origin": "explicit_local_export_import", "identity": "caller_supplied_roles_not_authenticated",
        "coverage": {"messages": len(value["messages"]), "by_role": counts,
            "history_complete": value["history_complete"], "completeness_basis": "operator_export_claim_not_independent_audit",
            "message_limit": MAX_MESSAGES, "first_at": value["messages"][0]["ts"],
            "last_at": value["messages"][-1]["ts"]},
        "analysis": analyzed,
        "imports": [{"at": row["at"], "export_sha256": row["evidence"]["sha256"],
            "export_bytes": row["evidence"]["bytes"], "new_messages": len(row["new_message_ids"])}
            for row in value["imports"]],
        "accounting": {"import_operations": len(value["imports"]), "llm_requests": 0, "total_tokens": 0,
            "recorded_import_seconds": sum(row["analysis_seconds"] for row in value["imports"]),
            "retained_text_bytes": sum(len(message["text"].encode()) for message in value["messages"]),
            "cost_total": None, "cost_currency": "untracked",
            "scope": "local_history_import_and_projection_not_original_agent_usage"},
        "audit": [{key: display(row[key], 1024) if key in {"by", "reason"} else row[key]
                   for key in row} for row in value["audit"]],
        "capabilities": {"control": False, "cleanup": False, "feedback_upload": False}}


def attach_intents(tasks: list, goals: list, intents: list) -> None:
    for goal in goals:
        goal["intent_links"] = [row["intent_id"] for row in intents
            if row["goal_id"] == goal["goal_id"] and row["status"] == "authorized"]
    for task in tasks:
        linked = {goal["goal_id"] for goal in task["goal_links"]}
        task["intent_links"] = [row["intent_id"] for row in intents
            if row["goal_id"] in linked and row["status"] == "authorized"]
