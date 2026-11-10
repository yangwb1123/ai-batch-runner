"""Exact host-declared reads, confined to a pinned workspace directory inode."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import uuid

from .hub_file_snapshot_models import (
    MAX_FILE_BYTES, MAX_TOTAL_BYTES, rejects_secret_content, validate_snapshot_paths,
)
from .workspace_files import _open_root, _open_relative_parent, _read_file

MANIFEST_ENV = "PBATCH_HUB_FILE_CAPTURE_MANIFEST"


def root_identity(workspace):
    descriptor = _open_root(workspace)
    try:
        info = os.fstat(descriptor)
        return [info.st_dev, info.st_ino]
    finally:
        os.close(descriptor)


def unavailable(paths, code):
    return {"files": [], "unavailable": [{"path": path, "code": code} for path in paths]}


def capture_files(workspace, paths, identity):
    paths = validate_snapshot_paths(paths)
    if (not isinstance(identity, list) or len(identity) != 2
            or any(type(value) is not int or value < 0 for value in identity)):
        raise ValueError("invalid workspace identity")
    try:
        descriptor = _open_root(workspace)
    except (OSError, ValueError):
        return unavailable(paths, "capture_unavailable")
    try:
        info = os.fstat(descriptor)
        if [info.st_dev, info.st_ino] != identity:
            return unavailable(paths, "changed")
        return _capture_paths(descriptor, paths)
    finally:
        os.close(descriptor)


def _capture_paths(descriptor, paths):
    files, missing, remaining = [], [], MAX_TOTAL_BYTES
    for path in paths:
        row, code = _capture_path(descriptor, path, remaining)
        if row is None:
            missing.append({"path": path, "code": code})
        else:
            files.append(row)
            remaining -= row["size_bytes"]
    return {"files": files, "unavailable": missing}


def _capture_path(descriptor, path, remaining):
    parent = None
    try:
        parent, name = _open_relative_parent(descriptor, path)
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return None, "not_regular_file"
        if info.st_size > MAX_FILE_BYTES:
            return None, "too_large"
        if info.st_size > remaining:
            return None, "capacity_exceeded"
        raw = _read_file(parent, name, remaining)
        if rejects_secret_content(raw):
            return None, "secret_detected"
        return {"snapshot_id": uuid.uuid4().hex, "path": path,
                "content_b64": base64.b64encode(raw).decode("ascii"),
                "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}, ""
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "read_failed"
    finally:
        if parent is not None:
            os.close(parent)


def main():
    """Private child entrypoint; errors never echo paths or file contents."""
    try:
        raw = os.environ.get(MANIFEST_ENV, "")
        if len(raw.encode("utf-8")) > 16384:
            return 2
        manifest = json.loads(raw)
        if not isinstance(manifest, dict) or set(manifest) != {"workspace", "paths", "identity"}:
            return 2
        value = capture_files(manifest["workspace"], manifest["paths"], manifest["identity"])
        print(json.dumps(value, ensure_ascii=True, separators=(",", ":")))
        return 0
    except Exception:
        return 2
