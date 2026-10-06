"""Gate traversal regressions: prune only already-excluded directory trees."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKERS = (
    "check-ui-spec.py", "check-frontend-quality.py", "check-backend-quality.py",
    "check-design-intelligence.py", "check-backend-experience.py",
    "check-knowledge-freshness.py",
)
EXTRA = {"scratch", "ignored.ts"}


@pytest.fixture(params=CHECKERS)
def checker(request, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "traversal_" + request.param.replace("-", "_"), ROOT / "scripts" / request.param)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return request.param, module


def _scope(name, module):
    if hasattr(module, "scan_directory"):
        return set(module.DEFAULT_EXTS), module.DEFAULT_EXCLUDES | EXTRA
    if name == "check-knowledge-freshness.py":
        return module._SOURCE_SUFFIXES, {".dart_tool", "node_modules"}
    extensions = ({".go", ".dart", ".ts", ".tsx", ".py", ".js"}
                  if name == "check-backend-experience.py"
                  else {".dart", ".ts", ".tsx", ".vue", ".js", ".jsx"})
    excluded = {".dart_tool", "node_modules", "build"} | EXTRA
    if name == "check-backend-experience.py":
        excluded.add("vendor")
    return extensions, excluded


def _collect(name, module, root):
    if hasattr(module, "scan_directory"):
        return module.scan_directory(root, module.DEFAULT_EXTS, module.DEFAULT_EXCLUDES | EXTRA)
    if name == "check-knowledge-freshness.py":
        return list(module._source_files(root))
    return module._collect_files(root, ",".join(sorted(EXTRA)))


def _legacy_files(name, module, root):
    """Freeze the former rglob admission predicates, not its traversal cost."""
    extensions, excluded = _scope(name, module)
    paths = []
    for path in root.rglob("*"):
        if hasattr(module, "scan_directory"):
            admitted = (not path.is_dir() and path.suffix.lower() in extensions
                        and not any(part in excluded for part in path.parts[:-1]))
        else:
            admitted = (path.is_file() and path.suffix in extensions
                        and not any(part in excluded for part in path.parts))
        if admitted:
            paths.append(path)
    return sorted(paths)


def _tree(root):
    names = (
        "src/valid.ts", "src/ignored.ts", "src/UPPER.TS", "src/service.go",
        "src/account.py", "src/widget.dart", "src/view.vue", "src/style.css",
        "src/.hidden.js", "src/scratch/a.ts", "src/generated/a.go",
        "node_modules/pkg/a.ts", ".dart_tool/pkg/a.dart", "build/a.ts",
        "vendor/a.go", ".git/example.ts", "docs/example.ts", "tests/example.ts",
        "scratch/deep/a.ts", "node_modules_extra/a.ts", "building/a.ts",
    )
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("console.log('finding');\n", encoding="utf-8")
    (root / "src" / "link.ts").symlink_to(root / "src" / "valid.ts")
    (root / "src" / "missing.ts").symlink_to(root / "absent")


def test_pruned_collection_preserves_every_legacy_admission(checker, tmp_path):
    name, module = checker
    _tree(tmp_path)
    expected = _legacy_files(name, module, tmp_path)
    assert expected
    actual = _collect(name, module, tmp_path)
    assert sorted(actual) == expected
    assert len(actual) == len(set(actual))
    # A substring is not a directory-segment exclusion.
    assert tmp_path / "node_modules_extra" / "a.ts" in actual
    assert tmp_path / "building" / "a.ts" in actual


def test_excluded_directories_are_never_opened(checker, tmp_path, monkeypatch):
    name, module = checker
    _tree(tmp_path)
    _, excluded = _scope(name, module)
    original = os.scandir
    visited = []

    def checked_scan(path):
        directory = Path(path)
        assert not any(part in excluded for part in directory.relative_to(tmp_path).parts)
        visited.append(directory)
        return original(path)

    monkeypatch.setattr(module.os, "scandir", checked_scan)
    assert _collect(name, module, tmp_path)
    assert visited and tmp_path in visited


def test_excluded_scan_root_stays_excluded(checker, tmp_path, monkeypatch):
    name, module = checker
    root = tmp_path / "node_modules" / "app"
    root.mkdir(parents=True)
    (root / "view.ts").write_text("console.log(1);\n")
    monkeypatch.setattr(module.os, "scandir", lambda _path: pytest.fail("excluded root opened"))
    assert _collect(name, module, root) == []


def test_traversal_never_follows_directory_symlinks(checker, tmp_path, monkeypatch):
    name, module = checker
    root = tmp_path / "project"
    _tree(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.ts").write_text("console.log(1);\n")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "cycle").symlink_to(root, target_is_directory=True)
    original = os.scandir

    def checked_scan(path):
        assert Path(path).name not in {"linked", "cycle"}
        return original(path)

    monkeypatch.setattr(module.os, "scandir", checked_scan)
    actual = _collect(name, module, root)
    assert actual
    assert not any(path.name == "leak.ts" for path in actual)


def test_experience_counts_the_enumeration_it_actually_checked(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "experience_once", ROOT / "scripts/check-backend-experience.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "service.go"
    source.write_text("package service\n")
    calls = []

    def collect(root, skip):
        calls.append((root, skip))
        assert len(calls) == 1, "report must not enumerate the changing tree twice"
        return [source]

    monkeypatch.setattr(module, "_collect_files", collect)
    assert module.main(["--dir", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["files_scanned"] == 1
    assert len(calls) == 1


def test_knowledge_early_module_probe_does_not_limit_audit(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "knowledge_probe", ROOT / "scripts/check-knowledge-freshness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "finance"
    root.mkdir()
    safe = root / "first.go"
    safe.write_text("package finance\n")
    unsafe = root / "second.go"
    unsafe.write_text("balance = balance + 100\n")
    examined = []

    def sources(directory):
        assert directory in (tmp_path, root)
        for path in (safe, unsafe):
            examined.append(path)
            yield path

    monkeypatch.setattr(module, "_source_files", sources)
    assert module._module_dirs(tmp_path) == [root]
    assert examined == [safe]
    examined.clear()
    violations = module._check_critical_number_audit(tmp_path)
    assert examined == [safe, unsafe]
    assert [item["file"] for item in violations] == [str(unsafe)]
