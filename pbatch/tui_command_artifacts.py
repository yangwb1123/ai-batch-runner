"""Private, bounded storage for command output promoted to TUI ArtifactRefs."""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
import errno
from . import file_lock as fcntl
import hashlib
import os
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Any, Iterator, Optional

from . import config
from .tui_codex_support import MAX_FIELD_BYTES
from .tui_command_artifact_helpers import (
    _context_key, _identity_digest, _output_bytes, _positive_int,
)


COMMAND_ARTIFACT_KIND = "command_output"
COMMAND_ARTIFACT_MEDIA_TYPE = "text/plain; charset=utf-8"
COMMAND_ARTIFACT_SUFFIX = ".command-output.txt"
COMMAND_ARTIFACT_MARKER = b"\n...[command output artifact truncated]\n"
DEFAULT_MAX_ACTIVE = 64
_FAILURE_LIMIT = 128
_STALE_TEMP_SECONDS = 60 * 60


@dataclass(frozen=True)
class CommandArtifactContext:
    """Trusted runtime identities used only through irreversible digests."""

    workspace_id: str
    session_id: str
    run_id: str
    turn_id: str
    item_id: str


@dataclass(frozen=True)
class CommandOutputArtifact:
    """A finalized private file and the metadata safe to place in an event."""

    path: str
    size_bytes: int
    original_bytes: int
    sha256: str
    truncated: bool = False

    def event_payload(self, item_id: str, name: str = "Command output") -> dict[str, Any]:
        return {
            "path": self.path,
            "scope": "data",
            "name": _bounded_line(name, 256) or "Command output",
            "artifact_id": f"command-output:{self.sha256[:24]}",
            "kind": COMMAND_ARTIFACT_KIND,
            "media_type": COMMAND_ARTIFACT_MEDIA_TYPE,
            "size_bytes": self.size_bytes,
            "original_bytes": self.original_bytes,
            "sha256": self.sha256,
            "truncated": self.truncated,
            "item_id": _bounded_line(item_id, 2048),
        }


@dataclass(frozen=True)
class CommandArtifactOutcome:
    artifact: Optional[CommandOutputArtifact] = None
    warning: str = ""


@dataclass
class _Capture:
    context: CommandArtifactContext
    buffer: bytearray = field(default_factory=bytearray)
    original_bytes: int = 0
    written_bytes: int = 0
    truncated: bool = False
    descriptor: int = -1
    directory_descriptor: int = -1
    temporary_name: str = ""
    final_name: str = ""
    relative_path: str = ""
    digest: Any = field(default_factory=hashlib.sha256)


