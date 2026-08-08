#!/usr/bin/env python3
"""Tests for the style gate."""
import tempfile
import unittest
from pathlib import Path

import checks.style as style
from checks.config import StyleGate


class TestStyle(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.gate = StyleGate(max_line_length=100)

    def test_clean_file_passes(self):
        p = self.root / "a.py"
        p.write_text("x = 1\n\nprint(x)\n", encoding="utf-8")
        self.assertEqual(style.check_file(p, self.root, self.gate), 0)

    def test_trailing_whitespace_fails(self):
        p = self.root / "a.py"
        p.write_text("x = 1   \n", encoding="utf-8")
        self.assertEqual(style.check_file(p, self.root, self.gate), 1)

    def test_tab_indent_fails(self):
        p = self.root / "a.py"
        p.write_text("if x:\n\tpass\n", encoding="utf-8")
        self.assertEqual(style.check_file(p, self.root, self.gate), 1)

    def test_missing_final_newline_fails(self):
        p = self.root / "a.py"
        p.write_text("x = 1", encoding="utf-8")
        self.assertEqual(style.check_file(p, self.root, self.gate), 1)

    def test_long_line_warns_only(self):
        gate = StyleGate(max_line_length=10)
        p = self.root / "a.py"
        p.write_text("x = 1  # this line is far too long\n", encoding="utf-8")
        self.assertEqual(style.check_file(p, self.root, gate), 0)

    def test_ignore_pattern_exempts(self):
        gate = StyleGate(ignore_patterns=["tests/"])
        p = self.root / "tests" / "x.py"
        p.parent.mkdir()
        p.write_text("x = 1   \n", encoding="utf-8")
        self.assertEqual(style.check_file(p, self.root, gate), 0)

    def test_non_utf8_fails(self):
        p = self.root / "a.py"
        p.write_bytes(b"x = '\xff\xfe'\n")
        self.assertEqual(style.check_file(p, self.root, self.gate), 1)

    def test_non_py_passes(self):
        p = self.root / "notes.txt"
        p.write_text("x   \n", encoding="utf-8")
        self.assertEqual(style.check_file(p, self.root, self.gate), 0)

    def test_run_scan_excludes_generated_dirs(self):
        """AC2: run() discovery never lists .py under build/, dist/,
        *.egg-info/, .git/, __pycache__/, logs/ — the scan list is exactly
        the authored tree (src/ok.py)."""
        tmp = Path(tempfile.mkdtemp())
        for kind in ("build", "dist", ".git", "__pycache__", "logs"):
            d = tmp / kind
            d.mkdir()
            (d / "x.py").write_text("x = 1\n", encoding="utf-8")
        egg = tmp / "vendor" / "pkg.egg-info"
        egg.mkdir(parents=True)
        (egg / "x.py").write_text("x = 1\n", encoding="utf-8")
        src = tmp / "src"
        src.mkdir()
        (src / "ok.py").write_text("x = 1\n", encoding="utf-8")

        orig_root = style.repo_root
        try:
            style.repo_root = lambda: tmp
            self.assertEqual(style.run(), 0)
            self.assertEqual(style.scan_files(tmp), [src / "ok.py"])
        finally:
            style.repo_root = orig_root


if __name__ == "__main__":
    unittest.main()
