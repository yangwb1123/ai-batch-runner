"""Environment boundary for provider-owned child processes."""

from __future__ import annotations

import os
from collections.abc import Mapping


_HUB_CONTROL_PREFIX = "PBATCH_HUB_"


def provider_child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Copy an environment while keeping Hub control-plane settings private.

    All PBATCH_HUB_* values belong to the local control plane, not the agent
    provider. Other variables, including provider API keys, remain available.
    The caller's mapping and os.environ are never modified.
    """
    environment = dict(os.environ if base is None else base)
    return {key: value for key, value in environment.items()
            if not key.startswith(_HUB_CONTROL_PREFIX)}
