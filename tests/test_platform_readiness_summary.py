import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples/snaplink-platform/scripts/check-readiness-summary.py"


def _checker():
    spec = importlib.util.spec_from_file_location("readiness_summary_checker", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _summary():
    return {
        "change_id": "COMPOSE-2026-018",
        "primary_project": "sverpweb-stable",
        "status": "READY_FOR_INDEPENDENT_GATE",
        "blockers": [],
        "persistence": {
            "tables": ["workflow_instances"],
            "transaction_boundaries": ["business + outbox"],
            "migration_steps": ["expand"],
            "open_questions": [],
        },
        "workflow": {
            "states": ["submitted", "executed"],
            "commands": ["submit", "execute"],
            "events": ["governance.workflow.submitted@v1"],
            "approval_rules": ["separation of duties"],
            "compensation_rules": ["explicit command"],
        },
        "audit": {
            "ingest_path": "/api/v1/events:batch",
            "schema_version": "v1",
            "outbox_contract": "local durable outbox",
            "receipt_contract": "event id + digest",
            "missing_evidence": [],
        },
        "operations": {
            "failure_cases": ["outbox unavailable"],
            "runbooks": ["replay DLQ"],
            "tests": ["tenant isolation"],
            "definition_of_ready": ["approved contract"],
            "definition_of_done": ["CI green"],
        },
        "ownership": {
            "collaborating_projects": ["snaplink", "aero-id"],
            "unaffected_projects": ["aero-im"],
            "sources_of_truth": {"workflow": "sverpweb-stable"},
        },
        "risks": [],
    }


def test_valid_readiness_summary_passes():
    assert _checker().check_summary(_summary(), ROOT) == []


def test_ready_summary_with_blocker_fails_closed():
    summary = _summary()
    summary["blockers"] = [{"id": "AUDIT-1"}]
    errors = _checker().check_summary(summary, ROOT)
    assert "status=READY_FOR_INDEPENDENT_GATE requires an empty blockers list" in errors
