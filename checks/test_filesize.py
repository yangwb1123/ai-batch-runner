#!/usr/bin/env python3
"""Tests for the filesize gate."""
import tempfile
import unittest
from pathlib import Path

import checks.filesize as filesize
from checks.config import FilesizeGate


class TestCheckFile(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.gate = FilesizeGate(max_lines=500)

    def _write(self, name, n_lines):
        p = self.root / name
        p.write_text("\n".join(f"line {i}" for i in range(n_lines)) + "\n", encoding="utf-8")
        return p

    def test_non_py_passes(self):
        p = self.root / "readme.md"
        p.write_text("hi\n", encoding="utf-8")
        self.assertTrue(filesize.check_file(p, self.root, self.gate))

    def test_short_file_passes(self):
        self.assertTrue(filesize.check_file(self._write("a.py", 10), self.root, self.gate))

    def test_oversized_fails(self):
        self.assertFalse(filesize.check_file(self._write("a.py", 501), self.root, self.gate))

    def test_exact_budget_passes(self):
        self.assertTrue(filesize.check_file(self._write("a.py", 500), self.root, self.gate))

    def test_ignore_pattern_exempts(self):
        gate = FilesizeGate(max_lines=1, ignore_patterns=["tests/"])
        p = self.root / "tests" / "x.py"
        p.parent.mkdir()
        p.write_text("\n".join("x" for _ in range(5)), encoding="utf-8")
        self.assertTrue(filesize.check_file(p, self.root, gate))

    def test_exemption_by_basename(self):
        gate = FilesizeGate(max_lines=1, exemptions=["legacy.py"])
        p = self.root / "sub" / "legacy.py"
        p.parent.mkdir()
        p.write_text("x\ny\n", encoding="utf-8")
        self.assertTrue(filesize.check_file(p, self.root, gate))

    def test_run_counts_failures(self):
        self._write("ok.py", 3)
        big = self._write("big.py", 501)
        self.assertEqual(filesize.run([big, self.root / "ok.py"]), 1)


if __name__ == "__main__":
    unittest.main()
