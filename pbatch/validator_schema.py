"""Compile declarative structured-artifact validator syntax."""

from __future__ import annotations

import shlex
from typing import Optional


SCHEMA_PREFIX = "jsonschema="


def schema_validator_command(value: str) -> Optional[str]:
    """Return a portable checker command for ``jsonschema=PATH`` values."""
    if not value.startswith(SCHEMA_PREFIX):
        return None
    schema_path = value[len(SCHEMA_PREFIX):].strip()
    return (
        "python {tool_root}/scripts/check-json-schema.py --schema "
        f"{shlex.quote(schema_path)} {{output}}"
    )
