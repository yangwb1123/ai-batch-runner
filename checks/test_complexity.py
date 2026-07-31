#!/usr/bin/env python3
"""Tests for the ast-based complexity gates."""
import ast
import tempfile
import unittest
from pathlib import Path

import checks.complexity as cx
from checks.config import ComplexityGate


def _fn(source):
    """Parse a function body and return its ast node."""
    tree = ast.parse("def f():\n" + "\n".join("    " + l for l in source.splitlines()))
    return tree.body[0]


class TestCyclomatic(unittest.TestCase):
    def test_flat_function_is_one(self):
        self.assertEqual(cx.cyclomatic(_fn("return 1")), 1)

    def test_if_chain(self):
        src = "if a:\n    pass\nelif b:\n    pass\nelse:\n    pass"
        self.assertEqual(cx.cyclomatic(_fn(src)), 3)

    def test_loops_and_handlers(self):
        src = "for x in y:\n    pass\nwhile z:\n    pass\ntry:\n    pass\nexcept E:\n    pass"
        self.assertEqual(cx.cyclomatic(_fn(src)), 4)  # for, while, except + base 1

    def test_bool_operators(self):
        self.assertEqual(cx.cyclomatic(_fn("return a and b or c")), 3)

    def test_ifexp_and_match(self):
        src = "return a if b else c\nmatch x:\n    case 1:\n        pass\n    case _:\n        pass"
        self.assertEqual(cx.cyclomatic(_fn(src)), 1 + 1 + 2)


class TestCognitive(unittest.TestCase):
    def test_flat_is_zero(self):
        self.assertEqual(cx.cognitive(_fn("return 1")), 0)

    def test_nested_if_penalty(self):
        src = "if a:\n    if b:\n        if c:\n            pass"
        # 1+0, 1+1, 1+2 = 1+2+3 = 6
        self.assertEqual(cx.cognitive(_fn(src)), 6)

    def test_bool_operators(self):
        self.assertEqual(cx.cognitive(_fn("return a and b and c")), 2)

    def test_siblings_do_not_nest(self):
        src = "if a:\n    pass\nif b:\n    pass"
        self.assertEqual(cx.cognitive(_fn(src)), 2)


class TestCheckFile(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_pass_and_fail(self):
        gate = ComplexityGate(max_cyclomatic=1, max_cognitive=0)
        ok = self.root / "ok.py"
        ok.write_text("def f():\n    return 1\n", encoding="utf-8")
        bad = self.root / "bad.py"
        bad.write_text("def f():\n    if a:\n        if b:\n            pass\n", encoding="utf-8")
        self.assertEqual(cx.check_file(ok, self.root, gate), 0)
        self.assertGreater(cx.check_file(bad, self.root, gate), 0)

    def test_ignore_pattern(self):
        gate = ComplexityGate(max_cyclomatic=1, max_cognitive=0, ignore_patterns=["tests/"])
        p = self.root / "tests" / "x.py"
        p.parent.mkdir()
        p.write_text("def f():\n    if a:\n        pass\n", encoding="utf-8")
        self.assertEqual(cx.check_file(p, self.root, gate), 0)

    def test_syntax_error_fails(self):
        gate = ComplexityGate()
        p = self.root / "broken.py"
        p.write_text("def f(:\n", encoding="utf-8")
        self.assertEqual(cx.check_file(p, self.root, gate), 1)

    def test_non_py_passes(self):
        gate = ComplexityGate()
        p = self.root / "notes.txt"
        p.write_text("text", encoding="utf-8")
        self.assertEqual(cx.check_file(p, self.root, gate), 0)


if __name__ == "__main__":
    unittest.main()
