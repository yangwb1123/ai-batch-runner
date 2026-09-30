"""Isolation for the repository's own engineering gates.

`tests/conftest.py` carries the same environment guard for the regression
suite; a root conftest would cover both at once, but the root file budget in
`engineering.yaml` exists precisely to keep such shared files out of the root.
"""

from __future__ import annotations

import os

import pytest


#: The entry shim sets this in `os.environ` while it is being imported.
MUTABLE_ENV = ("PBATCH_SCRIPT_DIR",)


@pytest.fixture(autouse=True)
def _restore_mutable_environment():
    """Snapshot the environment the entry shim rewrites on import.

    `pi-batch.py` calls `os.environ.setdefault("PBATCH_SCRIPT_DIR", ...)` as
    soon as it is imported, so a test that loads the shim in-process leaves
    project-config discovery pointing at the tool checkout for the rest of the
    session. These gates then read the tool's own registry instead of the
    project's file, and fail for a reason that has nothing to do with the test
    that leaked.
    """
    saved = {name: os.environ.get(name) for name in MUTABLE_ENV}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
