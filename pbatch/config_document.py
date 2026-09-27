"""Bound YAML construction and admit only finite, JSON-compatible settings."""

from __future__ import annotations

import math


MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_CONFIG_DEPTH = 64
MAX_CONFIG_EXPANDED_NODES = 100_000
MAX_CONFIG_EXPANDED_BYTES = 8 * 1024 * 1024
MAX_NUMERIC_LITERAL_CHARS = 4096
MAX_CONFIG_INTEGER_BITS = 4096
_TAG_PREFIX = "tag:yaml.org,2002:"
_SCALAR_TAGS = frozenset(_TAG_PREFIX + name for name in
                        ("str", "null", "bool", "int", "float", "merge"))


def _fail(node, reason: str) -> None:
    mark = node.start_mark
    raise ValueError(f"{reason} at line {mark.line + 1}, column {mark.column + 1}")


def _children(node):
    if node.id == "mapping" and node.tag == _TAG_PREFIX + "map":
        for key, value in node.value:
            if key.tag not in {_TAG_PREFIX + "str", _TAG_PREFIX + "merge"}:
                _fail(key, "configuration mapping keys must be strings")
            yield key
            yield value
    elif node.id == "sequence" and node.tag == _TAG_PREFIX + "seq":
        yield from node.value
    else:
        _fail(node, "unsupported YAML type; use JSON-compatible values")


def _scalar_cost(node) -> tuple[int, int, int]:
    if node.tag not in _SCALAR_TAGS:
        _fail(node, "unsupported YAML type; use JSON-compatible values")
    if node.tag in {_TAG_PREFIX + "int", _TAG_PREFIX + "float"}:
        if len(node.value) > MAX_NUMERIC_LITERAL_CHARS:
            _fail(node, "numeric literal exceeds configuration limit")
    return 1, len(node.value.encode("utf-8")) + 2, 1


class _DocumentBudget:
    """Cost aliases by their expanded size without actually expanding them."""

    def __init__(self):
        self.active = set()
        self.costs = {}

    def visit(self, node, depth: int = 1) -> tuple[int, int, int]:
        identity = id(node)
        if identity in self.active:
            _fail(node, "cyclic YAML aliases are not supported")
        if depth > MAX_CONFIG_DEPTH:
            _fail(node, "configuration depth exceeds limit")
        if identity not in self.costs:
            self.active.add(identity)
            try:
                self.costs[identity] = self._measure(node, depth)
            finally:
                self.active.remove(identity)
        cost = self.costs[identity]
        if depth + cost[2] - 1 > MAX_CONFIG_DEPTH:
            _fail(node, "configuration depth exceeds limit")
        return cost

    def _measure(self, node, depth: int) -> tuple[int, int, int]:
        if node.id == "scalar":
            return _scalar_cost(node)
        count, size, height = 1, 2, 1
        for child in _children(node):
            child_count, child_size, child_height = self.visit(child, depth + 1)
            count += child_count
            size += child_size + 2
            height = max(height, child_height + 1)
            if count > MAX_CONFIG_EXPANDED_NODES:
                _fail(node, "configuration expanded node count exceeds limit")
            if size > MAX_CONFIG_EXPANDED_BYTES:
                _fail(node, "configuration expanded byte size exceeds limit")
        return count, size, height


def _check_constructed(value, visited: set) -> None:
    """Reject nonfinite numbers and non-JSON scalars after safe construction."""
    if type(value) in (dict, list):
        identity = id(value)
        if identity in visited:
            return
        visited.add(identity)
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("configuration mapping keys must be strings")
            values = value.values()
        else:
            values = value
        for child in values:
            _check_constructed(child, visited)
    elif type(value) is float and not math.isfinite(value):
        raise ValueError("configuration numbers must be finite")
    elif type(value) is int and value.bit_length() > MAX_CONFIG_INTEGER_BITS:
        raise ValueError("configuration integer exceeds supported size")
    elif type(value) not in (str, bool, int, float, type(None)):
        raise ValueError("unsupported YAML type; use JSON-compatible values")


def load_config_yaml(text: str, yaml_module) -> dict:
    """Check the node graph before SafeLoader can flatten YAML merge aliases."""
    loader = yaml_module.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return {}
        _DocumentBudget().visit(node)
        try:
            data = loader.construct_document(node)
        except (ValueError, KeyError, TypeError, OverflowError):
            _fail(node, "invalid YAML scalar value")
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError("top-level YAML value must be a mapping")
        _check_constructed(data, set())
        return data
    finally:
        loader.dispose()
