"""Small text and fallback-result helpers shared by the product TUI."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any


_ANSI_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def clean_text(value: Any) -> str:
    return _ANSI_ESCAPE.sub("", str(value or "")).replace(
        "\r\n", "\n").replace("\r", "\n")


def one_line(value: str) -> str:
    return " ".join(value.split())


def inline_label(value: Any) -> str:
    label = " ".join(str(value or "").split())
    return label if label else "<none>"


def short_path(value: str, width: int) -> str:
    path = str(value)
    return path if len(path) <= width else "…" + path[-(width - 1):]


def is_codex_binary(value: Any) -> bool:
    try:
        binary = shlex.split(str(value or ""))[0]
    except (IndexError, ValueError):
        binary = str(value or "")
    return "codex" in Path(binary).name.lower()


def is_pi_binary(value: Any) -> bool:
    """Return true only for the canonical Pi executable basename."""
    return Path(str(value or "")).name.lower() in {"pi", "pi.exe"}


def render_entry(entry: Any, busy: bool) -> str:
    labels = {"user": "You", "assistant": "Assistant",
              "system": "System", "error": "Error"}
    body = entry.text
    if entry.role == "assistant" and not body:
        body = "_Working…_" if busy else "_No textual response._"
    if entry.role == "error":
        body = "\n".join(f"> {line}" for line in body.splitlines())
    return f"## {labels.get(entry.role, entry.role.title())}\n\n{body}"


def result_fields(result: Any) -> tuple[bool, str, str, str]:
    if result is None:
        return True, "", "", ""
    if isinstance(result, str):
        return True, clean_text(result), "", ""
    getter = (result.get if isinstance(result, dict)
              else lambda key, default=None: getattr(result, key, default))
    success = bool(getter("success", True))
    return (success, clean_text(getter("stdout", "")),
            clean_text(getter("stderr", "")), clean_text(getter("reason", "")))


def snapshot_scalars(snapshot: dict[str, Any]) -> list[tuple[str, Any]]:
    values = []
    for key, value in snapshot.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            values.append((str(key), value))
        if len(values) >= 16:
            break
    return values
