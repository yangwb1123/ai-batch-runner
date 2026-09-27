"""POSIX management writes for controlled, user-owned pool configurations.

The stable lock is ``.<name>.lock``; ``.<name>.*`` in the same directory is
reserved for management files and cannot be a credential/token path. Existing
0755 directories and 0644 configurations are accepted; new files are 0600.
Ancestors are traversed without symlinks; the containing directory must be
user-owned and not group/world writable. No credential contents are accessed.

Cooperating writers must retain and flock the sidecar. Directory descriptors,
stable reads and optimistic checks detect observed external edits, but POSIX
replace is not compare-and-swap: a noncooperating process with the same UID can
still modify names/bytes after the last check. Such processes, ancestors and
the transform callable are trusted. Transforms must have no external effects.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import copy
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

import yaml

from .llm_pool_config import (PoolConfigCommandError, decode_document, parse_config,
                              validate_resource_paths)

MAX_BYTES = 65536


class ConfigBusyError(ValueError):
    """Another cooperating writer holds the nonblocking configuration lock."""


class ConfigConflictError(ValueError):
    """The configuration changed; obtain a new preview before retrying."""


class ConfigCommittedError(ValueError):
    """Publication succeeded, but subsequent durability/cleanup failed."""

    committed = True

    def __init__(self, report: dict):
        super().__init__("configuration committed; durability confirmation failed")
        self.report = report


def _path(path: Path) -> Path:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("configuration management requires POSIX no-follow support")
    result = Path(path).expanduser().absolute()
    if result.anchor != "/" or ".." in result.parts or not result.name:
        raise ValueError("invalid configuration path")
    return result


def _flags() -> int:
    return os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)


def _directory(descriptor: int) -> None:
    info = os.fstat(descriptor)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("configuration directory must be owned and not publicly writable")


@contextmanager
def _parent(path: Path, *, create=False, missing_ok=False):
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | _flags())
    try:
        for part in path.parent.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | _flags(),
                                dir_fd=descriptor)
            except FileNotFoundError:
                _directory(descriptor)
                if missing_ok:
                    yield None
                    return
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | _flags(),
                                dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        _directory(descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


def _file(info, *, private=False) -> None:
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_uid != os.getuid() or info.st_mode & 0o022
            or (private and stat.S_IMODE(info.st_mode) != 0o600)):
        raise ValueError("configuration file must be an owned, single-link regular file")


def _signature(info) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)


def _read(directory: int, name: str) -> tuple[bytes, tuple]:
    descriptor = os.open(name, os.O_RDONLY | _flags(), dir_fd=directory)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        _file(before)
        if before.st_size > MAX_BYTES:
            raise ValueError("configuration exceeds 64 KiB")
        raw = stream.read(MAX_BYTES + 1)
        after = os.fstat(stream.fileno())
        named = os.stat(name, dir_fd=directory, follow_symlinks=False)
        _file(after)
        if _signature(before) != _signature(after) or _signature(after) != _signature(named):
            raise ConfigConflictError("configuration changed while reading")
    if len(raw) > MAX_BYTES:
        raise ValueError("configuration exceeds 64 KiB")
    return raw, _signature(after)


def _validate(data: dict, path: Path) -> None:
    try:
        settings = parse_config(data, path.parent)
        validate_resource_paths(settings, path)
        paths = [settings.token_file, *(Path(a.credential_file) for a in settings.accounts)]
        if any(p == path or (p.parent == path.parent and p.name.startswith(f".{path.name}."))
               for resource in paths for p in (resource, *resource.parents)):
            raise ValueError("reserved management path")
    except Exception:
        raise ValueError("invalid configuration or reserved credential path") from None


def _decode(raw: bytes, path: Path) -> dict:
    try:
        data = decode_document(raw)
    except Exception:
        raise ValueError("invalid configuration document") from None
    _validate(data, path)
    return data


def _digest(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def read_document(path: Path) -> tuple[dict, bytes, str]:
    """Read a validated bounded snapshot without locks, secrets or other writes."""
    path = _path(path)
    with _parent(path) as directory:
        raw, _ = _read(directory, path.name)
    return _decode(raw, path), raw, _digest(raw)


@contextmanager
def _lock(directory: int, name: str):
    from . import file_lock as fcntl
    lock_name = f".{name}.lock"
    descriptor = _lock_file(directory, lock_name)
    try:
        info = os.fstat(descriptor)
        _file(info, private=True)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigBusyError("configuration is busy") from None
        named = os.stat(lock_name, dir_fd=directory, follow_symlinks=False)
        if _signature(info) != _signature(named):
            raise ConfigConflictError("configuration lock changed")
        yield
    finally:
        os.close(descriptor)  # Never unlink the stable sidecar, including on failure.


def _lock_file(directory: int, name: str) -> int:
    try:
        descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | _flags(),
                             0o600, dir_fd=directory)
    except FileExistsError:
        return os.open(name, os.O_RDWR | _flags(), dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _snapshot(directory, path: Path, create: bool):
    if directory is None:
        return None, None, None
    if create:
        try:
            os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None, None, None
        raise FileExistsError("configuration already exists")
    raw, signature = _read(directory, path.name)
    return _decode(raw, path), raw, signature


def _prepare(data, raw, path: Path, transform) -> tuple[bool, bytes]:
    try:
        updated = transform(copy.deepcopy(data))
        _validate(updated, path)
        if updated == data:
            return False, raw
        buffer = _YamlBuffer()
        yaml.safe_dump(updated, stream=buffer, sort_keys=False, allow_unicode=True)
        output = bytes(buffer.raw)
        _decode(output, path)
        return True, output
    except PoolConfigCommandError:
        raise
    except Exception:
        raise ValueError("invalid configuration update") from None


class _YamlBuffer:
    def __init__(self):
        self.raw = bytearray()

    def write(self, value: str):
        encoded = value.encode("utf-8")
        if len(self.raw) + len(encoded) > MAX_BYTES:
            raise ValueError("configuration exceeds 64 KiB")
        self.raw.extend(encoded)


def _check_current(directory: int, path: Path, raw, signature) -> None:
    with _parent(path) as current:
        if _signature(os.fstat(current))[:2] != _signature(os.fstat(directory))[:2]:
            raise ConfigConflictError("configuration directory changed")
    if raw is None:
        _snapshot(directory, path, True)
        return
    try:
        observed, identity = _read(directory, path.name)
    except (OSError, ValueError):
        raise ConfigConflictError("configuration changed or became unsafe") from None
    if identity != signature or _digest(observed) != _digest(raw):
        raise ConfigConflictError("configuration changed; retry with a fresh preview")


def _remove(directory: int, artifact) -> None:
    if artifact is not None:
        name, identity = artifact
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if (info.st_dev, info.st_ino) == identity:
                os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass


def _cleanup(directory: int, *artifacts) -> None:
    for artifact in artifacts:
        try:
            _remove(directory, artifact)
        except OSError:
            pass  # Cleanup must not hide cancellation or a committed-state error.


def _write_file(directory: int, name: str, kind: str, raw: bytes):
    filename = f".{name}.{kind}-{secrets.token_hex(16)}"
    descriptor = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _flags(),
                         0o600, dir_fd=directory)
    info = os.fstat(descriptor)
    artifact = filename, (info.st_dev, info.st_ino)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return artifact
    except BaseException:
        _cleanup(directory, artifact)
        raise


def _publish(directory: int, path: Path, raw, signature, output: bytes, report: dict):
    temporary = backup = None
    committed = False
    try:
        if raw is not None:
            backup = _write_file(directory, path.name, "backup", raw)
            report["backup_file"] = str(path.parent / backup[0])
            os.fsync(directory)  # Make the exact original durable before replacing it.
        temporary = _write_file(directory, path.name, "temp", output)
        _check_current(directory, path, raw, signature)
        if raw is None:
            os.link(temporary[0], path.name, src_dir_fd=directory, dst_dir_fd=directory,
                    follow_symlinks=False)
        else:
            os.replace(temporary[0], path.name, src_dir_fd=directory, dst_dir_fd=directory)
        committed = True
        _remove(directory, temporary)
        temporary = None
        os.fsync(directory)
    except BaseException:
        if committed:
            raise ConfigCommittedError(report.copy()) from None
        raise
    finally:
        _cleanup(directory, temporary, None if committed else backup)


def edit_document(path: Path, transform, *, create=False, dry_run=False,
                  expected_revision: str | None = None) -> dict:
    """Validate before/after and atomically publish a full document.

    ``revision`` is the original SHA-256 (None for init); ``result_revision`` is
    the proposed/resulting SHA-256. Dry runs report intended changes/restart,
    never create directories/locks/artifacts, and always have backup_file=None.
    No-op updates retain original bytes/comments. A busy writer must retry the
    whole operation, so its transform receives the latest committed document.
    """
    path = _path(path)
    if expected_revision is not None and (create or not isinstance(expected_revision, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_revision) is None):
        raise ValueError("expected_revision must be a lowercase SHA-256 for an existing file")
    with _parent(path, create=create and not dry_run, missing_ok=create and dry_run) as directory:
        with nullcontext() if dry_run else _lock(directory, path.name):
            data, raw, signature = _snapshot(directory, path, create)
            if expected_revision is not None and expected_revision != _digest(raw):
                raise ConfigConflictError("configuration revision does not match")
            changed, output = _prepare(data, raw, path, transform)
            report = {"changed": changed, "config_file": str(path), "backup_file": None,
                      "revision": _digest(raw), "result_revision": _digest(output),
                      "restart_required": changed}
            if changed and not dry_run:
                _publish(directory, path, raw, signature, output, report)
            elif directory is not None:
                _check_current(directory, path, raw, signature)
            return report
