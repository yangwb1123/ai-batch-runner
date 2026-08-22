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
import re
import tempfile
from glob import escape as glob_escape
from pathlib import Path
from typing import Optional

from . import config
from .memory_io import bounded_lines

# test-injectable (monkeypatch), default matches pi's runtime layout
SESSIONS_DIR = Path.home() / ".pi" / "agent" / "sessions"
_PROVIDER_SESSION_MAX_BYTES = 4096
_CODEX_THREAD_RE = re.compile(r"^01[a-z0-9-]+$")


def provider_session_path(cwd: str, name: str, sid: str) -> Path:
    """Return the project-scoped provider thread mapping path."""
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", str(name))
    return config.data_dir(cwd) / "sessions" / f"{safe_name}.{sid}.codex-thread"


def valid_codex_thread_id(value: str) -> bool:
    """Whether *value* has the Codex UUID-like thread id shape."""
    return bool(_CODEX_THREAD_RE.fullmatch(value))


def write_provider_session(cwd: str, name: str, sid: str,
                           thread_id: str) -> Path:
    """Atomically persist one provider thread id without newline noise."""
    path = provider_session_path(cwd, name, sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(thread_id)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def capture_codex_thread(stdout_lines: list, name: str, sid: str, cwd: str,
                         bin_name: str) -> None:
    """Capture the first non-empty Codex JSONL thread event, if enabled."""
    if not config.agent_capture_thread_id(bin_name) or not stdout_lines:
        return
    first = next((line.lstrip() for line in stdout_lines if line.strip()), "")
    try:
        event = json.loads(first)
    except (TypeError, ValueError):
        return
    if not isinstance(event, dict) or event.get("type") != "thread.started":
        return
    thread_id = event.get("thread_id", "")
    if not isinstance(thread_id, str) or not valid_codex_thread_id(thread_id):
        return
    try:
        write_provider_session(cwd, name, sid, thread_id)
    except OSError as exc:
        config.log.warning("SESSION: cannot write Codex thread mapping: %s", exc)


def resolve_provider_session(cwd: str, name: str, sid: str, kind: str,
                             bin_name: str) -> tuple[str, bool]:
    """Resolve continue/fork to a captured provider id, or fall back."""
    if kind not in ("continue", "fork"):
        return sid, False
    path = provider_session_path(cwd, name, sid)
    try:
        with path.open("rb") as stream:
            raw = stream.read(_PROVIDER_SESSION_MAX_BYTES + 1)
    except FileNotFoundError:
        return sid, False
    except OSError as exc:
        config.log.warning("SESSION: cannot read %s provider mapping %s: %s",
                           bin_name, path, exc)
        return sid, False
    if len(raw) > _PROVIDER_SESSION_MAX_BYTES:
        config.log.warning("SESSION: provider mapping exceeds %d bytes: %s",
                           _PROVIDER_SESSION_MAX_BYTES, path)
        return sid, False
    try:
        thread_id = raw.decode("utf-8")
    except UnicodeDecodeError:
        thread_id = ""
    if not valid_codex_thread_id(thread_id):
        config.log.warning("SESSION: invalid %s provider mapping: %s", bin_name, path)
        return sid, False
    return thread_id, True


def workdir_key(cwd: str) -> str:
    """pi's session-dir key for a workdir: the absolute path with its
    leading '/' stripped and remaining '/' -> '-', wrapped in '--'. pi
    writes `--home-u1-project--` for /home/u1/project; dashing the leading
    slash too produces `---home-u1-project--`, which never matches a real
    pi session tree (round-4 finding P0-2)."""
    path = (cwd or os.getcwd()).lstrip("/")
    return "--" + path.replace("/", "-") + "--"


def session_file(cwd: str, session_name: str) -> Optional[Path]:
    """Newest session file for an id/name reference under *cwd*.

    Pi normally uses the session id as the filename suffix, while older
    adapters used the display name; accepting both keeps rotation portable.
    """
    d = SESSIONS_DIR / workdir_key(cwd)
    if not d.is_dir():
        return None
    try:
        escaped = glob_escape(session_name)
        candidates = sorted({path for pattern in (f"*_{escaped}.jsonl",
                                                   f"*_{escaped}-*.jsonl")
                             for path in d.glob(pattern)})
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
        for line in bounded_lines(f, config.SESSION_LINE_MAX_BYTES):
            if line is None:
                continue
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
