"""No-follow filesystem packing and fresh-directory workspace restore."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path

from .workspace_bundle import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    WorkspaceBundleError,
    _path_parts,
    validate_bundle,
    validate_outputs,
)

_MAX_TREE_ENTRIES = 4096


def _directory_flags() -> int:
    """Return safe directory open flags, failing closed without no-follow."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory or not hasattr(os, "supports_dir_fd"):
        raise WorkspaceBundleError("platform lacks safe no-follow directory access")
    return os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)


def _open_root(path: str | os.PathLike) -> int:
    """Open a real workspace directory without following its final symlink."""
    try:
        candidate = Path(path)
        fd = os.open(candidate, _directory_flags())
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            os.close(fd)
            raise WorkspaceBundleError("workspace root must be a directory")
        return fd
    except WorkspaceBundleError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceBundleError("workspace root is unavailable") from exc


def _open_child_directory(parent_fd: int, name: str, *, create=False) -> int:
    flags = _directory_flags()
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
    return os.open(name, flags, dir_fd=parent_fd)


def _read_file(parent_fd: int, name: str, remaining: int) -> bytes:
    """Read one regular, non-hardlinked file with no symlink following."""
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise WorkspaceBundleError("workspace contains a symlink or special file")
    if before.st_size > min(MAX_FILE_BYTES, remaining):
        raise WorkspaceBundleError("workspace file or total size exceeds the limit")
    nonblock = getattr(os, "O_NONBLOCK", 0)
    if not nonblock:
        raise WorkspaceBundleError("platform lacks safe nonblocking file access")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | nonblock
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            raise WorkspaceBundleError("workspace file changed during capture")
        content = _read_bounded(fd, min(MAX_FILE_BYTES, remaining))
        after = os.fstat(fd)
        if (after.st_nlink != 1 or len(content) != after.st_size
                or opened.st_mtime_ns != after.st_mtime_ns
                or opened.st_ctime_ns != after.st_ctime_ns):
            raise WorkspaceBundleError("workspace file changed during capture")
        return content
    finally:
        os.close(fd)


