"""Bounded durable checkpoint snapshots for the device control plane."""

from __future__ import annotations

import json
import os
from pathlib import Path


MAX_BLOB_BYTES = 256 * 1024
MAX_RECORD_BYTES = 512 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024


class CheckpointStateError(ValueError):
    """Raised when checkpoint state is malformed or cannot be persisted."""


class CheckpointStore:
    """Append-only latest-value checkpoint store with bounded compaction."""

    def __init__(self, state_dir, filename: str = "checkpoints.jsonl") -> None:
        self.path = Path(state_dir) / filename

    @staticmethod
    def _line(event: dict) -> bytes:
        line = (json.dumps(event, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")) + "\n").encode("utf-8")
        if len(line) > MAX_RECORD_BYTES:
            raise CheckpointStateError("checkpoint record too large")
        return line

    def load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or self.path.stat().st_size > MAX_FILE_BYTES:
            raise CheckpointStateError("checkpoint path is unsafe or too large")
        latest = {}
        with self.path.open("rb") as handle:
            for raw in iter(lambda: handle.readline(MAX_RECORD_BYTES + 1), b""):
                if len(raw) > MAX_RECORD_BYTES or not raw.endswith(b"\n"):
                    raise CheckpointStateError("checkpoint record too large")
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise CheckpointStateError("invalid checkpoint JSON") from exc
                task_id = str(event.get("task_id", "")) if isinstance(event, dict) else ""
                if not task_id:
                    raise CheckpointStateError("invalid checkpoint snapshot")
                if isinstance(event, dict) and event.get("deleted") is True:
                    latest.pop(task_id, None)
                    continue
                checkpoint = event.get("checkpoint") if isinstance(event, dict) else None
                if not isinstance(checkpoint, dict):
                    raise CheckpointStateError("invalid checkpoint snapshot")
                latest[task_id] = dict(checkpoint)
        return latest

    def save(self, task_id: str, checkpoint: dict) -> None:
        task_id = str(task_id or "")
        if not task_id or not isinstance(checkpoint, dict):
            raise CheckpointStateError("checkpoint task and value are required")
        blob = checkpoint.get("blob_b64", "")
        if not isinstance(blob, str) or not blob or len(blob) > MAX_BLOB_BYTES:
            raise CheckpointStateError("checkpoint blob is invalid or too large")
        event = {"task_id": task_id, "checkpoint": dict(checkpoint)}
        line = self._line(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = self.path.stat().st_size if self.path.exists() else 0
        if current + len(line) > MAX_FILE_BYTES:
            latest = self.load()
            latest[task_id] = dict(checkpoint)
            self.compact(latest)
            return
        try:
            with self.path.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise CheckpointStateError(f"cannot append checkpoint: {exc}") from exc

    def delete(self, task_id: str) -> None:
        """Append a tombstone so a rolled-back admission cannot reappear."""
        task_id = str(task_id or "")
        if not task_id:
            raise CheckpointStateError("checkpoint task id is required")
        line = self._line({"task_id": task_id, "deleted": True})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = self.path.stat().st_size if self.path.exists() else 0
        if current + len(line) > MAX_FILE_BYTES:
            latest = self.load()
            latest.pop(task_id, None)
            self.compact(latest)
            return
        try:
            with self.path.open("ab") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.path, 0o600)
        except OSError as exc:
            raise CheckpointStateError(
                f"cannot append checkpoint tombstone: {exc}") from exc

    def compact(self, checkpoints: dict[str, dict]) -> None:
        temp = self.path.with_name(self.path.name + ".tmp")
        try:
            with temp.open("wb") as handle:
                for task_id, checkpoint in checkpoints.items():
                    handle.write(self._line({"task_id": task_id,
                                             "checkpoint": checkpoint}))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            try:
                temp.unlink()
            except OSError:
                pass
            raise CheckpointStateError(
                f"cannot compact checkpoints: {exc}") from exc
