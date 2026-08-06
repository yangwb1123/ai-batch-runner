"""Knowledge Maintenance gate tests."""

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check-knowledge-freshness.py"


@pytest.fixture(scope="module")
def kf():
    spec = importlib.util.spec_from_file_location("kf", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _repo(kf, files: dict[str, str]) -> Path:
    """Build a temp repo from {relative_path: content}."""
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


class TestModuleDocs:
    def test_module_without_docs(self, kf):
        root = _repo(kf, {"orders/service.go": "package orders"})
        docs = [v for v in kf._check_module_docs(root) if "orders" in v["file"]]
        assert docs and docs[0]["check"] == "module_without_docs"

    def test_documented_module_ok(self, kf):
        root = _repo(
            kf,
            {
                "orders/service.go": "package orders",
                "orders/README.md": "# Orders",
            },
        )
        assert kf._check_module_docs(root) == []

    def test_skip_modules(self, kf):
        root = _repo(kf, {"internal/core.go": "package internal"})
        assert kf._check_module_docs(root, {"internal"}) == []


class TestAdr:
    def test_no_adr_dir(self, kf):
        root = _repo(kf, {"orders/service.go": "package orders"})
        checks = [v["check"] for v in kf._check_adr_presence(root)]
        assert "no_decision_records" in checks

    def test_docs_dir_ok(self, kf):
        root = _repo(
            kf,
            {
                "orders/service.go": "package orders",
                "docs/adr-001.md": "# ADR 001",
            },
        )
        assert kf._check_adr_presence(root) == []


class TestCriticalAudit:
    def test_critical_mutation_without_audit(self, kf):
        root = _repo(kf, {"finance/account.go": "balance = balance + 100"})
        checks = [v["check"] for v in kf._check_critical_number_audit(root)]
        assert "critical_number_without_audit" in checks

    def test_critical_with_audit_ok(self, kf):
        root = _repo(
            kf,
            {
                "finance/account.go": (
                    "balance = balance + 100; audit.log(entry)"
                )
            },
        )
        assert kf._check_critical_number_audit(root) == []

    def test_non_critical_ok(self, kf):
        root = _repo(kf, {"x/main.go": "name = 'abc'; count = count + 1"})
        assert kf._check_critical_number_audit(root) == []


class TestSelfcheck:
    def test_selfcheck_passes(self):
        result = subprocess.run(
            [sys.executable, str(CHECKER), "--selfcheck"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stdout
        assert "OK" in result.stdout


class TestRegistrySelection:
    def test_production_backend_gets_maintenance(self):
        sys.path.insert(0, str(ROOT))
        from pbatch.rule_matcher import match_rules

        r = match_rules("写一个订单创建接口，含支付审批，生产，go")
        ids = [x["id"] for x in r["rules"]]
        assert "design-intelligence-maintenance" in ids
