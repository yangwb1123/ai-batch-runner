"""Private, bounded credential files for the local LLM gateway."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import stat


def _flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "getuid"):
        raise ValueError("LLM pool private files require POSIX no-follow support")
    return os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


@contextmanager
def private_parent(path: Path, *, create: bool = False):
    """Anchor file access in a directory opened without following symlinks."""
    path = Path(os.path.abspath(path.expanduser()))
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | _flags())
    try:
        for part in path.parent.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | _flags(),
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("credential directory must be owner-only (0700)")
        yield descriptor, path.name
    finally:
        os.close(descriptor)


def _validate_file(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise ValueError("credential must be a private regular file (0600)")


def validate_secret(value: str) -> str:
    """Reject control characters and whitespace before constructing headers."""
    if not 16 <= len(value) <= 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError("credential must contain 16–4096 printable non-space ASCII characters")
    return value


def read_secret(path: Path) -> str:
    try:
        with private_parent(path) as (directory, name):
            descriptor = os.open(name, os.O_RDONLY | _flags(), dir_fd=directory)
            with os.fdopen(descriptor, "rb") as handle:
                _validate_file(handle.fileno())
                raw = handle.read(4098)
        return validate_secret(raw.decode("ascii").removesuffix("\n"))
    except (OSError, UnicodeError) as exc:
        raise ValueError("credential file is unavailable or invalid") from exc


def write_secret(path: Path, value: str, *, replace: bool = False) -> None:
    """Publish a private key atomically; overwriting requires an explicit flag."""
    raw = (validate_secret(value) + "\n").encode("ascii")
    with private_parent(path, create=True) as (directory, name):
        temporary = ".pool-key-" + secrets.token_hex(16)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _flags(),
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if replace:
                _check_replace(directory, name)
                os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            else:
                os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory,
                        follow_symlinks=False)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def _check_replace(directory: int, name: str) -> None:
    try:
        descriptor = os.open(name, os.O_RDONLY | _flags(), dir_fd=directory)
    except FileNotFoundError:
        return
    try:
        _validate_file(descriptor)
    finally:
        os.close(descriptor)
