"""Device-Aware Execution Fabric — 控制平面 CLI（serve/approve/runners）。

与 pbatch/fabric_server.py（ControlPlane 核心）分离以控制模块行数；TLS/mTLS
证书参数在此处统一校验。
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
from pathlib import Path
from typing import Optional

from . import config
from .config import log
from .fabric_devices import _effective_device_state_dir
from .fabric_server import ControlPlane, _read_lines_bounded, start_server


def _serve_blocking(args) -> None:
    if config.DEVICE_FABRIC_MTLS_REQUIRED:
        missing = [name for name, value in (
            ("--cert-file", args.cert_file), ("--key-file", args.key_file),
            ("--ca-file", args.ca_file)) if not value]
        if missing:
            log.error("配置要求 mTLS，但缺少 %s", ", ".join(missing))
            raise SystemExit(2)
        args.require_client_cert = True
    if args.host not in ("127.0.0.1", "localhost"):
        log.warning("非本机监听：D3a 通道无 TLS，请确认网络环境可信"
                    "（或经反向代理终止 TLS）")
    try:
        httpd, port = start_server(
            args.port, args.state_dir, auto_approve=args.auto_approve,
            host=args.host, budget_cap=args.budget_cap,
            tenant_quotas=args.tenant_quotas,
            device_quotas=args.device_quotas,
            require_client_cert=args.require_client_cert,
            tls_certfile=args.cert_file, tls_keyfile=args.key_file,
            tls_cafile=args.ca_file)
    except (OSError, ssl.SSLError, ValueError) as exc:
        log.error("控制平面 TLS/监听初始化失败：%s", exc)
        raise SystemExit(2) from exc
    scheme = "https" if args.cert_file else "http"
    log.info("control plane listening on %s://%s:%d (approval=%s, mtls=%s)",
             scheme,
             args.host, port,
             "auto" if args.auto_approve else "manual",
             "on" if args.require_client_cert else "off")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
    # 优雅停机：serve_forever 收到 Ctrl-C（已验证有意）
        pass
    finally:
        httpd.server_close()


def serve_main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="pi-batch.py serve",
                                     description="设备控制平面（D3）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--auto-approve", action="store_true",
                        help="注册即批准（测试/受信环境；默认人工审批）")
    parser.add_argument("--budget-cap", type=float, default=0.0,
                        help="每设备累计预算上限（0=不限，防失控任务烧配额）")
    parser.add_argument("--tenant-quota", default="",
                        help="多租户配额：tenant=并发上限，逗号分隔"
                             "（如 team-a=2,team-b=4）")
    parser.add_argument("--device-quota", default="",
                        help="设备并发配额：设备名或 device_id=上限，逗号分隔"
                             "（如 node-a=2,rn-b=1）")
    parser.add_argument("--cert-file", default="", help="服务端 TLS 证书")
    parser.add_argument("--key-file", default="", help="服务端 TLS 私钥")
    parser.add_argument("--ca-file", default="", help="客户端证书 CA（mTLS）")
    parser.add_argument("--require-client-cert", action="store_true",
                        help="强制客户端证书并绑定 device fingerprint")
    args = parser.parse_args(argv)
    try:
        args.tenant_quotas = _parse_quota_map(args.tenant_quota,
                                              "--tenant-quota")
        args.device_quotas = _parse_quota_map(args.device_quota,
                                              "--device-quota")
    except ValueError as exc:
        parser.error(str(exc))
    _serve_blocking(args)
    raise SystemExit(0)


def _parse_quota_map(raw: str, option: str) -> dict:
    """解析并校验 CLI 配额；错误配置必须在启动监听前 fail closed。"""
    quotas = {}
    for pair in (raw or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, separator, value = pair.partition("=")
        key = key.strip()
        value = value.strip()
        if not separator or not key or not value:
            raise ValueError(f"{option} 格式应为 name=非负整数")
        try:
            limit = int(value)
        except ValueError as exc:
            raise ValueError(f"{option} {key!r} 的上限必须是整数") from exc
        if limit < 0:
            raise ValueError(f"{option} {key!r} 的上限不能为负数")
        if key in quotas:
            raise ValueError(f"{option} 重复配置 {key!r}")
        quotas[key] = limit
    return quotas


def approve_main(argv: Optional[list] = None) -> int:
    """人工审批待接入设备：把注册记录提升为 approved（只追加）。"""
    parser = argparse.ArgumentParser(prog="pi-batch.py approve",
                                     description="批准设备 Runner 接入")
    parser.add_argument("name", help="待批准的设备名（runners 中列出）")
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir or _effective_device_state_dir())
    pending = None
    path = state_dir / "runners.jsonl"
    if path.exists():
        for line in _read_lines_bounded(path):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if (isinstance(record, dict)
                    and record.get("name") == args.name
                    and not pending):
                pending = record
    if pending is None:
        print(f"no registration for {args.name!r}"
              "（先在被接入设备上启动 devices runner）", file=sys.stderr)
        raise SystemExit(2)
    control = ControlPlane(str(state_dir))
    control._approve(pending["device_id"], pending["name"], "cli")
    print(f"approved: {args.name} ({pending['device_id']})")
    raise SystemExit(0)


def runners_main(argv: Optional[list] = None) -> int:
    """列出已注册设备 Runner（读取 runners.jsonl，只读）。"""
    parser = argparse.ArgumentParser(prog="pi-batch.py runners",
                                     description="已注册设备 Runner 清单")
    parser.add_argument("--state-dir", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    state_dir = Path(args.state_dir or _effective_device_state_dir())
    records = []
    path = state_dir / "runners.jsonl"
    if path.exists():
        for line in _read_lines_bounded(path):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("device_id"):
                records.append(record)
    if args.json:
        safe = [{key: value for key, value in record.items()
                 if key != "secret"} for record in records]
        print(json.dumps(safe, indent=2))
    else:
        lines = ["# Registered Runners", ""]
        lines.append(f"{'DEVICE ID':<24} {'NAME':<20} REGISTERED_AT")
        for record in records:
            lines.append(f"{record['device_id']:<24} {record.get('name', ''):<20} "
                         f"{record.get('registered_at', '-')}")
        print("\n".join(lines))
    raise SystemExit(0)
