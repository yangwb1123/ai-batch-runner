"""Shared config + logging for pi-batch.py and its pi_batch_* helper modules.

Split out of pi-batch.py (was 918 lines, over the 500-line code-file gate)
per CLAUDE.md's "先拆分,再继续" rule -- pure extraction, no behavior change.

`AGENT_BIN` is mutated at runtime by pi-batch.py's --agent-bin flag, so
importers must reference it as `cfg.AGENT_BIN` (module-attribute access via
`import pi_batch_config as cfg`), never `from pi_batch_config import
AGENT_BIN` -- the latter binds a stale copy at import time and would
silently ignore the CLI override.
"""

from __future__ import annotations

import logging
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


def _load_batch_config(path: str = "pi-batch.yaml") -> dict:
    """Optional defaults for pi-batch.py. Missing file -> {} (built-in
    defaults below apply), so the script still runs standalone with zero
    config -- copy pi-batch.yaml alongside pi-batch.py to point it at a
    different agent CLI."""
    p = Path(path)
    if not yaml or not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


_BATCH_CFG = _load_batch_config()
_AGENT_CFG = _BATCH_CFG.get("agent", {})
AGENT_BIN = _AGENT_CFG.get("bin", "pi")
AGENT_DEFAULT_MODEL = _AGENT_CFG.get("default_model", "")
AGENT_DEFAULT_TIMEOUT = _AGENT_CFG.get("default_timeout", 300)
AGENT_DEFAULT_WORKERS = _AGENT_CFG.get("default_workers", 4)
COMMIT_PREFIX_DEFAULT = _BATCH_CFG.get("commit", {}).get("prefix", "[pi-batch]")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pi-batch")