class CommandArtifactStore:
    """Spool only oversized output and retain a bounded per-session ring."""

    def __init__(
            self, cwd: str, *, preview_bytes: int = MAX_FIELD_BYTES,
            max_bytes: Optional[int] = None,
            store_max_bytes: Optional[int] = None,
            max_files: Optional[int] = None,
            max_active: int = DEFAULT_MAX_ACTIVE) -> None:
        self.cwd = str(Path(cwd).resolve())
        self.data_root = config.data_dir(self.cwd).resolve()
        self.preview_bytes = _positive_int(preview_bytes, "preview_bytes")
        self.max_bytes = _positive_int(
            max_bytes if max_bytes is not None
            else config.TUI_COMMAND_ARTIFACT_MAX_BYTES, "max_bytes")
        self.store_max_bytes = _positive_int(
            store_max_bytes if store_max_bytes is not None
            else config.TUI_COMMAND_ARTIFACT_STORE_MAX_BYTES,
            "store_max_bytes")
        self.max_files = _positive_int(
            max_files if max_files is not None
            else config.TUI_COMMAND_ARTIFACT_MAX_FILES, "max_files")
        self.max_active = _positive_int(max_active, "max_active")
        if self.max_bytes <= self.preview_bytes + len(COMMAND_ARTIFACT_MARKER):
            raise ValueError("max_bytes must exceed preview_bytes and marker size")
        if self.store_max_bytes < self.max_bytes:
            raise ValueError("store_max_bytes must be at least max_bytes")
        self._content_limit = self.max_bytes - len(COMMAND_ARTIFACT_MARKER)
        self._captures: OrderedDict[str, _Capture] = OrderedDict()
        self._failures: OrderedDict[str, tuple[CommandArtifactContext, str]] = OrderedDict()
        self._lock = threading.RLock()
        self._closed = False

    def append(self, context: CommandArtifactContext, value: Any) -> None:
        """Append one provider delta; failures are reported once at finish."""
        self._require_context(context)
        data = _output_bytes(value)
        with self._lock:
            if self._closed or not data:
                return
            key = _context_key(context)
            if key in self._failures:
                return
            capture = self._captures.get(key)
            if capture is None:
                capture = self._new_capture(key, context)
            if capture is None:
                return
            try:
                self._append_data(capture, data)
            except (OSError, RuntimeError, ValueError) as exc:
                self._record_failure(key, capture, _safe_error(exc))

    def capture(self, context: CommandArtifactContext,
                value: Any) -> CommandArtifactOutcome:
        """Replace any streamed state with one authoritative aggregate."""
        self.abort(context)
        self.append(context, value)
        return self.finish(context)

    def finish(self, context: CommandArtifactContext) -> CommandArtifactOutcome:
        """Finalize an oversized capture or discard a preview-sized one."""
        self._require_context(context)
        with self._lock:
            key = _context_key(context)
            failure = self._failures.pop(key, None)
            capture = self._captures.pop(key, None)
            if failure is not None:
                return CommandArtifactOutcome(warning=failure[1])
            if capture is None:
                return CommandArtifactOutcome()
            if capture.descriptor < 0:
                self._cleanup_capture(capture)
                return CommandArtifactOutcome()
            try:
                return CommandArtifactOutcome(artifact=self._finalize(capture))
            except (OSError, RuntimeError, ValueError) as exc:
                self._cleanup_capture(capture)
                return CommandArtifactOutcome(warning=_safe_error(exc))

    def abort(self, context: CommandArtifactContext) -> None:
        self._require_context(context)
        with self._lock:
            key = _context_key(context)
            capture = self._captures.pop(key, None)
            self._failures.pop(key, None)
            if capture is not None:
                self._cleanup_capture(capture)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.reset()

    def reset(self) -> None:
        """Discard unfinished captures while retaining finalized artifacts."""
        with self._lock:
            captures = list(self._captures.values())
            self._captures.clear()
            self._failures.clear()
        for capture in captures:
            self._cleanup_capture(capture)

    def _new_capture(
            self, key: str,
            context: CommandArtifactContext) -> Optional[_Capture]:
        if len(self._captures) >= self.max_active:
            self._remember_failure(key, context, "command artifact active limit reached")
            return None
        capture = _Capture(context)
        self._captures[key] = capture
        return capture

    def _append_data(self, capture: _Capture, data: bytes) -> None:
        capture.original_bytes += len(data)
        if capture.descriptor < 0:
            combined = len(capture.buffer) + len(data)
            if combined <= self.preview_bytes:
                capture.buffer.extend(data)
                return
            self._promote(capture)
        self._write_content(capture, data)

    def _promote(self, capture: _Capture) -> None:
        directory, relative = self._open_artifact_directory(capture.context)
        capture.directory_descriptor = directory
        final_name = _artifact_name(capture.context)
        descriptor, temporary = _create_private_temp(directory, final_name)
        capture.descriptor = descriptor
        capture.temporary_name = temporary
        capture.final_name = final_name
        capture.relative_path = f"{relative}/{final_name}"
        buffered = bytes(capture.buffer)
        capture.buffer.clear()
        self._write_content(capture, buffered)

    def _write_content(self, capture: _Capture, data: bytes) -> None:
        remaining = self._content_limit - capture.written_bytes
        if remaining <= 0:
            capture.truncated = capture.truncated or bool(data)
            return
        selected = data if len(data) <= remaining else _utf8_prefix(data, remaining)
        if selected:
            _write_all(capture.descriptor, selected)
            capture.digest.update(selected)
            capture.written_bytes += len(selected)
        capture.truncated = capture.truncated or len(selected) < len(data)

    def _finalize(self, capture: _Capture) -> CommandOutputArtifact:
        capture.truncated = (
            capture.truncated or capture.original_bytes > capture.written_bytes)
        if capture.truncated:
            _write_all(capture.descriptor, COMMAND_ARTIFACT_MARKER)
            capture.digest.update(COMMAND_ARTIFACT_MARKER)
            capture.written_bytes += len(COMMAND_ARTIFACT_MARKER)
        os.fsync(capture.descriptor)
        os.close(capture.descriptor)
        capture.descriptor = -1
        with _retention_lock(capture.directory_descriptor):
            self._prune(capture.directory_descriptor, capture.final_name,
                        capture.written_bytes)
            _require_safe_target(capture.directory_descriptor, capture.final_name)
            os.replace(
                capture.temporary_name, capture.final_name,
                src_dir_fd=capture.directory_descriptor,
                dst_dir_fd=capture.directory_descriptor)
            capture.temporary_name = ""
            os.fsync(capture.directory_descriptor)
        artifact = CommandOutputArtifact(
            path=capture.relative_path,
            size_bytes=capture.written_bytes,
            original_bytes=capture.original_bytes,
            sha256=capture.digest.hexdigest(),
            truncated=capture.truncated)
        self._cleanup_capture(capture)
        return artifact

    def _prune(self, directory: int, target: str, incoming_size: int) -> None:
        rows = _artifact_rows(directory, target, self.max_files)
        count, total = len(rows), sum(row[2] for row in rows)
        for _, name, size in rows:
            if count + 1 <= self.max_files and total + incoming_size <= self.store_max_bytes:
                break
            os.unlink(name, dir_fd=directory)
            count -= 1
            total -= size
        if count + 1 > self.max_files or total + incoming_size > self.store_max_bytes:
            raise RuntimeError("command artifact retention budget is exhausted")

    def _open_artifact_directory(
            self, context: CommandArtifactContext) -> tuple[int, str]:
        base = _ensure_private_root(self.data_root)
        names = (
            "tui", "artifacts", _identity_digest(context.workspace_id),
            _identity_digest(context.session_id))
        current, descriptor = self.data_root, base
        try:
            for name in names:
                next_path = current / name
                _ensure_private_directory(next_path)
                next_descriptor = _open_directory_nofollow(next_path)
                os.close(descriptor)
                descriptor = next_descriptor
                current = next_path
            relative = "/".join(names)
            return descriptor, relative
        except Exception:
            os.close(descriptor)
            raise

    def _record_failure(self, key: str, capture: _Capture, reason: str) -> None:
        self._captures.pop(key, None)
        self._cleanup_capture(capture)
        self._remember_failure(key, capture.context, reason)

    def _remember_failure(
            self, key: str, context: CommandArtifactContext, reason: str) -> None:
        self._failures[key] = (context, reason)
        self._failures.move_to_end(key)
        while len(self._failures) > _FAILURE_LIMIT:
            self._failures.popitem(last=False)

    @staticmethod
    def _require_context(context: CommandArtifactContext) -> None:
        if not isinstance(context, CommandArtifactContext):
            raise TypeError("context must be a CommandArtifactContext")
        values = (
            context.workspace_id, context.session_id, context.run_id,
            context.turn_id, context.item_id)
        if any(not isinstance(value, str) or not value for value in values):
            raise ValueError("command artifact context identities must be non-empty")

    @staticmethod
    def _cleanup_capture(capture: _Capture) -> None:
        if capture.descriptor >= 0:
            try:
                os.close(capture.descriptor)
            except OSError:
                pass
            capture.descriptor = -1
        if capture.temporary_name and capture.directory_descriptor >= 0:
            try:
                os.unlink(capture.temporary_name, dir_fd=capture.directory_descriptor)
            except OSError:
                pass
            capture.temporary_name = ""
        if capture.directory_descriptor >= 0:
            try:
                os.close(capture.directory_descriptor)
            except OSError:
                pass
            capture.directory_descriptor = -1


