"""Operator commands for the opt-in, user-scoped LLM account pool."""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
import secrets
import sys

from .llm_pool_config import load_config
from .llm_pool_client import client_config
from .llm_pool_accounts import AccountCommandError
from .llm_pool_config_io import ConfigBusyError, ConfigCommittedError, ConfigConflictError
from .llm_pool_secrets import read_secret, write_secret
from .llm_pool_store import PoolStore, PoolStoreError


def _parser():
    parser = argparse.ArgumentParser(
        prog="pi-batch pool", description="Go 账号池、配额冷却和本地模型网关")
    parser.add_argument("--config", default="", help="账号池 YAML（默认用户级 llm-pool.yaml）")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="校验配置；不调用模型、不创建运行状态")
    check.add_argument("--credentials", action="store_true", help="同时核验私有凭据文件")
    check.add_argument("--json", action="store_true")
    status = commands.add_parser("status", help="账号状态、冷却和占用；不查询供应商余额")
    status.add_argument("--model", default="")
    status.add_argument("--json", action="store_true")
    events = commands.add_parser("events", help="有界调度事件；不含密钥或对话正文")
    events.add_argument("--limit", type=int, default=20)
    events.add_argument("--json", action="store_true")
    usage = commands.add_parser("usage", help="查看缓存额度；--refresh 显式查询供应商")
    usage.add_argument("--account", default="", help="只查询指定账号")
    usage.add_argument("--refresh", action="store_true", help="有界刷新；缓存有效期内仍复用")
    usage.add_argument("--json", action="store_true")
    commands.add_parser("serve", help="启动需要令牌认证的 127.0.0.1 网关")
    commands.add_parser("mcp", help="只读 MCP stdio：pool_status / pool_usage / pool_events")
    for name in ("pause", "resume", "reset-auth"):
        item = commands.add_parser(name, help="暂停、恢复或显式清除凭据故障")
        item.add_argument("account")
    key = commands.add_parser("key-set", help="交互式写入账号私有 Key；不接受命令行明文")
    key.add_argument("account")
    key.add_argument("--stdin", action="store_true", help="从标准输入有界读取 Key")
    key.add_argument("--replace", action="store_true", help="显式替换已存在 Key")
    token = commands.add_parser("token-init", help="生成本地网关令牌；不会打印令牌")
    token.add_argument("--replace", action="store_true", help="显式轮换已存在令牌")
    client = commands.add_parser("client-config", help="输出 OpenCode 配置（仅引用令牌文件）")
    client.add_argument("--model", required=True)
    from .llm_pool_accounts import add_management_commands
    add_management_commands(commands)
    from .llm_pool_doctor import add_doctor_command
    add_doctor_command(commands)
    from .llm_pool_run import add_run_command
    add_run_command(commands)
    return parser


def _account(settings, account_id):
    account = next((item for item in settings.accounts if item.id == account_id), None)
    if account is None:
        raise ValueError("unknown pool account")
    return account


def _model(settings, model):
    if model and model not in settings.models:
        raise ValueError("model is not configured in this pool")


def _check(settings, credentials: bool) -> dict:
    if credentials:
        from .llm_pool_gateway import _load_keys
        read_secret(settings.token_file)
        _load_keys(settings)
    return {"valid": True, "pool_id": settings.pool_id,
            "provider": "opencode-go", "accounts": [item.id for item in settings.accounts],
            "models": settings.models, "credentials_checked": credentials}


def _key_set(settings, args):
    account = _account(settings, args.account)
    if args.stdin:
        value = sys.stdin.read(4098).removesuffix("\n")
    else:
        if not sys.stdin.isatty():
            raise ValueError("use an interactive terminal or explicit --stdin")
        value = getpass.getpass(f"API key for {account.id}: ")
    write_secret(Path(account.credential_file), value, replace=args.replace)
    return {"account": account.id, "saved": True,
            "note": "restart the gateway to load changed credentials; reset-auth is separate"}


def _serve(settings) -> None:
    from .llm_pool_gateway import PoolGateway
    with PoolGateway(settings) as server:
        print(f"LLM pool {settings.pool_id}: http://127.0.0.1:{server.server_port}/v1",
              file=sys.stderr)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass


def _local_action(settings, args):
    if args.command == "run":
        from .llm_pool_run import run_command
        return run_command(settings, args)
    if args.command == "check":
        return _check(settings, args.credentials)
    if args.command == "client-config":
        return client_config(settings, args.model)
    if args.command == "key-set":
        return _key_set(settings, args)
    if args.command == "token-init":
        write_secret(settings.token_file, secrets.token_urlsafe(32), replace=args.replace)
        return {"saved": True, "token_file": str(settings.token_file)}
    if args.command == "serve":
        _serve(settings)
        return None
    return _state_action(settings, args)


def _state_action(settings, args):
    if args.command == "status":
        _model(settings, args.model)
    if args.command == "events" and not 1 <= args.limit <= 100:
        raise ValueError("event limit must be 1–100")
    if args.command in ("pause", "resume", "reset-auth"):
        _account(settings, args.account)
    if args.command == "usage" and args.account:
        _account(settings, args.account)
    store = PoolStore(settings.state_file)
    if args.command == "status":
        return store.status(settings.accounts, args.model, require_usage=settings.usage_enabled)
    if args.command == "usage":
        from .llm_pool_usage_refresh import usage_report
        return usage_report(settings, store, account_id=args.account, refresh=args.refresh)
    if args.command == "events":
        return {"events": store.events(args.limit)}
    if args.command == "mcp":
        from .llm_pool_mcp import serve_mcp
        return serve_mcp(settings, store)
    if args.command == "reset-auth":
        store.reset_auth(args.account)
    else:
        store.set_enabled(args.account, args.command == "resume")
    return {"account": args.account, "action": args.command, "ok": True}


def pool_main(argv=None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            from .llm_pool_doctor import doctor_main
            return doctor_main(args.config, gateway=args.gateway, json_output=args.json)
        if args.command in ("init", "account"):
            from .llm_pool_accounts import management_action
            result = management_action(args)
        else:
            result = _local_action(load_config(args.config), args)
        if type(result) is int:
            return result
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
            if args.command == "usage" and args.refresh and not result["ok"]:
                return 1
        return 0
    except AccountCommandError as exc:
        print("LLM pool: " + str(exc), file=sys.stderr)
        return 2
    except ConfigBusyError:
        print("LLM pool: configuration is busy; retry the whole operation", file=sys.stderr)
        return 5
    except ConfigConflictError:
        print("LLM pool: configuration changed; obtain a fresh preview", file=sys.stderr)
        return 2
    except ConfigCommittedError as exc:
        print(json.dumps(dict(exc.report, committed=True, durability_confirmed=False), indent=2))
        print("LLM pool: configuration saved but durability not confirmed; inspect it and its "
              "backup before retrying", file=sys.stderr)
        return 1
    except (ValueError, OSError, PoolStoreError):
        # YAML/parser/key exceptions may contain sensitive file fragments.
        print("LLM pool: invalid configuration, private credential or unavailable state; "
              "check the pool file, account IDs and 0700/0600 permissions", file=sys.stderr)
        return 2
