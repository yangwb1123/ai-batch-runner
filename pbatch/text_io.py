"""Small bounded-text primitives used at untrusted prompt boundaries."""

from __future__ import annotations

import os
from pathlib import Path
import stat

from .config_document import load_yaml_document


def parse_yaml_bounded(raw: str, yaml_module, label: str = "document"):
    """Apply node/alias budgets before construction and omit raw YAML from errors."""
    try:
        return load_yaml_document(raw, yaml_module)
    except (yaml_module.YAMLError, RecursionError) as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ValueError(f"{label}: invalid YAML{location} ({type(exc).__name__})") from None
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from None


def read_yaml_bounded(path: Path, maximum: int, yaml_module, label: str = "document"):
    """Use identical byte and construction budgets at all declarative input reads."""
    return parse_yaml_bounded(read_text_bounded(path, maximum, label), yaml_module, label)


def read_stream_bounded(stream, maximum: int, label: str = "stdin") -> str:
    """Read a UTF-8 stream with bounded reads and a byte, not character, budget."""
    if maximum < 1:
        raise ValueError(f"{label} limit must be positive")
    reader = getattr(stream, "buffer", stream)
    raw = bytearray()
    try:
        while True:
            chunk = reader.read(min(8192, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
            if len(raw) > maximum:
                raise ValueError(f"{label} exceeds {maximum} bytes")
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8") from exc
    except OSError as exc:
        raise ValueError(f"unable to read {label}") from exc


def read_text_bounded(path: Path, maximum: int, label: str = "file") -> str:
    """Read UTF-8 text without allocating an unbounded input buffer.

    The caller owns the policy for *maximum*.  A positive limit is required so
    a malformed configuration cannot accidentally turn this guard off.  The
    size check is repeated after opening to cover files that grow between
    ``stat`` and ``read``.  ``ValueError`` is deliberately used for policy
    rejection so callers can fail a task/stage without saving partial work.
    """
    if maximum < 1:
        raise ValueError(f"{label} limit must be positive")
    path = Path(path)
    try:
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular file: {path}")
        if metadata.st_size > maximum:
            raise ValueError(f"{label} exceeds {maximum} bytes: {path}")
        raw = _read_regular_bytes(path, maximum, label)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"unable to read {label} {path}: {exc}") from exc
    if len(raw) > maximum:
        raise ValueError(f"{label} exceeds {maximum} bytes: {path}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8: {path}") from exc


def _read_regular_bytes(path: Path, maximum: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular file: {path}")
        if metadata.st_size > maximum:
            raise ValueError(f"{label} exceeds {maximum} bytes: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read(maximum + 1)
    finally:
        os.close(descriptor)
