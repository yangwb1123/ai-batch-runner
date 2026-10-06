"""Bounded regular-file JSONL snapshots and prefix digests; never discover sessions."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .task_center_io import _open_regular, checked_path
from .task_goal_store import _stat_key
from .task_native_history import decode_record

MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_SCAN_LINE_BYTES = 1024 * 1024
MAX_SCAN_RECORDS = 65536


def authorized_path(source: dict, file: str) -> Path:
    path = Path(file).expanduser()
    if not path.is_absolute():
        if not source["workspace"]:
            raise ValueError("relative history needs an explicitly registered workspace")
        path = Path(source["workspace"]) / path
    path = checked_path(path)
    roots = [Path(source["data_dir"])] + ([Path(source["workspace"])] if source["workspace"] else [])
    if not any(path == root or root in path.parents for root in roots):
        raise ValueError("history is outside registered roots")
    return path


class Snapshot:
    """At most one bounded line in memory; proof available only after exhaustion."""

    def __init__(self, path: Path, upto: int | None = None, prefixes: list | None = None):
        if upto is not None and (type(upto) is not int or not 0 < upto <= MAX_SCAN_BYTES):
            raise ValueError("history snapshot byte budget exceeded")
        self.path, self.upto = path, upto
        self.checkpoints = sorted(set(prefixes or []))
        if len(self.checkpoints) > 32 or any(type(n) is not int or not 0 < n <= MAX_SCAN_BYTES for n in self.checkpoints):
            raise ValueError("invalid history prefix verification budget")
        self.prefixes, self.proof, self.stat_key = {}, None, None
        self.records, self.total = 0, 0
        self.digest = hashlib.sha256()

    def rows(self):
        with _open_regular(self.path) as handle:
            before = os.fstat(handle.fileno())
            maximum = self.upto if self.upto is not None else before.st_size
            if not 0 < maximum <= MAX_SCAN_BYTES or before.st_size < maximum:
                raise ValueError("history snapshot is empty, shortened or exceeds byte budget")
            if self.checkpoints and self.checkpoints[-1] > maximum:
                raise ValueError("new history snapshot cannot omit a prior byte prefix")
            yield from self._rows(handle, maximum)
            after = os.fstat(handle.fileno())
            if _stat_key(before) != _stat_key(after) or _stat_key(self.path.stat()) != _stat_key(after):
                raise ValueError("native history changed during bounded scan")
            self.stat_key = _stat_key(after)
            self.proof = {"path": str(self.path), "bytes": self.total, "sha256": self.digest.hexdigest()}

    def _rows(self, handle, maximum: int):
        while self.total < maximum:
            line = handle.readline(min(MAX_SCAN_LINE_BYTES + 1, maximum - self.total))
            if not line or not line.endswith(b"\n") or len(line) > MAX_SCAN_LINE_BYTES:
                raise ValueError("native history has an unfinished or over-budget line")
            self.records += 1
            if self.records > MAX_SCAN_RECORDS:
                raise ValueError("native history record budget exceeded; no records were truncated")
            self._fold(line)
            yield self.records - 1, decode_record(line)

    def _fold(self, line: bytes) -> None:
        offset = 0
        while self.checkpoints and self.checkpoints[0] <= self.total + len(line):
            checkpoint = self.checkpoints.pop(0)
            cut = checkpoint - self.total
            self.digest.update(line[offset:cut])
            self.prefixes[checkpoint] = self.digest.hexdigest()
            offset = cut
        self.digest.update(line[offset:])
        self.total += len(line)
