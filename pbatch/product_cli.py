"""产品 CLI 规整命令：product / init / version / clean / completion。

- `pi-batch product`：把需求转成可评审的产品方向简报（不调用 LLM）。

- `pi-batch init`：创建用户级目录骨架（~/.config|.cache/.local/share/pbatch），
  打印目录布局与配置分层说明。
- `pi-batch version [--json]`：版本标识（pyproject.toml 单事实源）+ 运行环境。
- `pi-batch clean`：清空用户级缓存（~/.cache/pbatch），保留配置与密钥。
- `pi-batch completion bash|zsh`：输出 shell 补全脚本（注册表驱动）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

from . import user_dirs
from . import config
from .product import LEVELS, format_product_brief, product_brief
from .text_io import read_text_bounded
from .tools import list_subcommands
from .version import VERSION

# 产品名（与 pyproject 的 [project] name 一致；单事实源在 pyproject.toml）
PRODUCT = "pi-batch"


def _product_text(args, parser: argparse.ArgumentParser) -> str:
    """Read product requirements from --file or positional text."""
    if args.file and args.task:
        parser.error("use either positional product text or --file, not both")
    if args.file:
        try:
            return read_text_bounded(Path(args.file), config.INPUT_MAX_BYTES,
                                     "product source")
        except ValueError as exc:
            parser.error(str(exc))
    text = " ".join(args.task)
    if not text.strip():
        parser.error("Provide a product requirement (positional text or --file)")
    return text


def _write_product_output(target: Path, rendered: str) -> None:
    """Atomically write one bounded brief without following a target symlink."""
    payload = rendered + "\n"
    if len(payload.encode("utf-8")) > config.OUTPUT_MAX_BYTES:
        raise ValueError(
            f"product brief exceeds {config.OUTPUT_MAX_BYTES} output bytes")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f"product brief output is a symlink: {target}")
    descriptor, name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def product_main(argv: Optional[list] = None) -> int:
    """`pi-batch product`：生成产品方向简报（JSON 或 Markdown）。"""
    parser = argparse.ArgumentParser(
        prog="pi-batch.py product",
        description="Generate a deterministic product-direction brief; "
                    "unknown business decisions remain 待确认.")
    parser.add_argument("task", nargs="*", help="product requirement text")
    parser.add_argument("--file", default="", help="read requirement from FILE")
    parser.add_argument("--level", choices=LEVELS,
                        help="override the detected productization level")
    parser.add_argument("--json", action="store_true",
                        help="print a machine-readable product brief")
    parser.add_argument("-o", "--output", default="",
                        help="write the rendered brief to FILE")
    args = parser.parse_args(argv)
    text = _product_text(args, parser)
    try:
        brief = product_brief(text, args.level)
    except ValueError as exc:
        parser.error(str(exc))
    rendered = (json.dumps(brief, ensure_ascii=False, indent=2)
                if args.json else format_product_brief(brief))
    if args.output:
        target = Path(args.output)
        try:
            _write_product_output(target, rendered)
        except (OSError, ValueError) as exc:
            parser.error(f"unable to write product brief {target}: {exc}")
    else:
        print(rendered)
    return 0


def _version_from_pyproject() -> str:
    """兼容旧调用面的版本读取；实现集中在 pbatch.version。"""
    return VERSION


def init_main(argv: Optional[list] = None) -> int:
    """`pi-batch init`：用户级目录骨架 + 布局说明。"""
    parser = argparse.ArgumentParser(
        prog="pi-batch.py init",
        description="创建用户级工作目录（~/.config|.cache/.local/share/pbatch）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    dirs = user_dirs.ensure_user_dirs()
    settings = user_dirs.user_settings_path()
    if not settings.exists():
        settings.write_text(
            "# pi-batch 用户配置（分层：CLI > 项目 pi-batch.yaml > 此处 > 默认）\n"
            "# 示例：\n"
            "# agent:\n"
            "#   bin: pi\n"
            "#   default_workers: 4\n",
            encoding="utf-8",
        )
    if args.json:
        report = {k: str(v) for k, v in dirs.items()}
        report["settings"] = str(settings)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"# {PRODUCT} 用户目录已就绪")
        for kind, path in dirs.items():
            print(f"  {kind:<8} {path}")
        print(f"  settings {user_dirs.user_settings_path()}")
        print()
        print("分层（高优先）：CLI 参数 > 项目 pi-batch.yaml > "
              "~/.config/pbatch/settings.yaml > 内置默认")
        print("运行时状态（会话/记忆/被拒诊断/campaign 报告与日志）默认写入 "
              "~/.abr/projects/<workspace>，不污染项目仓库。")
        print("迁移设备密钥：旧项目内 devices/keys 请用 `pi-batch devices "
              "keymigrate` 导入。")
    raise SystemExit(0)


def version_main(argv: Optional[list] = None) -> int:
    """`pi-batch version [--json]`。"""
    parser = argparse.ArgumentParser(prog="pi-batch.py version",
                                     description="版本与环境信息")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    import platform
    report = {
        "product": PRODUCT,
        "version": _version_from_pyproject(),
        "python": platform.python_version(),
        "user_dirs": {k: str(v) for k, v in user_dirs.ensure_user_dirs().items()},
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"{PRODUCT} {report['version']} (python {report['python']})")
        for kind, path in report["user_dirs"].items():
            print(f"  {kind:<8} {path}")
    raise SystemExit(0)


def clean_main(argv: Optional[list] = None) -> int:
    """`pi-batch clean [--all]`：清缓存（默认）；--all 连用户配置一起。"""
    parser = argparse.ArgumentParser(
        prog="pi-batch.py clean",
        description="清空用户级缓存（保留配置与设备密钥）")
    parser.add_argument("--all", action="store_true",
                        help="连同配置、设备密钥和 ~/.abr runtime 一起清除")
    parser.add_argument("--yes", action="store_true", help="跳过确认")
    args = parser.parse_args(argv)
    cache = user_dirs.pbatch_cache_home()
    if args.all and not args.yes:
        reply = input(f"将删除 {cache} 及配置/密钥/runtime 目录。确认? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("clean: aborted")
            raise SystemExit(1)
    if cache.exists():
        shutil.rmtree(cache)
    if args.all:
        for path in (user_dirs.pbatch_config_home(),
                     user_dirs.pbatch_data_home(),
                     user_dirs.pbatch_runtime_home()):
            if path.exists():
                shutil.rmtree(path)
    print(f"clean: {'all user dirs' if args.all else 'cache'} removed")
    raise SystemExit(0)


def _completion_script(shell: str) -> str:
    """注册表驱动补全（bash/zsh 用同一命令清单生成）。"""
    commands = sorted(list_subcommands())
    if shell == "zsh":
        return (
            "#compdef pi-batch\n"
            "_pi_batch() {\n"
            "  local -a cmds\n"
            f"  cmds=({' '.join(commands)})\n"
            "  _describe 'command' cmds\n"
            "}\n"
            "compdef _pi_batch pi-batch\n"
        )
    return (
        "_pi_batch_complete() {\n"
        "  local cur prev\n"
        "  cur=\"${COMP_WORDS[COMP_CWORD]}\"\n"
        "  prev=\"${COMP_WORDS[COMP_CWORD-1]}\"\n"
        "  local cmds=\"%s\"\n"
        "  COMPREPLY=( $(compgen -W \"$cmds\" -- \"$cur\") )\n"
        "}\n"
        "complete -F _pi_batch_complete pi-batch\n" % " ".join(commands)
    )


def completion_main(argv: Optional[list] = None) -> int:
    """`pi-batch completion bash|zsh` → 输出补全脚本（重定向到 rc 文件）。"""
    parser = argparse.ArgumentParser(
        prog="pi-batch.py completion",
        description="生成 shell 补全（注册表驱动，命令清单自动同步）")
    parser.add_argument("shell", choices=("bash", "zsh"))
    args = parser.parse_args(argv)
    print(_completion_script(args.shell), end="")
    raise SystemExit(0)


def _doctor_config_checks() -> list:
    """Report parse and schema failures without starting any provider."""
    from .config_inspect import config_check
    report = config_check()
    labels = {"project": "项目配置", "user": "用户配置", "defaults": "打包默认配置"}
    checks = []
    for source in report["sources"]:
        name = source["name"]
        validation = report["validation"]["sources"].get(name, {})
        errors = source["errors"] + validation.get("errors", [])
        detail = "; ".join(errors) if errors else (
            source["path"] if source["loaded"] else "未创建（可选）")
        checks.append((labels[name], not errors, detail))
    return checks


def _doctor_checks() -> list:
    """环境诊断清单（每项: (name, ok, detail)）。"""
    from . import user_dirs, config
    checks = []
    checks.append(("安装", True, f"{PRODUCT} {_version_from_pyproject()}"))
    checks.append(("Python", True, __import__("platform").python_version()))
    for kind, path in user_dirs.ensure_user_dirs().items():
        checks.append((f"目录 {kind}", path.exists(), str(path)))
    checks.extend(_doctor_config_checks())
    agent_bin = config.AGENT_BIN
    import shutil
    agent_ok = shutil.which(agent_bin) is not None
    checks.append((f"agent 二进制 {agent_bin}", agent_ok,
                   shutil.which(agent_bin) or "PATH 中未找到"))
    keys = user_dirs.keys_dir()
    key_count = len(list(keys.glob("*.json"))) if keys.exists() else 0
    # 信息性：无密钥是 serve 前的正常状态（注册时生成），不阻断。
    checks.append(("设备密钥", True,
                   f"{key_count} 个（{keys}）" if key_count else "无（serve 前生成）"))
    import os
    from .lock import _lock_path
    lock = _lock_path(os.getcwd())
    checks.append(("实例锁", not lock.exists(),
                   "空闲" if not lock.exists() else "另一实例运行中"))
    return checks


def doctor_main(argv: Optional[list] = None) -> int:
    """`pi-batch doctor [--json]`：环境诊断（安装/目录/配置/agent/密钥/锁）。"""
    parser = argparse.ArgumentParser(
        prog="pi-batch.py doctor",
        description="环境诊断：安装、目录、配置分层、agent、密钥、锁")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    checks = _doctor_checks()
    if args.json:
        print(json.dumps([{"name": n, "ok": ok, "detail": d}
                          for n, ok, d in checks], ensure_ascii=False,
                         indent=2))
    else:
        for name, ok, detail in checks:
            print(f"  [{'OK' if ok else 'WARN'}] {name:<18} {detail}")
    failed = [n for n, ok, _ in checks if not ok]
    raise SystemExit(1 if failed else 0)
