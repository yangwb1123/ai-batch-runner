import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples/snaplink-platform/scripts/check-architecture-contract.py"


def _checker():
    spec = importlib.util.spec_from_file_location("architecture_contract_checker", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_phase3_mechanical_contract_is_valid_without_approval_gate():
    report = _checker().check(ROOT, mechanical_only=True)

    assert report["ok"] is True
    assert report["mechanical_ok"] is True
    assert report["approval_ready"] is False
    assert report["gate"]["ok"] is False
    assert any("approval gate pending" in warning for warning in report["warnings"])


def test_phase3_default_gate_fails_closed_until_gate_artifact_is_passed():
    report = _checker().check(ROOT, mechanical_only=False)

    assert report["ok"] is False
    assert report["approval_ready"] is False
    assert any(item.startswith("approval gate:") for item in report["errors"])
