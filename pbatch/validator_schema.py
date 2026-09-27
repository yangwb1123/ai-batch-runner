"""Compile declarative structured-artifact validator syntax."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Optional

from .launcher import is_frozen, script_argv


SCHEMA_PREFIX = "jsonschema="


def schema_validator_command(value: str) -> Optional[str]:
    """Return a portable checker command for ``jsonschema=PATH`` values."""
    if not value.startswith(SCHEMA_PREFIX):
        return None
    schema_path = value[len(SCHEMA_PREFIX):].strip()
    checker = Path(__file__).with_name("json_schema_cli.py").resolve()
    invocation = (shlex.join(script_argv("scripts/check-json-schema.py"))
                  if is_frozen() else f"python {shlex.quote(str(checker))}")
    return (
        f"{invocation} --schema "
        f"{shlex.quote(schema_path)} {{output}}"
    )
