"""pi session file access for rotation (R9 / T10).

Reads the session files pi writes under ~/.pi/agent/sessions/ (location
spike-validated in docs/spike-pi-session.md). Used to detect session
growth (rotation watermark) and compaction entries (warn), and to build
the --fork invocation that starts a fresh session from the old file.
Everything degrades gracefully: a missing/unreadable session simply means
"no rotation needed".
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

# test-injectable (monkeypatch), default matches pi's runtime layout
SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"


def workdir_key(cwd: str) -> str:
    """pi's session-dir key for a workdir: the absolute path with its
    leading '/' stripped and remaining '/' -> '-', wrapped in '--'. pi
    writes `--home-u1-project--` for /home/u1/project; dashing the leading
    slash too produces `---home-u1-project--`, which never matches a real
    pi session tree (round-4 finding P0-2)."""
    path = (cwd or os.getcwd()).lstrip("/")
    return "--" + path.replace("/", "-") + "--"


def session_file(cwd: str, session_name: str) -> Optional[Path]:
    """Newest session file for *session_name* under *cwd*'s session dir."""
    d = SESSIONS_DIR / workdir_key(cwd)
    if not d.is_dir():
        return None
    try:
        candidates = sorted(d.glob(f"*_{session_name}.jsonl"))
    except OSError:
        return None
    return candidates[-1] if candidates else None


def session_size_bytes(cwd: str, session_name: str) -> int:
    """Size of the active session file (0 when absent)."""
    f = session_file(cwd, session_name)
    if f is None:
        return 0
    try:
        return f.stat().st_size
    except OSError:
        return 0


def has_compaction(cwd: str, session_name: str) -> bool:
    """True when the session file contains a compaction entry (pi shrinks
    the context; worth warning the operator about)."""
    f = session_file(cwd, session_name)
    if f is None:
        return False
    try:
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    if json.loads(line).get("type") == "compaction":
                        return True
                except ValueError:
                    continue
    except OSError:
        return False
    return False


def fork_flags(session_file_path: str) -> list:
    """Agent flags that fork the session file into a fresh session (the
    rotation mechanism; --fork is documented by pi 0.83)."""
    from .config import _session_flags
    return _session_flags("fork", session_file_path, "")
