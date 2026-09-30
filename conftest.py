"""Suite-wide isolation for process-shared state.

These fixtures live at the repository root so they also cover `checks/`, which
`pyproject.toml` puts in `testpaths` alongside `tests/`. A leak from one
directory that the other directory's gate then trips is still a leak.
"""

from __future__ import annotations

import os

import pytest


# Runner defaults a test may legitimately retarget at a fake agent, restored
# afterwards. A leaked value does not fail the test that set it — it silently
# re-aims every later test in the session, which is how a temp agent path once
# reached meta routing and made an unrelated planner assertion blow up.
MUTABLE_CONFIG = (
    "AGENT_BIN", "MEMORY_MODE", "STREAM_OUTPUT", "AGENT_MODEL",
    "AGENT_PROVIDER", "AGENT_THINKING_OVERRIDE", "AGENT_LEDGER_ENABLED",
)

#: The entry shim sets this in `os.environ` while it is being imported.
MUTABLE_ENV = ("PBATCH_SCRIPT_DIR",)


@pytest.fixture(autouse=True)
def _restore_mutable_config():
    """Snapshot process-shared config globals and restore them per test.

    Restored with plain ``setattr`` on purpose: routing the restore through
    ``monkeypatch`` would record it, and monkeypatch's own finalization runs
    last and would undo it — reinstating the very leak this guards.
    """
    from pbatch import config

    saved = {name: getattr(config, name, None) for name in MUTABLE_CONFIG}
    yield
    for name, value in saved.items():
        setattr(config, name, value)


@pytest.fixture(autouse=True)
def _restore_mutable_environment():
    """Snapshot the environment the entry shim rewrites on import.

    `pi-batch.py` calls `os.environ.setdefault("PBATCH_SCRIPT_DIR", ...)` as
    soon as it is imported, so a test that loads the shim in-process leaves
    project-config discovery pointing at the tool checkout for the rest of the
    session. The project-config gate then reads the tool's own registry instead
    of the project's file, and fails for a reason that has nothing to do with
    the test that leaked.
    """
    saved = {name: os.environ.get(name) for name in MUTABLE_ENV}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
