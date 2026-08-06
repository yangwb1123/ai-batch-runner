#!/usr/bin/env python3
"""Snaplink Suite 组合仓库门禁（纯标准库，零依赖）。

校验：
  1. compatibility.yaml 结构完整（suite_version / components / topologies /
     contracts / integration_levels 必须存在且非空）
  2. topologies.components 引用的组件名必须在 components 中定义
  3. topologies.compose 引用的 compose 文件必须存在
  4. compose 文件中必须定义对应拓扑要求的服务名（粗校验：service 名出现）
  5. contracts 版本号格式（vN 或 semver）

用法：
  python scripts/suite-check.py            # 检查当前仓库
  python scripts/suite-check.py --json     # JSON 报告（供 pi-batch 验证器）
退出码：0 = 通过；1 = 门禁失败；2 = 配置/用法错误。
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r"^(v?\d+(\.\d+){0,2})|(>=[0-9]+\.[0-9]+ <[0-9]+\.[0-9]+)$")


def parse_mini_yaml(text: str) -> dict:
    """解析本仓库 YAML 的受限子集：顶层 key、2 空格缩进的嵌套 dict、
    '- ' 列表项、单行 flow list [a, b]。空值挂载点按下一行类型
    惰性物化为 dict 或 list；其余结构报错（fail closed）。"""
    root: dict = {}
    # stack: (indent, container, mount_key) — mount_key 非空时表示
    # container[mount_key] 是本层的惰性挂载点。
    stack = [(0, root, None)]
    for lineno, raw in enumerate(text.splitlines(), 1):
        entry = _clean_line(raw, lineno)
        if entry is None:
            continue
        indent, content = entry
        _trim_stack(stack, indent, lineno)
        _dispatch_line(stack, indent, content, lineno)
    return root


def _clean_line(raw: str, lineno: int):
    """去注释与首尾空白；返回 (indent, content) 或 None（空行）。"""
    line = raw.split("#", 1)[0].rstrip()
    if not line.strip():
        return None
    indent = len(line) - len(line.lstrip(" "))
    if indent % 2 != 0:
        raise ValueError(f"line {lineno}: indent must be 2-space multiples")
    return indent, line.strip()


def _trim_stack(stack: list, indent: int, lineno: int):
    """弹出缩进 >= 当前行的层级；栈空说明结构不一致。"""
    while len(stack) > 1 and indent <= stack[-1][0]:
        stack.pop()
    if not stack:
        raise ValueError(f"line {lineno}: inconsistent indentation")


def _dispatch_line(stack: list, indent: int, content: str, lineno: int):
    """按行类型分派：列表项 / flow list / 标量 / 空值挂载点。"""
    _, container, mount_key = stack[-1]
    target = _mount(container, mount_key)
    if content.startswith("- "):
        _append_item(target, container, mount_key, content, lineno)
        return
    if ":" not in content:
        raise ValueError(f"line {lineno}: expected 'key:' mapping entry")
    key, _, value = content.partition(":")
    key = key.strip()
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        _put_flow_list(target, key, value)
        return
    value = value.strip('"')
    if value:
        _put_scalar(target, key, value, lineno)
        return
    if isinstance(target, dict):
        target[key] = None
        stack.append((indent, target, key))
    else:
        new_container: dict = {}
        target.append(new_container)
        stack.append((indent, new_container, None))


def _mount(container, mount_key):
    """惰性物化：挂载点值为 None 时先变成空 dict，返回实际容器。"""
    if isinstance(container, dict) and mount_key is not None:
        if container.get(mount_key) is None:
            container[mount_key] = {}
        return container[mount_key]
    return container


def _append_item(target, container, mount_key, content: str, lineno: int):
    """处理 '- ' 列表项；若挂载点是空 dict 则转换为 list。"""
    if isinstance(target, dict):
        if mount_key is not None and container.get(mount_key) == {}:
            container[mount_key] = []
            target = container[mount_key]
        else:
            raise ValueError(f"line {lineno}: list item outside a list")
    if not isinstance(target, list):
        raise ValueError(f"line {lineno}: list item outside a list")
    target.append(content[2:].strip().strip('"'))
    return target


def _put_flow_list(target, key: str, value: str):
    """单行 flow list: [a, b, c] → 列表。"""
    items = [i.strip().strip('"') for i in value[1:-1].split(",") if i.strip()]
    if isinstance(target, dict):
        target[key] = items
    else:
        target.append(items)


def _put_scalar(target, key: str, value: str, lineno: int):
    """标量赋值；拒绝重复 key（空列表挂载点除外）。"""
    if isinstance(target, dict):
        if key in target and not (isinstance(target[key], list) and not target[key]):
            raise ValueError(f"line {lineno}: duplicate key '{key}'")
        target[key] = value
    else:
        target.append(value)


def load_config(root: Path) -> dict:
    path = root / "compatibility.yaml"
    if not path.is_file():
        raise ValueError("compatibility.yaml not found (run from suite root)")
    return parse_mini_yaml(path.read_text(encoding="utf-8"))


def check(config: dict, root: Path) -> list:
    errors: list = []
    for section in ("suite_version", "components", "topologies", "contracts",
                    "integration_levels"):
        if section not in config or not config[section]:
            errors.append(f"missing or empty section: {section}")
    if errors:
        return errors

    components = config["components"]
    for name, window in components.items():
        if not isinstance(window, str) or not VERSION_RE.match(window):
            errors.append(f"components.{name}: bad version window '{window}'")

    topo = config["topologies"]
    if not isinstance(topo, dict):
        return ["topologies must be a mapping"]
    for name, spec in topo.items():
        errors += _check_topology(name, spec, components, root)

    for contract, version in config.get("contracts", {}).items():
        if not isinstance(version, str) or not re.match(r"^v?\d+(\.\d+){0,2}$", version):
            errors.append(f"contracts.{contract}: bad version '{version}'")
    return errors


def _check_topology(name: str, spec, components: dict, root: Path) -> list:
    """校验一个拓扑：组件必须已定义、compose 文件必须存在且含全部服务。"""
    errors: list = []
    if not isinstance(spec, dict):
        return [f"topologies.{name}: must be a mapping"]
    comps = spec.get("components")
    if not isinstance(comps, list) or not comps:
        return [f"topologies.{name}: components must be a non-empty list"]
    for c in comps:
        if c not in components:
            errors.append(f"topologies.{name}: unknown component '{c}'")
    compose = spec.get("compose")
    if not compose:
        errors.append(f"topologies.{name}: missing compose file")
        return errors
    compose_path = root / compose
    if not compose_path.is_file():
        errors.append(f"topologies.{name}: compose file missing: {compose}")
        return errors
    text = compose_path.read_text(encoding="utf-8", errors="replace")
    for c in comps:
        # 粗校验：compose 服务名必须包含组件名（snaplink-console -> console
        # 需在 compose 中显式写全名，见各拓扑文件约定）
        if f"{c}:" not in text and f"  {c}" not in text:
            errors.append(f"topologies.{name}: compose '{compose}' lacks service '{c}'")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=".", help="suite repo root (default: cwd)")
    parser.add_argument("--json", action="store_true", help="emit JSON report")
    args = parser.parse_args()
    root = Path(args.dir).resolve()
    config: dict = {}
    try:
        config = load_config(root)
        errors = check(config, root)
    except ValueError as exc:
        errors = [str(exc)]
    report = {
        "ok": not errors,
        "errors": errors,
        "suite_version": config.get("suite_version", "") if isinstance(config, dict) else "",
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for e in errors:
            print(f"SUITE-CHECK: {e}")
        print("SUITE-CHECK: OK" if report["ok"] else "SUITE-CHECK: FAILED")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
