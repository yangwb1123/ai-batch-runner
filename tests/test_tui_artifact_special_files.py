"""Untrusted artifact paths cannot make the synchronous preview wait on a FIFO."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from pbatch import tui_artifacts
from pbatch.tui_artifacts import ArtifactReference, preview_artifact


pytestmark = pytest.mark.skipif(
    not hasattr(os, "mkfifo") or not hasattr(os, "O_NOFOLLOW")
    or os.open not in os.supports_dir_fd, reason="POSIX no-follow artifact preview")
ROOT = Path(__file__).resolve().parents[1]


def _reference(path):
    return ArtifactReference("event", 1, "run", "turn", "actor", "Artifact", path=path)


@pytest.mark.parametrize("path", ["named.pipe", "named.pipe/child.txt"])
def test_fifo_without_writer_is_rejected_within_deadline(tmp_path, path):
    os.mkfifo(tmp_path / "named.pipe")
    code = """
from dataclasses import asdict
import json, sys
from pbatch.tui_artifacts import ArtifactReference, preview_artifact
reference = ArtifactReference('event', 1, 'run', 'turn', 'actor', 'FIFO', path=sys.argv[2])
print('ready', flush=True)
print(json.dumps(asdict(preview_artifact(reference, sys.argv[1]))), flush=True)
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path), path],
                             cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    try:
        try:
            output, errors = child.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            pytest.fail("artifact preview blocked on a FIFO without a writer")
        assert child.returncode == 0, errors
        ready, result = output.splitlines()
        assert ready == "ready"
        preview = json.loads(result)
        assert preview["status"] == "blocked" and not preview["content"]
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=3)


def test_nonblocking_preview_preserves_regular_utf8_bounds(tmp_path):
    content = "hello 世界\n" * 100
    (tmp_path / "normal.txt").write_text(content, encoding="utf-8")
    reference = _reference("normal.txt")
    complete = preview_artifact(reference, str(tmp_path))
    bounded = preview_artifact(reference, str(tmp_path), max_bytes=8)
    assert complete.status == "available" and complete.content == content
    assert complete.size_bytes == len(content.encode("utf-8"))
    assert bounded.status == "available" and bounded.truncated
    assert bounded.content == "hello "


@pytest.mark.parametrize("component", [False, True])
def test_preview_still_rejects_symlink_targets(tmp_path, component):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("outside secret")
    if component:
        (root / "redirect").symlink_to(outside, target_is_directory=True)
        path = "redirect/private.txt"
    else:
        (root / "redirect").symlink_to(outside / "private.txt")
        path = "redirect"
    preview = preview_artifact(_reference(path), str(root))
    assert preview.status == "blocked" and "symlink" in preview.reason
    assert not preview.content


def test_missing_nonblocking_capability_never_uses_a_blocking_fallback(tmp_path, monkeypatch):
    (tmp_path / "normal.txt").write_text("safe")
    without_nonblock = {name: getattr(os, name) for name in dir(os) if name != "O_NONBLOCK"}
    monkeypatch.setattr(tui_artifacts, "os", SimpleNamespace(**without_nonblock))
    result = preview_artifact(_reference("normal.txt"), str(tmp_path))
    assert result.status == "blocked" and not result.content


@pytest.mark.parametrize("replacement", ["symlink", "fifo"])
def test_replacement_after_open_reads_only_the_original_regular_descriptor(
        tmp_path, monkeypatch, replacement):
    root = tmp_path / "workspace"
    root.mkdir()
    artifact = root / "report.txt"
    artifact.write_text("original content")
    outside = tmp_path / "private.txt"
    outside.write_text("outside secret")
    original_fstat = tui_artifacts.os.fstat
    replaced = False

    def swap_before_type_check(descriptor):
        nonlocal replaced
        if not replaced:
            replaced = True
            artifact.unlink()
            if replacement == "fifo":
                os.mkfifo(artifact)
            else:
                artifact.symlink_to(outside)
        return original_fstat(descriptor)

    monkeypatch.setattr(tui_artifacts.os, "fstat", swap_before_type_check)
    result = preview_artifact(_reference("report.txt"), str(root))
    assert replaced and result.status == "available"
    assert result.content == "original content"
    assert "outside secret" not in json.dumps(asdict(result))
