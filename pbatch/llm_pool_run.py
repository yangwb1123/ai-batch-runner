"""Launch one explicitly supplied command with a bounded, private environment overlay.

No shell interpolation, credential reads, config-file writes, network requests or
runtime state. This is an OpenCode inline-config convenience, not a network sandbox:
the child can ignore it, select other models, or apply managed configuration.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys

from .llm_pool_client import PROVIDER, client_config
from .llm_pool_config import PoolConfigCommandError


INLINE_ENV = "OPENCODE_CONFIG_CONTENT"
MAX_CONFIG_BYTES = 65536
MAX_CONFIG_DEPTH = 32
_CONTROLLED_HEADERS = {"authorization", "proxy-authorization", "x-api-key", "cookie",
                       "host", "x-opencode-session"}


def add_run_command(commands):
    parser = commands.add_parser("run", help="仅为指定命令接入账号池；不修改全局环境或认证文件")
    parser.add_argument("--model", required=True, help="账号池中配置的默认模型")
    parser.add_argument("--small-model", default="", help="轻量任务模型；默认与 --model 相同")
    parser.add_argument("--dry-run", action="store_true", help="只输出脱敏计划，不启动命令")
    parser.add_argument("argv", nargs=argparse.REMAINDER, help="必须使用 -- COMMAND [ARGS...]")


def _unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate inline config field")
    return result


def _constant(_):
    raise ValueError("non-finite inline config constant")


def _bounded_tree(data):
    pending = [(data, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > MAX_CONFIG_DEPTH or isinstance(value, float) and not math.isfinite(value):
            raise ValueError("invalid inline config depth or number")
        if isinstance(value, dict):
            pending.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            pending.extend((child, depth + 1) for child in value)


def _inline_config(raw):
    try:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ValueError
        data = json.loads(raw, object_pairs_hook=_unique_fields, parse_constant=_constant) if raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError
        _bounded_tree(data)
        return data
    except (ValueError, RecursionError):
        raise PoolConfigCommandError("OPENCODE_CONFIG_CONTENT must be a bounded plain JSON object "
                                     "without duplicate fields or non-finite numbers; JSONC is not supported") from None


def _object(parent, name):
    value = parent.setdefault(name, {})
    if not isinstance(value, dict):
        raise PoolConfigCommandError("inline provider, model and options sections must be JSON objects")
    return value


def _allowed(data, name, choices, *, allowlist=False):
    if name not in data:
        return
    values = data[name]
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise PoolConfigCommandError("inline provider/model filters must be arrays of strings")
    permitted = all(item in values for item in choices) if allowlist else not any(item in values for item in choices)
    if not permitted:
        raise PoolConfigCommandError("inline configuration disables the pool provider or a selected model; "
                                     "review the existing filters instead of silently overriding them")


def _headers(data):
    if "headers" not in data:
        return
    headers = data["headers"]
    if (not isinstance(headers, dict) or any(not isinstance(value, str) for value in headers.values())
            or any(name.lower() in _CONTROLLED_HEADERS for name in headers)):
        raise PoolConfigCommandError("inline pool headers conflict with gateway authentication or session routing")


def _provider_options(provider, models):
    if "npm" in provider:
        raise PoolConfigCommandError("custom pool SDK overrides require manual integration")
    _allowed(provider, "blacklist", models)
    _allowed(provider, "whitelist", models, allowlist=True)
    options = _object(provider, "options")
    if any(name in options for name in ("fetch", "endpoint")):
        raise PoolConfigCommandError("custom pool transport overrides require manual integration")
    _headers(options)
    definitions = _object(provider, "models") if "models" in provider else {}
    for model in models:
        if model not in definitions:
            continue
        definition = _object(definitions, model)
        if "provider" in definition or definition.get("id", model) != model:
            raise PoolConfigCommandError("custom selected-model provider or ID overrides require manual integration")
        _headers(definition)
    return options


def merged_client_config(settings, model, small_model, raw):
    """Never return this configuration to logs/preview: inherited fields may be secrets."""
    patch = client_config(settings, model)
    small = small_model or model
    client_config(settings, small)  # Both selected models must belong to the pool.
    result = _inline_config(raw)
    _allowed(result, "disabled_providers", [PROVIDER])
    _allowed(result, "enabled_providers", [PROVIDER], allowlist=True)
    provider = _object(_object(result, "provider"), PROVIDER)
    options = _provider_options(provider, {model, small})
    options.update(patch["provider"][PROVIDER]["options"])
    result.setdefault("$schema", patch["$schema"])
    result.update(model=patch["model"], small_model=PROVIDER + "/" + small)
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_CONFIG_BYTES:
            raise ValueError
    except (ValueError, RecursionError):
        raise PoolConfigCommandError("merged inline configuration must be valid UTF-8 and at most 64 KiB") from None
    return encoded


def _command(arguments):
    if (not arguments or arguments[0] != "--" or len(arguments) < 2
            or not arguments[1] or arguments[1].startswith("-")):
        raise PoolConfigCommandError("pool run requires an explicit -- COMMAND [ARGS...] delimiter")
    if any(not isinstance(value, str) or "\0" in value for value in arguments):
        raise PoolConfigCommandError("invalid command arguments")
    return list(arguments[1:])


def prepare_launch(settings, args, environment):
    """Prepare separate argv/env values without mutating the caller's environment."""
    command = _command(args.argv)
    original = environment.get(INLINE_ENV, "")
    merged = merged_client_config(settings, args.model, args.small_model, original)
    child_env = dict(environment)
    child_env[INLINE_ENV] = merged
    report = {"action": "run", "dry_run": True, "pool_id": settings.pool_id,
              "provider": PROVIDER, "model": args.model, "small_model": args.small_model or args.model,
              "gateway_url": f"http://127.0.0.1:{settings.port}/v1",
              "inline_config_merged": bool(original.strip()), "inline_config_bytes": len(merged.encode("utf-8")),
              "environment_changes": [INLINE_ENV], "argument_count": len(command),
              "command_started": False, "credentials_read": False, "runtime_state_changed": False,
              "not_checked": ["executable", "gateway", "provider_authentication", "quota",
                              "effective_client_configuration"],
              "note": "Only an inline default-model overlay, not a network sandbox. Start the gateway "
                      "separately. Child arguments, environment and inherited configuration are not displayed."}
    return command, child_env, report


def run_command(settings, args):
    if os.name != "posix":
        raise PoolConfigCommandError("pool run requires POSIX exec support")
    command, child_env, report = prepare_launch(settings, args, os.environ)
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    try:
        os.execvpe(command[0], command, child_env)
    except FileNotFoundError:
        print("LLM pool: command executable or interpreter was not found", file=sys.stderr)
        return 127
    except (OSError, ValueError):
        print("LLM pool: cannot execute command; check permissions and argument/environment size", file=sys.stderr)
        return 126
    raise RuntimeError("exec unexpectedly returned")
