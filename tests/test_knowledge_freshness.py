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


_PATH_PREFIX = "from pathlib import Path\n"
_PATH_BINDING = "root = Path(__file__).resolve().parent\ninventory = root.parent / 'manifest.json'\n"


@pytest.mark.parametrize("source", [
    _PATH_PREFIX + "inventory = Path('manifest.json')\ninventory.read_text()\n",
    "from pathlib import Path as P\ninventory = P('manifest.json')\ninventory.read_bytes()\n",
    "import pathlib as paths\ninventory = paths.Path('manifest.json')\ninventory.read_text()\n",
    _PATH_PREFIX + _PATH_BINDING + "inventory.read_text()\ninventory.read_bytes()\n",
    _PATH_PREFIX + "库存 = Path('清单.json')\n库存.read_text(encoding='utf-8')\n",
    _PATH_PREFIX + "label = '金额'; inventory = Path('manifest.json'); inventory.read_text()\n",
])
def test_readonly_path_binding_is_not_a_number(kf, tmp_path, source):
    (tmp_path / "probe.py").write_text(source, encoding="utf-8")
    assert kf._check_critical_number_audit(tmp_path) == []


@pytest.mark.parametrize("source", [
    _PATH_PREFIX + _PATH_BINDING + "inventory.read_text()\ninventory = 5\n",
    _PATH_PREFIX + _PATH_BINDING + "inventory.read_text()\ninventory -= 1\n",
    _PATH_PREFIX + _PATH_BINDING + "inventory.read_text()\norder.inventory -= 1\n",
    _PATH_PREFIX + _PATH_BINDING + "inventory.read_text()\norder.amount = 42\n",
    _PATH_PREFIX + "Path = custom\ninventory = Path('x')\ninventory.read_text()\n",
    "class Path: pass\ninventory = Path('x')\ninventory.read_text()\n",
    "from pathlib import Path as P\nP = custom\ninventory = P('x')\ninventory.read_text()\n",
    "import pathlib as p\np.Path = custom\ninventory = p.Path('x')\ninventory.read_text()\n",
    _PATH_PREFIX + "root = unknown()\ninventory = root.parent / 'x'\ninventory.read_text()\n",
    _PATH_PREFIX + "inventory = other = Path('x')\ninventory.read_text()\n",
    _PATH_PREFIX + "inventory, other = Path('x'), 2\ninventory.read_text()\nprice = 2\n",
    _PATH_PREFIX + "inventory = Path('x')\ninventory.write_text('x')\n",
    _PATH_PREFIX + "inventory = Path('x')\nconsume(inventory)\n",
    _PATH_PREFIX + "inventory = Path('x')\nread = inventory.read_text\n",
    _PATH_PREFIX + "inventory = Path('x')\ninventory.read_text()\ndef fn(inventory): return inventory\n",
    _PATH_PREFIX + "inventory = Path('x')\ninventory.read_text()\nexec(code)\n",
    _PATH_PREFIX + "inventory = Path('x')\ninventory.read_text()\nif :\n",
    _PATH_PREFIX + "Path.resolve = custom\ninventory = Path('x').resolve()\ninventory.read_text()\n",
    _PATH_PREFIX + "setattr(Path, 'resolve', custom)\ninventory = Path('x').resolve()\ninventory.read_text()\n",
    _PATH_PREFIX + "mutate(Path)\ninventory = Path('x')\ninventory.read_text()\n",
    _PATH_PREFIX + "root = Path('x')\nroot.parent = 42\ninventory = root.parent / 'x'\ninventory.read_text()\n",
    _PATH_PREFIX + "from unknown import *\ninventory = Path('x')\ninventory.read_text()\n",
    _PATH_PREFIX + "__file__ = 42\ninventory = Path(__file__)\ninventory.read_text()\n",
    _PATH_PREFIX + "inventory = Path('x')\ninventory.read_text()\ntry: pass\nexcept ValueError as Path: pass\n",
    _PATH_PREFIX + "inventory = Path('x')\ninventory.read_text()\ndef Path(): pass\n",
    _PATH_PREFIX + "inventory = Path('x')\ninventory.read_text()\ndel Path\n",
    "inventory = 10\n",
    "def fn(inventory):\n    inventory -= 1\n",
    _PATH_PREFIX + "if ready:\n    inventory = Path('x')\n    inventory.read_text()\n",
    "if ready:\n    from pathlib import Path\ninventory = Path('x')\ninventory.read_text()\n",
    _PATH_PREFIX + "inventory = Path(value)\ninventory.read_text()\n",
    _PATH_PREFIX + "inventory = Path('x')\ninventory.read_text()\ninventory == 5\n",
    _PATH_PREFIX + "inventory = Path('x')\ninventory.read_text()\ntext = 'inventory = 3'\n",
])
def test_unknown_or_numeric_bindings_remain_findings(kf, tmp_path, source):
    (tmp_path / "probe.py").write_text(source, encoding="utf-8")
    assert [v["check"] for v in kf._check_critical_number_audit(tmp_path)] == [
        "critical_number_without_audit"]


def test_dist_paths_remain_in_scope_with_individual_classification(kf, tmp_path):
    folder = tmp_path / "dist" / "candidates"
    folder.mkdir(parents=True)
    safe = folder / "reader.py"
    unsafe = folder / "numbers.py"
    safe.write_text(_PATH_PREFIX + _PATH_BINDING + "inventory.read_text()\n")
    unsafe.write_text("inventory = 10\n")
    assert set(kf._source_files(tmp_path)) == {safe, unsafe}
    assert [v["file"] for v in kf._check_critical_number_audit(tmp_path)] == [str(unsafe)]


def test_python_analysis_budget_does_not_clear_findings(kf, tmp_path):
    source = _PATH_PREFIX + _PATH_BINDING + "inventory.read_text()\n"
    (tmp_path / "large.py").write_text(source + "# padding\n" * 110000)
    (tmp_path / "nodes.py").write_text(source + "None\n" * 21000)
    assert len(kf._check_critical_number_audit(tmp_path)) == 2


def test_nonnumeric_classification_is_python_only(kf, tmp_path):
    (tmp_path / "account.go").write_text(
        _PATH_PREFIX + _PATH_BINDING + "inventory.read_text()\n")
    assert len(kf._check_critical_number_audit(tmp_path)) == 1


def test_path_expression_budget_keeps_ambiguous_finding(kf, tmp_path):
    source = _PATH_PREFIX + "root = Path('x')\n"
    for index in range(20):
        previous = "root" if not index else f"p{index - 1}"
        source += f"p{index} = {previous}.parent\n"
    (tmp_path / "probe.py").write_text(source + "inventory = p19 / 'x'\ninventory.read_text()\n")
    assert len(kf._check_critical_number_audit(tmp_path)) == 1
