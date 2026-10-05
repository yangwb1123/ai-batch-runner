"""Campaign budget and deadline that survive a supervisor restart.

A 7x24 campaign is normally driven by a supervisor loop, and the natural
implementation of that loop restarts the campaign process.  If the budget is
measured from *process* start, every restart silently extends the run — the
campaign then lives longer than the operator ever authorised, and nothing in
the logs says so.

Two origins are supported, both anchored outside the process:

* ``budget_seconds`` — measured from the first ``CAMPAIGN_RUN_STARTED`` row in
  the campaign state ledger, i.e. from the first time this campaign ever ran;
* ``deadline_at`` — an absolute ISO-8601 timestamp or epoch seconds, for an
  externally enforced wall-clock cutoff.

Both are opt-in; with neither configured the behaviour is unchanged.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

_RUN_STARTED = "CAMPAIGN_RUN_STARTED"
# Bounded: the origin row is found by scanning a bounded tail of the ledger.
_MAX_SCAN_BYTES = 4 * 1024 * 1024


def origin_epoch(state_path: Path | None) -> float | None:
    """First recorded campaign start, or None when the ledger has no row."""
    if state_path is None or not state_path.is_file():
        return None
    try:
        with state_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if _RUN_STARTED not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("status") != _RUN_STARTED:
                    continue
                stamp = _parse_time(row.get("updated_at"))
                if stamp is not None:
                    return stamp
    except OSError:
        return None
    return None


def _parse_time(value) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def parse_deadline(value: str) -> float | None:
    """Accept ISO-8601 or epoch seconds; reject anything else fail-closed."""
    if not str(value or "").strip():
        return None
    return _parse_time(str(value).strip())


def deadline_for(budget_seconds: int, deadline_at: str,
                 state_path: Path | None) -> tuple[float | None, str]:
    """Resolve the campaign cutoff and the origin label used for logging."""
    absolute = parse_deadline(deadline_at)
    if absolute is not None:
        return absolute, "absolute deadline"
    if budget_seconds and budget_seconds > 0:
        origin = origin_epoch(state_path)
        if origin is not None:
            return origin + budget_seconds, "first recorded campaign start"
        return time.time() + budget_seconds, "this process start (no ledger origin yet)"
    return None, ""


def expired(budget_seconds: int, deadline_at: str,
            state_path: Path | None) -> tuple[bool, str]:
    """(is_expired, human reason). Never raises: a stop decision, not a crash."""
    try:
        cutoff, origin = deadline_for(budget_seconds, deadline_at, state_path)
        if cutoff is None:
            return False, ""
        remaining = cutoff - time.time()
        if remaining > 0:
            return False, (f"campaign budget has {int(remaining // 60)} min left "
                           f"(origin: {origin})")
        return True, (f"campaign budget exhausted (origin: {origin}, "
                      f"cutoff {datetime.fromtimestamp(cutoff, timezone.utc).isoformat()})")
    except (OSError, ValueError, OverflowError):
        return False, ""


def state_path_for(root: Path, settings) -> Path:
    """The ledger a campaign actually writes to (config or private default)."""
    try:
        return settings.state_path(root)
    except (AttributeError, OSError, ValueError):
        return Path(root) / "docs" / "auto" / "state.jsonl"
