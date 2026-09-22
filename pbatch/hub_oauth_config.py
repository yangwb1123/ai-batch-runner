"""Private, bounded configuration for the Agent Hub machine credential."""

from __future__ import annotations

import ipaddress
import json
import math
import os
from pathlib import Path
import stat
import urllib.parse

MAX_CONFIG_BYTES = 32 * 1024
MAX_SECRET_BYTES = 16 * 1024
CONFIG_ERROR = "machine credential configuration is invalid or unavailable"
_KEYS = {"hub_url", "token_endpoint", "client_id", "client_secret_file", "resource", "scopes"}


def private_bytes(path, maximum: int) -> bytes:
    """Open an owned private regular file without blocking on special files."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise ValueError(CONFIG_ERROR)
    flags = os.O_RDONLY | nofollow | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if not _private_file(info):
                raise ValueError(CONFIG_ERROR)
            raw = _read_descriptor(descriptor, maximum)
        finally:
            os.close(descriptor)
    except (OSError, TypeError, ValueError):
        raise ValueError(CONFIG_ERROR) from None
    return raw


def _private_file(info) -> bool:
    return (stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) in {0o400, 0o600}
            and (not hasattr(os, "getuid") or info.st_uid == os.getuid()))


def _read_descriptor(descriptor: int, maximum: int) -> bytes:
    chunks, size = [], 0
    while size <= maximum:
        chunk = os.read(descriptor, min(4096, maximum + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if size > maximum:
        raise ValueError(CONFIG_ERROR)
    return b"".join(chunks)


def strict_json(raw: bytes):
    """Reject duplicate object keys and the nonstandard numeric constants."""
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_pairs,
                      parse_constant=_invalid_constant, parse_float=_finite_float)


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(CONFIG_ERROR)
        value[key] = item
    return value


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(CONFIG_ERROR)
    return number


def _invalid_constant(_value):
    raise ValueError(CONFIG_ERROR)


def _text(value, maximum: int, *, whitespace: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise ValueError(CONFIG_ERROR)
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise ValueError(CONFIG_ERROR)
    if not whitespace and any(char.isspace() for char in value):
        raise ValueError(CONFIG_ERROR)
    return value


def _url(value: str):
    _text(value, 4096)
    if not value.isascii() or "\\" in value:
        raise ValueError(CONFIG_ERROR)
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(CONFIG_ERROR)
    if parsed.username is not None or parsed.password is not None or "?" in value or "#" in value:
        raise ValueError(CONFIG_ERROR)
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError(CONFIG_ERROR)
    if parsed.scheme == "http" and not ipaddress.ip_address(parsed.hostname).is_loopback:
        raise ValueError(CONFIG_ERROR)
    return parsed


def canonical_origin(value: str) -> str:
    parsed = _url(value)
    if parsed.path not in {"", "/"}:
        raise ValueError(CONFIG_ERROR)
    hostname = parsed.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{host}:{port}"


def _scopes(value) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise ValueError(CONFIG_ERROR)
    for scope in value:
        _text(scope, 256)
        if any(ord(char) < 33 or ord(char) > 126 or char in '\\"' for char in scope):
            raise ValueError(CONFIG_ERROR)
    if len(set(value)) != len(value) or len(" ".join(value)) > 4096:
        raise ValueError(CONFIG_ERROR)
    return value


def load_config(path, hub_url: str) -> dict:
    """Validate configuration and initial secret, without making a request."""
    try:
        config_path = Path(os.path.abspath(os.path.expanduser(path)))
        value = strict_json(private_bytes(config_path, MAX_CONFIG_BYTES))
        if not isinstance(value, dict) or set(value) != _KEYS:
            raise ValueError(CONFIG_ERROR)
        if canonical_origin(value["hub_url"]) != canonical_origin(hub_url):
            raise ValueError(CONFIG_ERROR)
        endpoint = _url(value["token_endpoint"])
        if not endpoint.path.startswith("/"):
            raise ValueError(CONFIG_ERROR)
        if ":" in _text(value["client_id"], 1024, whitespace=True):
            raise ValueError(CONFIG_ERROR)
        _text(value["resource"], 2048)
        _scopes(value["scopes"])
        secret = _text(value["client_secret_file"], 4096, whitespace=True)
        value["client_secret_file"] = str(config_path.parent / os.path.expanduser(secret))
        read_secret(value["client_secret_file"])
    except (OSError, TypeError, ValueError, RecursionError):
        raise ValueError(CONFIG_ERROR) from None
    return value


def read_secret(path: str) -> str:
    value = private_bytes(path, MAX_SECRET_BYTES).decode("utf-8")
    return _text(value, MAX_SECRET_BYTES, whitespace=True)
