"""Read-only configuration inspection commands."""

from __future__ import annotations

import argparse
import json
from typing import Optional

from .config_inspect import config_check, explain


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-batch.py config",
        description="检查配置版本、来源、合并结果和运行时解析值（只读）",
    )
    subparsers = parser.add_subparsers(dest="action")
    for name, help_text in (
        ("explain", "显示配置来源、键级 provenance 和最终运行时值"),
        ("check", "只执行配置版本/结构校验，适合 CI"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--file", default="", metavar="PATH",
                             help="只检查指定 YAML，不叠加用户配置")
        command.add_argument("--max-bytes", type=int, default=2 * 1024 * 1024,
                             help="单个配置文件最大读取字节数")
        command.add_argument("--json", action="store_true",
                             help="输出机器可读 JSON")
    return parser


def _print_human(report: dict, *, detailed: bool) -> None:
    """Render a compact operator-facing report."""
    status = report.get("status")
    if status is None:
        status = "ok" if report.get("valid") else "invalid"
    print(f"CONFIG: {status.upper()}")
    print(f"schema_version: {report['schema_version']}")
    for source in report["sources"]:
        state = "loaded" if source["loaded"] else "absent"
        print(f"{source['name']}: {state} ({source['path'] or '-'})")
        for error in source["errors"]:
            print(f"  ERROR: {error}")
    validation = report["validation"]
    for warning in validation["warnings"]:
        print(f"WARNING: {warning}")
    for error in validation["errors"]:
        print(f"ERROR: {error}")
    if not detailed:
        return
    print("resolved_runtime:")
    for key, value in sorted(report["resolved_runtime"].items()):
        print(f"  {key}: {value}")
    print("cli_override_contract:")
    for item in report["cli_override_contract"]:
        print(f"  {item['flag']} > {item['config_path']}")


def config_main(argv: Optional[list] = None) -> int:
    """Entry point for ``pi-batch.py config explain|check``."""
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 2
    if args.max_bytes <= 0:
        parser.error("--max-bytes must be positive")
    report = (config_check if args.action == "check" else explain)(
        args.file, args.max_bytes)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_human(report, detailed=args.action == "explain")
    return 0 if report.get("valid", report.get("status") == "ok") else 1


if __name__ == "__main__":
    raise SystemExit(config_main())
