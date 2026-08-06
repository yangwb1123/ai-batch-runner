"""平台组合机制共享件：受限 YAML 解析 + platform-map 加载。

供 check-decision-summary.py / check-change-manifest.py 使用（纯标准库）。
只解析本套件 YAML 的受限子集：顶层 key、2 空格缩进嵌套、'- ' 列表、
单行 flow list [a, b]；其余结构报错（fail closed）。
"""

from __future__ import annotations

from pathlib import Path


def parse_mini_yaml(text: str) -> dict:
    """解析受限 YAML 子集；空值挂载点按下一行类型惰性物化为 dict/list。"""
    root: dict = {}
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


def load_platform_map(root: Path) -> dict:
    """加载 platform-map.yaml；缺失或损坏抛 ValueError。"""
    path = root / "platform-map.yaml"
    if not path.is_file():
        raise ValueError("platform-map.yaml not found")
    return parse_mini_yaml(path.read_text(encoding="utf-8"))


def project_names(pmap: dict) -> set:
    return set((pmap.get("projects") or {}).keys())