def _artifact_name(context: CommandArtifactContext) -> str:
    values = (context.run_id, context.turn_id, context.item_id)
    tokens = [_identity_digest(value) for value in values]
    return "-".join(tokens) + COMMAND_ARTIFACT_SUFFIX


def _ensure_private_root(path: Path) -> int:
    if path.is_symlink():
        raise RuntimeError("command artifact data root must not be a symlink")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError("command artifact data root is not a directory")
    return _open_directory_nofollow(path)


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("command artifact directory must not be a symlink")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError("command artifact path is not a directory")
    os.chmod(path, 0o700)


def _open_directory_nofollow(path: Path) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("platform cannot guarantee no-follow artifact storage")
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0)
    return os.open(path, flags)


def _create_private_temp(directory: int, final_name: str) -> tuple[int, str]:
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0))
    for _ in range(16):
        name = f".{final_name}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=directory)
        except FileExistsError:
            continue
        try:
            os.fchmod(descriptor, 0o600)
        except BaseException:
            os.close(descriptor)
            try:
                os.unlink(name, dir_fd=directory)
            except OSError:
                pass
            raise
        return descriptor, name
    raise RuntimeError("cannot allocate a unique command artifact temporary file")


@contextmanager
def _retention_lock(directory: int) -> Iterator[None]:
    flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    descriptor = os.open(".retention.lock", flags, 0o600, dir_fd=directory)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError("command artifact retention lock is not a private file")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _artifact_rows(directory: int, target: str,
                   max_files: int) -> list[tuple[int, str, int]]:
    names = os.listdir(directory)
    scan_limit = max(1024, max_files * 4)
    if len(names) > scan_limit:
        raise RuntimeError("command artifact directory scan limit exceeded")
    rows = []
    now_ns = time.time_ns()
    for name in names:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _stale_temp(name, info, now_ns):
            os.unlink(name, dir_fd=directory)
        elif name.endswith(COMMAND_ARTIFACT_SUFFIX) and name != target:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError("command artifact retention entry is not a private file")
            rows.append((info.st_mtime_ns, name, info.st_size))
    rows.sort()
    return rows


def _stale_temp(name: str, info: os.stat_result, now_ns: int) -> bool:
    age = now_ns - info.st_mtime_ns
    return (name.startswith(".") and name.endswith(".tmp")
            and stat.S_ISREG(info.st_mode)
            and age > _STALE_TEMP_SECONDS * 1_000_000_000)


def _require_safe_target(directory: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError("command artifact target is not a private regular file")


def _write_all(descriptor: int, value: bytes) -> None:
    view = memoryview(value)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(errno.EIO, "short command artifact write")
        view = view[written:]


def _utf8_prefix(value: bytes, maximum: int) -> bytes:
    if len(value) <= maximum:
        return value
    return value[:maximum].decode("utf-8", "ignore").encode("utf-8")


def _safe_error(error: BaseException) -> str:
    return _bounded_line(
        str(error) or type(error).__name__, 512) or "command artifact storage failed"


def _bounded_line(value: Any, maximum: int) -> str:
    text = " ".join(str(value or "").replace("\0", "�").split())
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text
    return encoded[:maximum - 3].decode("utf-8", "ignore") + "…"
