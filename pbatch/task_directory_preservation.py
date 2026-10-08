"""Explicit private directory-byte publication; no worktree/database recovery or restore."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

from .task_center_io import checked_path
from .task_goal_store import resolve_source
from .task_lifecycle import stamp
from .task_retention_store import load_resource, resource_root, _actor
from .task_state_io import locked
from .task_preservation import (preservation_root, load_receipt, verify_copy, _source,
                                _private_root, _publish_or_reuse)
from .task_preservation_io import remove_owned_temporary
from .task_preservation_schema import receipt_key, validate_receipt
from .task_directory_archive import copy_directory


def preserve_directory(key: str, by: str, reason: str, expected: int, *, confirm: bool = False) -> dict:
    if confirm is not True:
        raise ValueError("directory preservation needs explicit --confirm; no source or names were read")
    _actor(by, reason)
    _source(load_resource(key), expected, kind="directory")
    root = checked_path(preservation_root())
    with locked(resource_root() / ".catalog"), locked(root / ".publication"):
        value = load_resource(key)
        source, path, info = _source(value, expected, kind="directory")
        _private_root(root)
        descriptor, name = tempfile.mkstemp(prefix=".preservation-", dir=root)
        temporary, created = Path(name), os.fstat(descriptor)
        try:
            with os.fdopen(descriptor, "wb") as output:
                payload, directory = copy_directory(path, output, info)
                output.flush()
                os.fsync(output.fileno())
            if (resolve_source(value["source_id"]) != source or
                    _source(load_resource(key), expected, kind="directory") != (source, path, info)):
                raise ValueError("source/resource changed while preserving directory; no receipt published")
            receipt = {"schema_version": 2, "resource_id": key, "resource_version": expected,
                "source_id": value["source_id"], "source_digest": value["source_digest"], "source_stat": info,
                "payload": payload, "directory_copy": directory, "created_at": stamp(), "by": by, "reason": reason}
            receipt["preservation_id"] = receipt_key(receipt)
            validate_receipt(receipt)
            _publish_or_reuse(root, temporary, receipt)
            verify_copy(receipt["preservation_id"], confirm=True)
            return load_receipt(receipt["preservation_id"])
        finally:
            remove_owned_temporary(temporary, created)
