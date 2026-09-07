"""Typed, bounded native Pipeline recovery-boundary projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


_MAX_TEXT_BYTES = 256
_MAX_INDEX = 1_000_000
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class PipelineCheckpointSnapshot:
    """Last native stage boundary; it never authorizes a resume operation."""

    checkpoint_id: str = ""
    stage: str = ""
    stage_index: int = 0
    stage_total: int = 0
    completed_stages: int = 0
    next_stage_index: int = 0
    task_count: int = 0
    successful_tasks: int = 0
    success: bool = False
    cancelled: bool = False
    resume_boundary: str = ""
    plan_digest: str = ""
    event_id: str = ""


class PipelineCheckpointProjection:
    """Fold only explicit native ``pipeline.checkpoint`` facts."""

    def __init__(self) -> None:
        self._snapshot = PipelineCheckpointSnapshot()

    def reset(self) -> None:
        self._snapshot = PipelineCheckpointSnapshot()

    def validate(self, event: Any) -> None:
        if getattr(event, "kind", "") != "pipeline.checkpoint":
            return
        extensions = getattr(event, "extensions", {})
        if (extensions.get("runtime") != "pbatch-native"
                or extensions.get("read_only") is not True):
            raise ValueError("pipeline checkpoint is not a native read-only event")
        payload = getattr(event, "payload", {})
        if not isinstance(payload, dict):
            raise ValueError("pipeline checkpoint payload must be an object")
        for name in ("checkpoint_id", "stage", "resume_boundary"):
            _text(payload.get(name), name)
        counts = {
            name: _count(payload.get(name), name)
            for name in (
                "stage_index", "stage_total", "completed_stages",
                "next_stage_index", "task_count", "successful_tasks")}
        _validate_counts(counts)
        if payload["resume_boundary"] not in {"next_stage", "retry_stage"}:
            raise ValueError("pipeline checkpoint resume boundary is invalid")
        for name in ("success", "cancelled"):
            if not isinstance(payload.get(name), bool):
                raise ValueError(f"pipeline checkpoint {name} is invalid")
        digest = extensions.get("plan_digest", "")
        if digest and (not isinstance(digest, str) or len(digest) != 64
                       or any(char not in _HEX for char in digest)):
            raise ValueError("pipeline checkpoint plan digest is invalid")

    def consume(self, event: Any) -> None:
        if getattr(event, "kind", "") != "pipeline.checkpoint":
            return
        self.validate(event)
        payload = event.payload
        self._snapshot = PipelineCheckpointSnapshot(
            checkpoint_id=_text(payload["checkpoint_id"], "checkpoint_id"),
            stage=_text(payload["stage"], "stage"),
            stage_index=_count(payload["stage_index"], "stage_index"),
            stage_total=_count(payload["stage_total"], "stage_total"),
            completed_stages=_count(payload["completed_stages"], "completed_stages"),
            next_stage_index=_count(payload["next_stage_index"], "next_stage_index"),
            task_count=_count(payload["task_count"], "task_count"),
            successful_tasks=_count(payload["successful_tasks"], "successful_tasks"),
            success=payload["success"], cancelled=payload["cancelled"],
            resume_boundary=_text(payload["resume_boundary"], "resume_boundary"),
            plan_digest=str(event.extensions.get("plan_digest", "") or ""),
            event_id=_text(getattr(event, "event_id", ""), "event_id"),
        )

    def snapshot(self) -> PipelineCheckpointSnapshot:
        return self._snapshot


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value:
        raise ValueError(f"pipeline checkpoint {label} is invalid")
    if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError(f"pipeline checkpoint {label} is too long")
    return value


def _count(value: Any, label: str) -> int:
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < 0 or value > _MAX_INDEX):
        raise ValueError(f"pipeline checkpoint {label} is invalid")
    return value


def _validate_counts(counts: dict[str, int]) -> None:
    total = counts["stage_total"]
    index = counts["stage_index"]
    if (total and (index == 0 or index > total)
            or counts["completed_stages"] > total
            or counts["next_stage_index"] > total + 1
            or counts["next_stage_index"] != counts["completed_stages"] + 1
            or counts["successful_tasks"] > counts["task_count"]):
        raise ValueError("pipeline checkpoint counts are inconsistent")
