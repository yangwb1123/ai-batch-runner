"""Operator-only pool configuration lifecycle; never opens credentials or state.

Account IDs, quota groups and credential references are stable identities. Updates
only change scheduling preferences. Removing an entry does not revoke a provider
key, erase history, change a running gateway, or bypass stored pause/auth controls.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from .llm_pool_config import PoolConfigCommandError as AccountCommandError
from .llm_pool_config import config_path, parse_config


def _preview_options(parser):
    parser.add_argument("--dry-run", action="store_true", help="预览；不创建配置、锁、备份或状态")
    parser.add_argument("--json", action="store_true")


def _new_account_options(parser, *, positional):
    if positional:
        parser.add_argument("account")
    else:
        parser.add_argument("--account", required=True)
    parser.add_argument("--quota-group", required=True, help="共享同一订阅额度的组标识")
    parser.add_argument("--credential-file", default="", help="可选的 Key 文件引用；不读取内容")
    parser.add_argument("--priority", type=int, default=0)
    parser.add_argument("--max-concurrent", type=int, default=1)
    parser.add_argument("--disabled", action="store_true", help="在配置中禁用账号")


def add_management_commands(commands):
    init = commands.add_parser("init", help="创建账号池配置；不生成密钥、不覆盖已有文件")
    init.add_argument("--pool-id", default="opencode-go")
    init.add_argument("--model", action="append", required=True, help="MODEL=PROTOCOL，可重复")
    init.add_argument("--port", type=int, default=8741)
    _new_account_options(init, positional=False)
    _preview_options(init)
    account = commands.add_parser("account", help="配置账号清单；修改后需重启网关")
    actions = account.add_subparsers(dest="account_action", required=True)
    listing = actions.add_parser("list", help="列出配置账号，不读 Key 或运行状态")
    listing.add_argument("--json", action="store_true")
    add = actions.add_parser("add", help="添加配置账号；凭据另用 key-set 导入")
    _new_account_options(add, positional=True)
    update = actions.add_parser("update", help="修改优先级、并发上限或配置启用标记")
    update.add_argument("account")
    update.add_argument("--priority", type=int)
    update.add_argument("--max-concurrent", type=int)
    enabled = update.add_mutually_exclusive_group()
    enabled.add_argument("--enable", dest="enabled", action="store_const", const=True, default=None)
    enabled.add_argument("--disable", dest="enabled", action="store_const", const=False)
    remove = actions.add_parser("remove", help="移除配置条目，保留 Key 和历史；须 --confirm ID")
    remove.add_argument("account")
    remove.add_argument("--confirm", default="", help="再次输入账号 ID 确认移除")
    for parser in (add, update, remove):
        _preview_options(parser)
        parser.add_argument("--if-revision", help="仅当配置仍匹配预览的 SHA-256 时应用")


def _key_path(pool_id, account_id):
    return f"llm-pool-keys/{pool_id}/accounts/{account_id}.key"


def _definition(args, pool_id):
    return {"id": args.account, "quota_group": args.quota_group,
            "credential_file": args.credential_file or _key_path(pool_id, args.account),
            "priority": args.priority, "max_concurrent": args.max_concurrent,
            "enabled": not args.disabled}


def initial_document(args) -> dict:
    models = {}
    for value in args.model:
        model, separator, protocol = value.partition("=")
        if not separator or model in models:
            raise AccountCommandError("models must be unique MODEL=PROTOCOL pairs")
        models[model] = protocol
    return {"schema_version": 1, "pool_id": args.pool_id,
            "models": models, "accounts": [_definition(args, args.pool_id)],
            "gateway": {"token_file": f"llm-pool-keys/{args.pool_id}/gateway-token",
                        "port": args.port},
            "usage": {"enabled": True, "max_age": 60, "timeout": 5}}


def changed_document(data: dict, args) -> dict:
    if args.account_action not in ("add", "update", "remove"):
        raise AccountCommandError("unsupported configuration change")
    result = deepcopy(data)
    existing = next((item for item in result["accounts"] if item["id"] == args.account), None)
    if args.account_action == "add":
        if existing is not None:
            raise AccountCommandError("account ID already exists; use update for scheduling changes")
        result["accounts"].append(_definition(args, result["pool_id"]))
    elif existing is None:
        raise AccountCommandError("unknown pool account; use account list to inspect configuration")
    elif args.account_action == "remove":
        if len(result["accounts"]) == 1:
            raise AccountCommandError("cannot remove the last account; disable it instead")
        result["accounts"].remove(existing)
    else:
        _update(existing, args)
    return result


def _update(account, args):
    changes = {name: getattr(args, name) for name in ("priority", "max_concurrent", "enabled")
               if getattr(args, name) is not None}
    if not changes:
        raise AccountCommandError("account update requires priority, max-concurrent, enable or disable")
    defaults = {"priority": 0, "max_concurrent": 1, "enabled": True}
    for name, value in changes.items():
        if account.get(name, defaults[name]) != value:
            account[name] = value


def account_listing(path: Path) -> dict:
    from .llm_pool_config_io import read_document
    data, _, revision = read_document(path)
    settings = parse_config(data, path.parent)
    return {"pool_id": settings.pool_id, "config_file": str(path), "revision": revision,
            "configuration_only": True, "credentials_checked": False,
            "accounts": [{"id": item.id, "quota_group": item.quota_group,
                          "priority": item.priority, "max_concurrent": item.max_concurrent,
                          "enabled": item.enabled} for item in settings.accounts]}


def _summary(data, account_id):
    if data is None:
        return None
    item = next((entry for entry in data["accounts"] if entry["id"] == account_id), None)
    if item is None:
        return None
    return {"id": item["id"], "quota_group": item["quota_group"],
            "priority": item.get("priority", 0), "max_concurrent": item.get("max_concurrent", 1),
            "enabled": item.get("enabled", True)}


def management_action(args) -> dict:
    from .llm_pool_config_io import edit_document
    path = config_path(args.config)
    creating = args.command == "init"
    if not creating and args.account_action == "list":
        return account_listing(path)
    if (not creating and args.account_action == "remove" and not args.dry_run
            and args.confirm != args.account):
        raise AccountCommandError("removal requires matching --confirm account ID, or use --dry-run")
    changes = {}
    def transform(data):
        result = initial_document(args) if creating else changed_document(data, args)
        changes.update(before=_summary(data, args.account), after=_summary(result, args.account))
        return result
    report = edit_document(path, transform, create=creating, dry_run=args.dry_run,
                           expected_revision=getattr(args, "if_revision", None))
    report.update(action="init" if creating else args.account_action, account=args.account,
                  dry_run=args.dry_run, credentials_changed=False, runtime_state_changed=False,
                  changes=changes)
    report["would_require_restart"] = report["changed"]
    report["restart_required"] = report["changed"] and not args.dry_run
    report["note"] = ("Configuration only; restart the gateway to apply. Credentials and "
                      "runtime pause/auth/quota history are unchanged. Backup preserves original YAML.")
    return report
