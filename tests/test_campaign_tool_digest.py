"""Campaign reuse must change when source or frozen tool code changes."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from pbatch import campaign_state, config, launcher


@pytest.fixture(params=[False, True], ids=["source", "frozen"])
def tool_root(request, tmp_path, monkeypatch):
    frozen = request.param
    extraction = tmp_path / "extraction"
    root = extraction / "share/ai-batch-runner" if frozen else tmp_path / "source"
    package = root / "pbatch"
    package.mkdir(parents=True)
    (package / "worker.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "pi-batch.py").write_text("from pbatch.cli import main\n", encoding="utf-8")
    (root / "pi-batch.yaml").write_text("config_version: 1\n", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(extraction), raising=False)
    # Frozen __file__ points at the PYZ tree, not the physical source snapshot.
    imported_package = extraction / "pbatch" if frozen else package
    monkeypatch.setattr(launcher, "__file__", str(imported_package / "launcher.py"))
    monkeypatch.setattr(campaign_state, "__file__", str(imported_package / "campaign_state.py"))
    monkeypatch.setattr(config, "TOOL_ROOT", str(root))
    return root


@pytest.mark.parametrize("relative", ["pbatch/worker.py", "pi-batch.py", "pi-batch.yaml"])
def test_tool_digest_changes_when_distributed_tool_file_changes(tool_root, relative):
    initial = campaign_state.tool_digest()
    assert len(initial) == 64
    assert campaign_state.tool_digest() == initial
    path = tool_root / relative
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "# changed\n", encoding="utf-8")
    assert campaign_state.tool_digest() != initial
    path.write_text(original, encoding="utf-8")
    assert campaign_state.tool_digest() == initial


def test_tool_digest_ignores_bytecode_cache(tool_root):
    initial = campaign_state.tool_digest()
    cache = tool_root / "pbatch/__pycache__"
    cache.mkdir()
    (cache / "worker.cpython-311.pyc").write_bytes(b"machine-specific cache")
    assert campaign_state.tool_digest() == initial


def test_tool_digest_is_stable_across_installation_directories(tool_root, tmp_path, monkeypatch):
    initial = campaign_state.tool_digest()
    relocated = tmp_path / "another extraction"
    new_root = relocated / "share/ai-batch-runner" if launcher.is_frozen() else relocated
    shutil.copytree(tool_root, new_root)
    monkeypatch.setattr(sys, "_MEIPASS", str(relocated))
    monkeypatch.setattr(config, "TOOL_ROOT", str(new_root))
    monkeypatch.setattr(launcher, "__file__", str(new_root / "pbatch/launcher.py"))
    assert campaign_state.tool_digest() == initial
