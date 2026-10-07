"""Control metadata registry: measured minima + who actually gets named.

``ui-specs/control-fit.json`` records each Iris control's inline ``minWidth``
together with an evidence pattern. Before a number is used it is re-checked
inside that component's own definition body in the installed package; a
mismatch drops the entry so the gate reports nothing rather than something
nobody can defend.

Scraping a minified bundle for "the component's min-width" is unsound and was
tried first: a fixed 12000-character window read IrisSelect back as 180px and
IrisTimePicker as 260px, and a 30000-character window read IrisSwitch as 140px
when its own track is 28/36/44. Hence the registry, verified per entry.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

REGISTRY = Path(__file__).resolve().parent.parent / "ui-specs" / "control-fit.json"

IRIS_PACKAGE_GLOBS = (
    "node_modules/@iris-ui-kit/vue/dist/index.js",
    "node_modules/.pnpm/@iris-ui-kit+vue@*/node_modules/@iris-ui-kit/vue/dist/index.js",
)

# 一行式组件定义的边界：dist 里的顶层定义都在行首。
_TOP_LEVEL_DEF = re.compile(r"^var (\w+) = (?:/\*@__PURE__\*/\s*)?defineComponent\(", re.M)


def component_segment(source: str, name: str) -> Optional[str]:
    """该组件自己的定义体：起点是它的顶层定义，终点是下一个顶层定义。"""
    bounds = [(m.group(1), m.start()) for m in _TOP_LEVEL_DEF.finditer(source)]
    for index, (defined, start) in enumerate(bounds):
        if defined != name:
            continue
        end = bounds[index + 1][1] if index + 1 < len(bounds) else len(source)
        return source[start:end]
    return None


def _registry_entries() -> dict:
    try:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = registry.get("controls")
    return entries if isinstance(entries, dict) else {}


def names_control_map() -> dict[str, bool]:
    """Which Iris controls actually let `aria-label` name the control.

    False means the attribute lands on a wrapper element instead: `IrisInput`,
    `IrisNumberInput` and `IrisSwitch` are all `inheritAttrs: false` and spread
    attrs onto their wrapper, so an `aria-label` written there names nothing.
    Measured by mounting the real package -- see
    sverpweb-stable/apps/web/src/pages/aps/settings/irisAriaLanding.real.test.tsx.
    """
    declared = _registry_raw().get("names_control") or {}
    return {name: value for name, value in declared.items() if isinstance(value, bool)}


def _registry_raw() -> dict:
    try:
        return json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _verified(source: str, entries: dict) -> dict[str, int]:
    """Keep only the entries whose evidence still matches the installed build."""
    widths: dict[str, int] = {}
    for control, spec in entries.items():
        if not isinstance(spec, dict):
            continue
        evidence = spec.get("evidence")
        value = spec.get("min_width")
        if not isinstance(evidence, str) or not isinstance(value, int):
            continue
        segment = component_segment(source, control)
        if segment is None or re.search(evidence, segment) is None:
            continue
        widths[control] = value
    return widths


def installed_vue_package(repo_root: Path) -> Optional[Path]:
    """Prefer active dependency links; never choose a stale virtual-store version."""
    for relative in ("apps/web/node_modules/@iris-ui-kit/vue", "node_modules/@iris-ui-kit/vue"):
        candidate = repo_root / relative
        if (candidate / "dist/index.js").is_file():
            return candidate.resolve()
    return None


def _installed_bundle(repo_root: Path) -> str:
    package = installed_vue_package(repo_root)
    try:
        return (package / "dist/index.js").read_text(encoding="utf-8") if package else ""
    except (OSError, UnicodeError):
        return ""


def control_min_widths(repo_root: Path) -> dict[str, int]:
    """Registry entries verified against the installed package, or dropped.

    Scraping a minified bundle for "the component's min-width" is unsound: a
    fixed character window bleeds neighbouring components in (measured:
    IrisSwitch read back as 140px when its own track is 28/36/44). So the
    numbers live in ``ui-specs/control-fit.json`` with an evidence pattern,
    and each entry is re-checked inside that component's own definition body
    before it is believed. A mismatch drops the entry -- the rule then skips
    instead of reporting a number nobody can defend.
    """
    entries = _registry_entries()
    if not entries:
        return {}
    source = _installed_bundle(repo_root)
    return _verified(source, entries) if source else {}


