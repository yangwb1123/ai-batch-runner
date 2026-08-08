#!/usr/bin/env python3
"""Tests for the filesize gate."""
import tempfile
import unittest
from pathlib import Path

import checks.filesize as filesize
from checks.config import Config, FilesizeGate


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

    def test_build_dir_excluded_from_run(self):
        """AC1: a 600-line .py under build/ must not fail run(); the same
        file under src/ must fail (negative control — the exclusion, not a
        broken budget, is what passes)."""
        tmp = Path(tempfile.mkdtemp())
        oversized = tmp / "build" / "oversized.py"
        oversized.parent.mkdir()
        oversized.write_text("".join(f"# line {i}\n" for i in range(600)), encoding="utf-8")
        (tmp / "ok.py").write_text("x = 1\n", encoding="utf-8")

        orig_root, orig_cfg = filesize.repo_root, filesize.get_config
        try:
            filesize.repo_root = lambda: tmp
            filesize.get_config = lambda: Config(filesize=FilesizeGate(max_lines=500))
            self.assertEqual(filesize.run(), 0)          # build copy excluded
            (tmp / "src").mkdir()
            oversized.rename(tmp / "src" / "oversized.py")
            self.assertEqual(filesize.run(), 1)          # negative control
        finally:
            filesize.repo_root, filesize.get_config = orig_root, orig_cfg

    def test_run_scan_excludes_generated_dirs(self):
        """AC2: run() discovery never lists .py under build/, dist/,
        *.egg-info/, .git/, __pycache__/, logs/ — the scan list is exactly
        the authored tree (src/ok.py). A 600-line file under logs/ is the
        any-excluded-dir representative and must not fail run()."""
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
        (tmp / "logs" / "big.py").write_text(
            "".join(f"# line {i}\n" for i in range(600)), encoding="utf-8")

        orig_root = filesize.repo_root
        try:
            filesize.repo_root = lambda: tmp
            self.assertEqual(filesize.run(), 0)
            self.assertEqual(filesize.scan_files(tmp), [src / "ok.py"])
        finally:
            filesize.repo_root = orig_root

    def test_explicit_files_under_excluded_dir_unchanged(self):
        """R3: exclusion applies to discovery only — an explicit files
        list containing paths under an excluded dir is still checked
        (a future scope-tightening of explicit lists would fail here)."""
        tmp = Path(tempfile.mkdtemp())
        big = tmp / "build" / "big.py"
        big.parent.mkdir()
        big.write_text("".join(f"# {i}\n" for i in range(600)), encoding="utf-8")

        orig_root, orig_cfg = filesize.repo_root, filesize.get_config
        try:
            filesize.repo_root = lambda: tmp
            filesize.get_config = lambda: Config(filesize=FilesizeGate(max_lines=500))
            self.assertEqual(filesize.run([big]), 1)      # explicit list NOT filtered
        finally:
            filesize.repo_root, filesize.get_config = orig_root, orig_cfg


if __name__ == "__main__":
    unittest.main()
