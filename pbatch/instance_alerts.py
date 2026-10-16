"""Durable alert bookkeeping for flagged workspaces, with honest delivery.

The instance view already knows which workspaces are flagged. What it lacks is
a memory across invocations, so the same condition gets re-announced every time
somebody looks, or — worse — gets suppressed after a delivery that never
happened. This keeps that memory in one small, bounded, private file.

Three rules:

* **Anti-spam is per flag and per repeat window, not per run.** An operator
  wants to hear about a stuck instance repeatedly, and not to hear about it
  continuously.
* **A flag that clears resolves itself.** Entries are dropped as soon as the
  condition is gone, so the ledger cannot accumulate ghosts of incidents that
  were fixed days ago. There is deliberately no manual acknowledge step yet:
  the condition is the state, and inventing an ack vocabulary before there is
  evidence about what needs acknowledging would be guessing.
* **Delivery must be observable to be countable.** This posts synchronously
  with a deadline, unlike ``metering._post_webhook``, which is deliberately
  fire-and-forget for the hot path. Marking something notified without knowing
  it arrived would turn a failed webhook into permanent silence.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config
from .config import log

ALERTS_SCHEMA_VERSION = 1
MAX_ALERTS = 500
DEFAULT_REPEAT_SECONDS = 3600
DELIVERY_TIMEOUT_SECONDS = 5

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def alerts_path(cwd: str = "") -> Path:
    """Private alert state for this workspace host, never in the project."""
    return config.data_dir(cwd) / "ops" / "instance-alerts.json"


def alert_key(workspace: str, flag: str) -> str:
    return f"{workspace}:{flag}"


def sync_alerts(rows: list[dict], now: Optional[int] = None,
                repeat_seconds: int = DEFAULT_REPEAT_SECONDS,
                cwd: str = "", *,
                resolvable_workspaces: Optional[set[str]] = None) -> tuple[list[dict], list[str]]:
    """Reconcile stored alerts against currently flagged rows.

    Returns ``(due, resolved)``: alerts that should be delivered now, and keys
    that cleared and were dropped. Nothing is written here — delivery decides
    whether an alert was actually announced.
    """
    moment = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    stored = _load(cwd)
    flagged = {alert_key(row["workspace"], row["flag"]): row
               for row in rows if row.get("flag")}
    due: list[dict] = []
    for key, row in flagged.items():
        entry = stored.get(key)
        last = int((entry or {}).get("last_notified") or 0)
        if entry and moment - last < max(0, repeat_seconds):
            continue
        due.append({"key": key, "workspace": row["workspace"],
                    "flag": row["flag"], "reason": row["flag_reason"],
                    "idle_seconds": row["idle_seconds"],
                    "first_seen": int((entry or {}).get("first_seen") or moment),
                    "repeat": bool(entry)})
    resolved = [key for key in stored if key not in flagged and
                (resolvable_workspaces is None or
                 key.rsplit(":", 1)[0] in resolvable_workspaces)]
    return due, sorted(resolved)


def mark_notified(keys: list[str], now: Optional[int] = None, cwd: str = "") -> int:
    """Record these alerts as announced. Returns how many were stored."""
    moment = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    stored = _load(cwd)
    for key in keys:
        entry = stored.get(key) or {}
        entry.setdefault("first_seen", moment)
        entry["last_notified"] = moment
        entry["notify_count"] = int(entry.get("notify_count") or 0) + 1
        stored[key] = entry
    stored = _cap(stored)
    _save(stored, cwd)
    return len(stored)


def drop_alerts(keys: list[str], cwd: str = "") -> int:
    """Forget resolved alerts. Returns how many remain."""
    stored = _load(cwd)
    for key in keys:
        stored.pop(key, None)
    _save(stored, cwd)
    return len(stored)


def deliver(payload: dict, url: str) -> bool:
    """POST one alert. Bounded, and never raises: delivery failure is data."""
    if not url:
        return False
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=DELIVERY_TIMEOUT_SECONDS):
            return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("instance alert delivery failed: %s", exc)
        return False


def notify(rows: list[dict], url: str = "", repeat_seconds: int = DEFAULT_REPEAT_SECONDS,
           cwd: str = "", now: Optional[int] = None, *,
           resolvable_workspaces: Optional[set[str]] = None) -> dict:
    """Reconcile, deliver what is due, and record only what was announced.

    An alert whose delivery failed is *not* marked notified, so the next run
    retries it instead of staying silent forever because an endpoint was down.
    """
    moment = int(now if now is not None else datetime.now(timezone.utc).timestamp())
    due, resolved = sync_alerts(rows, moment, repeat_seconds, cwd,
                                resolvable_workspaces=resolvable_workspaces)
    sent: list[str] = []
    for alert in due:
        payload = {"kind": "pbatch.instance.flagged",
                   "schema_version": ALERTS_SCHEMA_VERSION,
                   "ts": datetime.fromtimestamp(moment, timezone.utc).strftime(_TS_FORMAT),
                   **alert}
        if deliver(payload, url) or not url:
            sent.append(alert["key"])
    if resolved:
        drop_alerts(resolved, cwd)
    if sent:
        mark_notified(sent, moment, cwd)
    return {"due": len(due), "sent": len(sent), "resolved": len(resolved),
            "delivered": url != "", "keys": sent}


def _load(cwd: str = "") -> dict:
    path = alerts_path(cwd)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if (not isinstance(document, dict)
            or document.get("schema_version") != ALERTS_SCHEMA_VERSION):
        return {}
    alerts = document.get("alerts")
    return dict(alerts) if isinstance(alerts, dict) else {}


def _save(stored: dict, cwd: str = "") -> None:
    path = alerts_path(cwd)
    document = {"schema_version": ALERTS_SCHEMA_VERSION, "alerts": stored}
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False,
                                        sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        # Losing alert memory must not lose the alert itself: the condition is
        # re-derived from the ledgers next run.
        log.warning("instance alert state not persisted: %s", exc)


def _cap(stored: dict) -> dict:
    """Bound the file by dropping the least recently announced alerts."""
    if len(stored) <= MAX_ALERTS:
        return stored
    ordered = sorted(stored.items(),
                     key=lambda item: int(item[1].get("last_notified") or 0),
                     reverse=True)
    return dict(ordered[:MAX_ALERTS])