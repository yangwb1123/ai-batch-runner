"""Suite-wide isolation for process-shared runtime state."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_provider_breaker(monkeypatch, tmp_path_factory):
    """Prevent fake-provider failures leaking across tests or into a repo."""
    state_dir = tmp_path_factory.mktemp("provider-breaker")
    monkeypatch.setenv(
        "PBATCH_PROVIDER_BREAKER_FILE",
        str(state_dir / "state.json"),
    )
