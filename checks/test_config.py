#!/usr/bin/env python3
"""Unit tests for checks.config: gate configuration and scan_files().

scan_files() is the shared scan discovery for the three rglob gates
(filesize/complexity/style). These tests pin the R2 segment semantics
(exact-segment, not substring), the sorted deterministic ordering, and
the parameter/edge behavior of the helper itself.
"""
import tempfile
import unittest
from pathlib import Path

from checks.config import scan_files


def _make_tree(root: Path, rels) -> None:
    """Create files with final-newline content under root (mkdir -p style)."""
    for rel in rels:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n", encoding="utf-8")


def _rels(root: Path) -> list:
    return sorted(str(p.relative_to(root)) for p in scan_files(root))


class TestScanFiles(unittest.TestCase):
    """F-1/F-3/F-5: segment semantics, ordering, parameter/edge coverage."""

    def test_segment_matching_not_substring(self):
        """R2: exclusion matches whole directory segment names only —
        src/build-tools/x.py, mylogs/x.py, root build.py and Build/ (case
        variant) are kept; subproject/build/x.py is pruned at depth."""
        tmp = Path(tempfile.mkdtemp())
        _make_tree(tmp, ("src/build-tools/x.py", "mylogs/x.py",
                         "subproject/build/x.py", "Build/x.py", "build.py",
                         "a/x.py", "b/x.py"))
        self.assertEqual(
            _rels(tmp),
            ["Build/x.py", "a/x.py", "b/x.py", "build.py",
             "mylogs/x.py", "src/build-tools/x.py"])

    def test_excluded_segments_pruned_at_any_depth(self):
        """R2: pruning happens before descent — an excluded segment is
        dropped wherever it appears, and *.egg-info/ dirs are excluded by
        suffix at any depth."""
        tmp = Path(tempfile.mkdtemp())
        _make_tree(tmp, ("sub/deep/build/x.py", "vendor/pkg.egg-info/x.py",
                         "vendor/pkg.egg-info/deep/x.py", "src/ok.py"))
        self.assertEqual(_rels(tmp), ["src/ok.py"])

    def test_sorted_ordering_across_dirs(self):
        """F-3: exact byte-wise sorted order independent of walk order;
        excluded dirs interleaved must not disturb it."""
        tmp = Path(tempfile.mkdtemp())
        _make_tree(tmp, ("b/y.py", "a/x.py", "dist/z.py", "c/w.py", "c/z.py"))
        self.assertEqual(_rels(tmp), ["a/x.py", "b/y.py", "c/w.py", "c/z.py"])

    def test_absolute_and_deterministic(self):
        """R2/F-3: results are absolute Paths and identical across calls."""
        tmp = Path(tempfile.mkdtemp())
        _make_tree(tmp, ("a/x.py", "b/y.py"))
        first = scan_files(tmp)
        self.assertTrue(all(p.is_absolute() for p in first))
        self.assertEqual(first, scan_files(tmp))

    def test_suffix_parameter(self):
        """F-5: suffix= selects a different file extension."""
        tmp = Path(tempfile.mkdtemp())
        _make_tree(tmp, ("a/x.py", "a/y.go", "b/z.md"))
        self.assertEqual(_rels(tmp), ["a/x.py"])
        self.assertEqual(
            sorted(str(p.relative_to(tmp)) for p in scan_files(tmp, suffix=".go")),
            ["a/y.go"])
        self.assertEqual(
            sorted(str(p.relative_to(tmp)) for p in scan_files(tmp, suffix=".md")),
            ["b/z.md"])

    def test_empty_root_returns_empty(self):
        """F-5: a root with no matching files yields an empty list."""
        tmp = Path(tempfile.mkdtemp())
        (tmp / "readme.txt").write_text("hi\n", encoding="utf-8")
        self.assertEqual(scan_files(tmp), [])

    def test_nonexistent_root_returns_empty(self):
        """F-5: nonexistent root yields an empty list (same as rglob)."""
        self.assertEqual(scan_files(Path(tempfile.mkdtemp()) / "nope"), [])


if __name__ == "__main__":
    unittest.main()
