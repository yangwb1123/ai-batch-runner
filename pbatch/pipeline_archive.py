"""Pipeline deliverable archiving with collision-safe destinations."""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime
from pathlib import Path

from .config import log


def _archive_outputs(outputs: list, archive_dir: str, label: str) -> list:
    """Move completed deliverables and return their destination paths."""
    return [destination for _, destination in
            _archive_output_pairs(outputs, archive_dir, label)]


def _archive_output_pairs(outputs: list, archive_dir: str,
                          label: str) -> list[tuple[str, str]]:
    """Move outputs and retain their original-to-archive mapping."""
    if not archive_dir or not outputs:
        return []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(archive_dir) / f"{label}-{stamp}"
    target.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[str, str]] = []
    for output in outputs:
        source = Path(output)
        if source.exists():
            destination = _archive_destination(target, source)
            shutil.move(str(source), str(destination))
            moved.append((str(output), str(destination)))
    if moved:
        log.info("ARCHIVED %d deliverable(s) -> %s", len(moved), target)
    return moved


def _archive_destination(target: Path, source: Path) -> Path:
    """Choose a destination without overwriting an earlier deliverable."""
    direct = target / source.name
    if not direct.exists():
        return direct
    digest = hashlib.sha256(str(source.resolve()).encode("utf-8")).hexdigest()[:8]
    candidate = target / f"{source.stem}-{digest}{source.suffix}"
    index = 2
    while candidate.exists():
        candidate = target / f"{source.stem}-{digest}-{index}{source.suffix}"
        index += 1
    return candidate
