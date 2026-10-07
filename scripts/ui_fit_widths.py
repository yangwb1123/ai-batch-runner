"""Whole-control SSR floors under the installed canonical light spacing tokens.

This is a static minimum, not browser text/skin/zoom geometry. Unknown units or
expressions remain unknown; do not borrow an inner input's 40px as a host floor.
"""
from __future__ import annotations

import ast
import operator
import re
from html.parser import HTMLParser


class WidthMarkup(HTMLParser):
    def __init__(self, markup: str):
        super().__init__()
        self.nodes = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.nodes.append((tag, dict(attrs)))


def _arithmetic(node):
    if isinstance(node, ast.Expression):
        return _arithmetic(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul}
    if isinstance(node, ast.BinOp) and type(node.op) in operations:
        return operations[type(node.op)](_arithmetic(node.left), _arithmetic(node.right))
    raise ValueError("unsupported width expression")


def length_px(value: str, spacing: dict) -> int | None:
    def variable(match):
        token, fallback = match.groups()
        return str(spacing.get(token, fallback or "unknown"))
    normalized = re.sub(r"var\((--[\w-]+)(?:,\s*([^()]+))?\)", variable, value)
    if normalized.startswith("calc(") and normalized.endswith(")"):
        normalized = normalized[5:-1]
    normalized = normalized.replace("px", "").strip()
    if not re.fullmatch(r"[\d.\s+*()-]+", normalized):
        return None
    try:
        number = _arithmetic(ast.parse(normalized, mode="eval"))
        return int(number) if number >= 0 and number == int(number) else None
    except (ValueError, SyntaxError, TypeError, OverflowError):
        return None


def control_floor(name: str, markup: str, spacing: dict) -> int | None:
    nodes = WidthMarkup(markup).nodes
    hooks = {"IrisNumberInput": "data-iris-number-input",
             "IrisSelect": "data-iris-select-trigger",
             "IrisDatePicker": "data-iris-date-picker-trigger",
             "IrisMonthPicker": "data-iris-month-picker-trigger"}
    for _tag, attrs in nodes:
        if hooks.get(name) not in attrs:
            continue
        styles = dict(pair.split(":", 1) for pair in attrs.get("style", "").split(";") if ":" in pair)
        value = styles.get("min-width", "")
        return length_px(value, spacing)
    # A switch track is its visible footprint; exclude the hidden native input.
    if name == "IrisSwitch":
        for tag, attrs in nodes:
            if tag == "span" and attrs.get("aria-hidden") == "true":
                match = re.search(r"(?:^|;)width:([^;]+)", attrs.get("style", ""))
                return length_px(match.group(1), spacing) if match else None
    return None


def floors_from_markup(records: dict, spacing: dict) -> dict:
    floors = {}
    for name, record in records.items():
        if not isinstance(record, dict):
            continue
        tiers = {tier: control_floor(name, markup, spacing)
                 for tier, markup in record.get("sizes", {}).items() if isinstance(markup, str)}
        known = {tier: number for tier, number in tiers.items() if number is not None}
        if known:
            floors[name] = known
    return floors