def _read_bounded(fd: int, limit: int) -> bytes:
    chunks, size = [], 0
    while size <= limit:
        chunk = os.read(fd, min(8192, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if size > limit:
        raise WorkspaceBundleError("workspace file or total size exceeds the limit")
    return b"".join(chunks)


def _open_relative_parent(root_fd: int, path: str) -> tuple[int, str]:
    """Open every parent component relative to an already trusted root fd."""
    parts = _path_parts(path)
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = _open_child_directory(current, part)
            os.close(current)
            current = child
        return current, parts[-1]
    except OSError as exc:
        os.close(current)
        raise WorkspaceBundleError("workspace path is missing or unsafe") from exc


def _walk_files(directory_fd: int, prefix: str, found: list,
                entries: list[int]) -> None:
    names = _bounded_names(directory_fd, _MAX_TREE_ENTRIES - entries[0])
    for name in names:
        entries[0] += 1
        path = f"{prefix}/{name}" if prefix else name
        _path_parts(path)
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = _open_child_directory(directory_fd, name)
            try:
                _walk_files(child, path, found, entries)
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            found.append(path)
            if len(found) > MAX_FILES:
                raise WorkspaceBundleError("workspace bundle file count exceeds the limit")
        else:
            raise WorkspaceBundleError("workspace contains a symlink or special file")


def _bounded_names(directory_fd: int, remaining: int) -> list[str]:
    """Read at most the remaining tree budget plus one directory entry."""
    names = []
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                names.append(entry.name)
                if len(names) > remaining:
                    raise WorkspaceBundleError("workspace tree has too many entries")
    except WorkspaceBundleError:
        raise
    except OSError as exc:
        raise WorkspaceBundleError("workspace directory changed during capture") from exc
    return sorted(names)


def _collect_paths(root_fd: int) -> list[str]:
    paths = []
    _walk_files(root_fd, "", paths, [0])
    return validate_outputs(paths)


def _read_paths(root_fd: int, paths: list[str]) -> dict:
    files, total = [], 0
    for path in paths:
        parent_fd, name = _open_relative_parent(root_fd, path)
        try:
            content = _read_file(parent_fd, name, MAX_TOTAL_BYTES - total)
        finally:
            os.close(parent_fd)
        total += len(content)
        files.append({"path": path,
                      "content_b64": base64.b64encode(content).decode("ascii")})
    return validate_bundle({"schema": "pbatch.workspace.v1", "files": files})


def pack_directory(path: str | os.PathLike,
                   paths: list[str] | None = None) -> dict:
    """Pack regular files, either all files under a root or an exact allowlist."""
    root_fd = _open_root(path)
    try:
        selected = _collect_paths(root_fd) if paths is None else validate_outputs(paths)
        return _read_paths(root_fd, selected)
    finally:
        os.close(root_fd)


def _write_file(parent_fd: int, name: str, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short workspace file write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_relative(root_fd: int, path: str, content: bytes) -> None:
    parts = _path_parts(path)
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = _open_child_directory(current, part, create=True)
            os.close(current)
            current = child
        _write_file(current, parts[-1], content)
    finally:
        os.close(current)


def _destination_parent(destination: str | os.PathLike) -> tuple[Path, str]:
    target = Path(destination)
    name = target.name
    if not name or name in (".", ".."):
        raise WorkspaceBundleError("workspace destination must be a new directory")
    _path_parts(name)
    try:
        parent = target.parent.resolve(strict=True)
        if not parent.is_dir():
            raise WorkspaceBundleError("workspace destination parent is unavailable")
        return parent, name
    except WorkspaceBundleError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspaceBundleError("workspace destination parent is unavailable") from exc


def restore_bundle(bundle: object, destination: str | os.PathLike) -> Path:
    """Restore into a fresh directory; failed restores may retain that directory."""
    normalized = validate_bundle(bundle)
    parent, name = _destination_parent(destination)
    parent_fd = os.open(parent, _directory_flags())
    root_fd = -1
    created_identity = None
    created = False
    target = parent / name
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        created = True
        created_identity = os.stat(name, dir_fd=parent_fd,
                                   follow_symlinks=False)
        if not stat.S_ISDIR(created_identity.st_mode):
            raise WorkspaceBundleError("workspace destination changed during creation")
        root_fd = _open_child_directory(parent_fd, name)
        if not _same_directory(os.fstat(root_fd), created_identity):
            raise WorkspaceBundleError("workspace destination changed during creation")
        for item in normalized["files"]:
            content = base64.b64decode(item["content_b64"], validate=True)
            _write_relative(root_fd, item["path"], content)
        os.fsync(root_fd)
        os.fsync(parent_fd)
        opened_identity = os.fstat(root_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_directory(current, opened_identity):
            raise WorkspaceBundleError("workspace destination changed during restore")
        return target
    except WorkspaceBundleError as exc:
        if created:
            if root_fd >= 0:
                _remove_partial(root_fd, created_identity)
            raise WorkspaceBundleError(
                f"{exc}; a directory may remain, choose a new destination") from exc
        raise
    except OSError as exc:
        if created and root_fd >= 0:
            _remove_partial(root_fd, created_identity)
        message = "workspace restore failed"
        if created:
            message += "; a directory may remain, choose a new destination"
        raise WorkspaceBundleError(message) from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)


def _remove_partial(root_fd: int, expected: os.stat_result | None) -> None:
    """Clear the pinned partial tree without unlinking a raceable root name."""
    try:
        if expected is None or not _same_directory(os.fstat(root_fd), expected):
            return
        _remove_directory_contents(root_fd, [0])
    except (OSError, WorkspaceBundleError, RecursionError):
        pass


def _remove_directory_contents(directory_fd: int, entries: list[int]) -> None:
    """Remove entries relative to a pinned directory without following links."""
    names = _bounded_names(directory_fd, _MAX_TREE_ENTRIES - entries[0])
    for name in names:
        entries[0] += 1
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = _open_child_directory(directory_fd, name)
            try:
                opened = os.fstat(child)
                if _same_directory(info, opened):
                    _remove_directory_contents(child, entries)
            finally:
                os.close(child)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return (stat.S_ISDIR(left.st_mode) and stat.S_ISDIR(right.st_mode)
            and (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino))


__all__ = ["pack_directory", "restore_bundle"]
