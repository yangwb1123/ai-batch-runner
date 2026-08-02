"""Reuse decision: is an existing artifact still reusable?

Centralizes the six reuse sites (cli._filter_reused, the four pipeline
task builders, and execute_stage's full-reuse short-circuit) behind one
contract: an artifact is reusable only when it exists, is non-empty, is
not a symlink, and (when validators are configured) still passes every
effective gate. A stale artifact is deleted so the caller regenerates it.

`--reuse-legacy` restores the old existence-only skip as an escape hatch.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Optional

from . import config
from .runner import revalidate_existing
from .config import log


def reuse_decision(output_path: str, validate_cmd: Optional[str],
                   workdir: str = "", legacy: bool = False) -> bool:
    """True when the artifact may be reused (skipped) by --reuse.

    Contract: exists AND non-empty AND not a symlink AND (no effective
    validators OR all of them pass against the artifact). A symlink or a
    failed revalidation deletes the artifact and returns False so the
    caller regenerates it (never validated through a symlink target).

    Args:
        output_path: the artifact path (resolved, absolute preferred)
        validate_cmd: the EFFECTIVE gate for this task/stage (task.validate
            > stage.validate_cmd > CLI --validate-cmd), or None/"" for none
        workdir: cwd used for validator commands ({cwd} placeholder) and
            relative output resolution
        legacy: existence-only check (--reuse-legacy escape hatch)
    """
    if legacy:
        return Path(output_path).exists()
    path = Path(output_path)
    if not path.is_file() or path.is_symlink():
        if path.is_symlink():
            log.warning("REUSE: %s is a symlink; deleting and regenerating", path)
            path.unlink(missing_ok=True)
        return False
    try:
        if path.stat().st_size == 0:
            return False
    except OSError:
        return False  # vanished between is_file and stat: fail closed
    wd = workdir or os.getcwd()
    if revalidate_existing(path, validate_cmd, wd):
        log.info("REUSED+VALIDATED: %s", path)
        return True
    log.warning("REUSE: deleting stale artifact %s", path)
    path.unlink(missing_ok=True)
    return False
