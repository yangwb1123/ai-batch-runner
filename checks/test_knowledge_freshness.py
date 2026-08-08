#!/usr/bin/env python3
"""Tests for the knowledge-freshness checker (trust signals).

Regression coverage: the checker must not flag device-fabric configuration
as financial critical fields (inventory = 设备清单, not 财务库存).
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _run_checker(root: Path) -> int:
    """Run check-knowledge-freshness --dir --json; return violation count."""
    script = (Path(__file__).resolve().parent.parent
              / "scripts" / "check-knowledge-freshness.py")
    result = subprocess.run(
        [sys.executable, str(script), "--dir", str(root), "--json"],
        capture_output=True, text=True, timeout=120)
    if result.returncode not in (0, 1):
        raise AssertionError(f"checker failed: {result.stderr[-300:]}")
    try:
        start = result.stdout.index("{")
        decoded, _ = __import__("json").JSONDecoder().raw_decode(
            result.stdout[start:])
        return decoded.get("total", -1)
    except (ValueError, __import__("json").JSONDecodeError):
        raise AssertionError(f"no JSON report: {result.stdout[-300:]}")


class TestKnowledgeFreshness(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def _write(self, name: str, content: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_device_inventory_config_not_flagged(self):
        """假阳性回归：_DEVICE_INVENTORY 是设备清单，不是财务库存。"""
        self._write("config.py", (
            '_DEVICE_CFG = {"inventory": ["local", "ssh"]}\n'
            "_DEVICE_INVENTORY = _DEVICE_CFG.get('inventory')\n"
            "print(_DEVICE_INVENTORY)\n"))
        self.assertEqual(_run_checker(self.root), 0)

    def test_real_critical_number_without_audit_flagged(self):
        """真实关键数字变更（无审计信号）必须被标记。"""
        self._write("pricing.py", (
            "def apply_discount(order, pct):\n"
            "    order.amount = order.amount * (1 - pct)\n"
            "    order.inventory = order.inventory - 1\n"))
        self.assertEqual(_run_checker(self.root), 1)

    def test_critical_number_with_audit_signal_passes(self):
        """关键数字变更 + 审计信号（版本/历史）→ 不标记。"""
        self._write("pricing.py", (
            "SCHEMA_VERSION = 3\n"
            "def apply_discount(order, pct, audit):\n"
            "    audit.record(order.id, 'discount', pct)\n"
            "    order.amount = order.amount * (1 - pct)\n"))
        self.assertEqual(_run_checker(self.root), 0)


if __name__ == "__main__":
    unittest.main()
