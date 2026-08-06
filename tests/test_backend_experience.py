"""Backend Experience Intelligence gate tests: checker behavior + registry."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check-backend-experience.py"


@pytest.fixture(scope="module")
def cdi():
    spec = importlib.util.spec_from_file_location("cbe", CHECKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _check(cdi, name: str, source: str):
    return cdi._check_file(Path(name), source)


class TestStateMachine:
    def test_status_literals_without_set(self, cdi):
        src = "def update(o): o.status = 'active'; o.status = 'cancelled'"
        kinds = [v["check"] for v in _check(cdi, "orders/service.py", src)]
        assert "status_literals_without_state_set" in kinds

    def test_enum_status_ok(self, cdi):
        src = "class OrderStatus: ACTIVE = 'active'; CANCELLED = 'cancelled'"
        assert _check(cdi, "orders/status.py", src) == []

    def test_async_states_exempt(self, cdi):
        src = "job.Status = 'queued'; job.Status = 'processing'"
        assert _check(cdi, "files/upload.go", src) == []


class TestAudit:
    def test_write_without_audit(self, cdi):
        src = "func (r *Repo) Update(o Order) { db.Update(o) }"
        kinds = [v["check"] for v in _check(cdi, "orders/repo.go", src)]
        assert "write_without_audit_signal" in kinds

    def test_write_with_audit_ok(self, cdi):
        src = "db.Update(o); r.audit.Log(o)"
        assert _check(cdi, "orders/repo.go", src) == []


class TestAsyncLifecycle:
    def test_long_op_without_async(self, cdi):
        src = "func Upload(f File) { scan(f); ocr(f); index(f); return }"
        kinds = [v["check"] for v in _check(cdi, "files/upload.go", src)]
        assert "long_op_without_async_lifecycle" in kinds

    def test_long_op_with_queue_ok(self, cdi):
        src = "q.Enqueue(job{Id: newId(), Status: 'queued'})"
        assert _check(cdi, "files/upload.go", src) == []


class TestSearchContext:
    def test_search_without_context(self, cdi):
        src = "func Search(q string) []Customer { return rows }"
        kinds = [v["check"] for v in _check(cdi, "search/customers.go", src)]
        assert "search_endpoint_without_context" in kinds

    def test_search_with_context_ok(self, cdi):
        src = (
            "func Search(q string) CustomerView { return CustomerView{"
            "Customer: c, Context: OrderSummary{Orders: 238}} }"
        )
        assert _check(cdi, "search/customers.go", src) == []


class TestInfraSkip:
    def test_migration_skipped(self, cdi):
        src = "UPDATE orders SET status='active'"
        assert _check(cdi, "db/migration_001.sql", src) == []


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
    def test_backend_standard_gets_experience_rules(self):
        sys.path.insert(0, str(ROOT))
        from pbatch.rule_matcher import match_rules

        r = match_rules("写一个订单创建接口，含支付审批，生产，go")
        ids = [x["id"] for x in r["rules"]]
        di = [i for i in ids if i.startswith("design-intelligence")]
        assert {
            "design-intelligence-api",
            "design-intelligence-datamodel",
        } <= set(di)
        assert "design-intelligence-arch" in ids
        assert "design-intelligence-pipeline" in ids
