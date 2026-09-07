"""Safe parsing helpers for meta-stage role plans."""

from __future__ import annotations

import json

from .config import log


def _extract_json_array(text: str) -> str:
    """Return the first JSON array literal in *text* using a
    bracket/string-aware scan."""
    start = (text or "").find("[")
    if start == -1:
        return ""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        char = text[i]
        if in_str:
            if esc:
                esc = False
            elif char == "\\":
                esc = True
            elif char == '"':
                in_str = False
            continue
        if char == '"':
            in_str = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def _parse_role_plan(stdout: str) -> list:
    """Parse the orchestrator's JSON role array; malformed output is empty."""
    arr = _extract_json_array(stdout)
    plan = []
    if arr:
        try:
            data = json.loads(arr)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str) and item.strip():
                        plan.append({"role": item.strip(), "task": ""})
                    elif isinstance(item, dict) and str(item.get("role", "")).strip():
                        plan.append({"role": str(item["role"]).strip(),
                                     "task": str(item.get("task", "")).strip()})
                return plan
        except json.JSONDecodeError:
            pass
    if not plan:
        log.warning("META orchestrator output did not contain a JSON role plan; treating as complete")
    return []
