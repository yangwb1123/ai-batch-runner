"""Versioned, provider-specific LLM pool configuration; no secrets in YAML."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import re

from .llm_pool_store import PoolAccount
from .user_dirs import pbatch_config_home, pbatch_runtime_home


GO_ORIGIN = "https://opencode.ai/zen/go/v1"
PROTOCOLS = frozenset({"chat/completions", "messages", "responses"})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


class PoolConfigCommandError(ValueError):
    """Fixed operator guidance only; never instantiate from external content."""


@dataclass(frozen=True)
class PoolConfig:
    pool_id: str
    accounts: tuple[PoolAccount, ...]
    models: dict[str, str]
    token_file: Path
    state_file: Path
    port: int = 8741
    request_timeout: float = 300
    probe_after: float = 60
    max_connections: int = 16
    usage_enabled: bool = False
    usage_max_age: float = 60
    usage_timeout: float = 5


def _mapping(value, fields: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) - fields:
        raise ValueError(f"{label}: expected a mapping with supported fields")
    return value


def _number(value, label: str, low: float, high: float, *, integer=False):
    valid = type(value) is int if integer else type(value) in (int, float)
    if not valid or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"{label}: expected a number in {low}..{high}")
    return value


def _identifier(value, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label}: expected a short alphanumeric identifier")
    return value


def _path(value, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError(f"{label}: file path required")
    path = Path(value).expanduser()
    return Path(os.path.abspath(path if path.is_absolute() else base / path))


def _account(value, base: Path) -> PoolAccount:
    data = _mapping(value, {"id", "credential_file", "quota_group", "priority",
                            "max_concurrent", "enabled"}, "account")
    enabled = data.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("account.enabled must be boolean")
    return PoolAccount(
        id=_identifier(data.get("id"), "account.id"),
        credential_file=str(_path(data.get("credential_file"), base, "credential_file")),
        quota_group=_identifier(data.get("quota_group"), "quota_group"),
        priority=_number(data.get("priority", 0), "priority", 0, 10000, integer=True),
        max_concurrent=_number(data.get("max_concurrent", 1), "max_concurrent", 1, 32,
                               integer=True),
        enabled=enabled,
    )


def _models(value) -> dict[str, str]:
    if not isinstance(value, dict) or not 1 <= len(value) <= 128:
        raise ValueError("models must explicitly map 1–128 model IDs to protocols")
    for name, protocol in value.items():
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name)
                or not isinstance(protocol, str) or protocol not in PROTOCOLS):
            raise ValueError("invalid model ID or protocol")
    return dict(value)


def _usage_options(value) -> dict:
    data = _mapping(value, {"enabled", "max_age", "timeout"}, "usage")
    enabled = data.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("usage.enabled must be boolean")
    return {"usage_enabled": enabled,
            "usage_max_age": _number(data.get("max_age", 60), "usage.max_age", 1, 3600),
            "usage_timeout": _number(data.get("timeout", 5), "usage.timeout", 0.1, 30)}


def validate_resource_paths(settings: PoolConfig, source: Path | None = None) -> None:
    """Credential replacement must never overwrite configuration or runtime state."""
    files = [settings.token_file, *(Path(item.credential_file) for item in settings.accounts)]
    files += [settings.state_file, *(Path(str(settings.state_file) + suffix)
                                     for suffix in ("-journal", "-wal", "-shm"))]
    if source is not None:
        source = Path(os.path.abspath(source))
        if any(path.parent == source.parent and path.name.startswith(f".{source.name}.")
               for path in files):
            raise ValueError("pool resources must not use the configuration management namespace")
        files.append(source)
    for index, path in enumerate(files):
        if any(path == other or path in other.parents or other in path.parents
               for other in files[index + 1:]):
            raise ValueError("pool file resources must not overlap")


def parse_config(data: dict, base: Path) -> PoolConfig:
    data = _mapping(data, {"schema_version", "pool_id", "accounts", "models", "gateway", "usage"},
                    "LLM pool")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("LLM pool requires schema_version: 1")
    pool_id = _identifier(data.get("pool_id"), "pool_id")
    raw = data.get("accounts")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 64:
        raise ValueError("accounts must contain 1–64 account definitions")
    accounts = tuple(_account(item, base) for item in raw)
    if len({account.id for account in accounts}) != len(accounts):
        raise ValueError("duplicate account ID")
    gateway = _mapping(data.get("gateway", {}), {"token_file", "port", "request_timeout",
                        "probe_after", "max_connections"}, "gateway")
    token_file = _path(gateway.get("token_file"), base, "gateway.token_file")
    paths = [item.credential_file for item in accounts]
    if len(set(paths)) != len(paths) or str(token_file) in paths:
        raise ValueError("each credential file and gateway token file must be distinct")
    settings = PoolConfig(
        pool_id, accounts, _models(data.get("models")), token_file,
        pbatch_runtime_home() / "llm-pools" / pool_id / "state.sqlite3",
        port=_number(gateway.get("port", 8741), "port", 1, 65535, integer=True),
        request_timeout=_number(gateway.get("request_timeout", 300), "request_timeout", 1, 900),
        probe_after=_number(gateway.get("probe_after", 60), "probe_after", 1, 3600),
        max_connections=_number(gateway.get("max_connections", 16), "max_connections", 1, 64,
                                integer=True),
        **_usage_options(data.get("usage", {})),
    )
    validate_resource_paths(settings)
    return settings


def _unique_mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ValueError("configuration keys must be unique strings")
        result[key] = loader.construct_object(value_node)
    return result


def config_path(path: str = "") -> Path:
    """Resolve the existing override chain without reading the pool file."""
    if not path:
        from . import config
        reference = _mapping(config._section("llm_pool"), {"config_file"}, "llm_pool")
        path = reference.get("config_file", "")
    if not isinstance(path, str):
        raise ValueError("llm_pool.config_file must be a string")
    source = Path(path or pbatch_config_home() / "llm-pool.yaml").expanduser()
    return Path(os.path.abspath(source))


def decode_document(raw: bytes) -> dict:
    """The reader and configuration editor share the same strict YAML grammar."""
    import yaml
    if not isinstance(raw, bytes) or len(raw) > 65536:
        raise ValueError("LLM pool configuration exceeds 64 KiB")
    class UniqueLoader(yaml.SafeLoader):
        pass
    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                                 _unique_mapping)
    try:
        if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
               for token in yaml.scan(raw)):
            raise ValueError("LLM pool configuration does not support YAML aliases")
        data = yaml.load(raw, Loader=UniqueLoader)
        if not isinstance(data, dict):
            raise ValueError("LLM pool requires a mapping")
        return data
    except (yaml.YAMLError, UnicodeError, RecursionError):
        raise ValueError("invalid LLM pool YAML") from None


def load_config(path: str = "") -> PoolConfig:
    """Load a separate shared pool file; project config only references its path."""
    source = config_path(path)
    with source.open("rb") as stream:
        raw = stream.read(65537)
    settings = parse_config(decode_document(raw), source.parent)
    validate_resource_paths(settings, source)
    return settings
