#!/usr/bin/env python3
"""Tests for the root policy gate."""
import tempfile
import unittest
from pathlib import Path

import checks.root_files as root_files
from checks.config import RootPolicyGate


class TestRootPolicy(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_empty_root_passes(self):
        self.assertEqual(root_files.check_root(self.root, RootPolicyGate()), 0)

    def test_allowed_files_pass(self):
        gate = RootPolicyGate(max_files=15, allowed_files=["README.md", "Makefile", "cli.py"])
        for name in gate.allowed_files:
            (self.root / name).write_text("x", encoding="utf-8")
        self.assertEqual(root_files.check_root(self.root, gate), 0)

    def test_unexpected_file_fails(self):
        gate = RootPolicyGate(max_files=15, allowed_files=["README.md"])
        (self.root / "stray_module.py").write_text("x", encoding="utf-8")
        self.assertEqual(root_files.check_root(self.root, gate), 1)

    def test_banned_pattern_fails(self):
        gate = RootPolicyGate(max_files=15, banned_patterns=["*_test.py"])
        (self.root / "thing_test.py").write_text("x", encoding="utf-8")
        self.assertEqual(root_files.check_root(self.root, gate), 1)

    def test_max_files_enforced(self):
        gate = RootPolicyGate(max_files=2, allowed_files=["a", "b", "c"])
        for name in gate.allowed_files:
            (self.root / name).write_text("x", encoding="utf-8")
        self.assertEqual(root_files.check_root(self.root, gate), 1)

    def test_directories_not_counted(self):
        gate = RootPolicyGate(max_files=2, allowed_files=["a"])
        (self.root / "a").write_text("x", encoding="utf-8")
        (self.root / "subdir").mkdir()
        self.assertEqual(root_files.check_root(self.root, gate), 0)


if __name__ == "__main__":
    unittest.main()
