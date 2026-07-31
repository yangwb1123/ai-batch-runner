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


if __name__ == "__main__":
    unittest.main()
