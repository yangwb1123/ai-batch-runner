"""Bounded event delivery with per-session rejection and shutdown-safe receipts."""

from __future__ import annotations

import time

from .hub_client import HubError
from .hub_gateway_protocol import field, is_fencing_error, nested_text


def _rejected_event(gateway, row, error):
    event = row["body"]
    running = event.get("kind") == "turn.running"
    obsolete = (error.code == "session_read_only" and error.status == 409) or (
        error.code == "turn_terminal" and running)
    with gateway._session_lock:
        if gateway._stop.is_set():
            return "stopped"
        if obsolete:
            gateway._store.obsolete_event(row["event_id"])
            turn_id = field(event, "turn_id")
            gateway._pending_starts.pop(turn_id, None)
            receipt = gateway._store.turn(turn_id)
            if running and receipt and receipt["state"] in {"accepted", "starting"}:
                gateway._store.set_turn(turn_id, "cancelled")
            return "obsolete"
        gateway._store.note_event_failure(row["event_id"])
    if is_fencing_error(error):
        gateway._fence_and_stop(error)
        raise error
    return "retry"


def _acknowledge_event(gateway, row, response):
    with gateway._session_lock:
        if gateway._stop.is_set():
            return False
        cursor = nested_text(response, "cursor")
        if not cursor:
            gateway._store.note_event_failure(row["event_id"])
            raise HubError("event acknowledgement omitted cursor", code="invalid_response")
        gateway._store.mark_event_sent(row["event_id"], cursor)
        return True


def post_running_ack(gateway, row):
    try:
        response = gateway.client.send_event(row["body"],
                                             instance_id=gateway.instance_id,
                                             generation=gateway.generation)
    except HubError as error:
        if _rejected_event(gateway, row, error) != "retry":
            return None
        raise
    return response if _acknowledge_event(gateway, row, response) else None


def flush_outbox(gateway):
    deadline = time.monotonic() + 0.5
    with gateway._session_lock:
        if gateway._stop.is_set():
            return
        rows = gateway._store.pending_events(limit=32)
    for row in rows:
        with gateway._session_lock:
            if gateway._stop.is_set() or time.monotonic() >= deadline:
                return
            event = row["body"]
            if event.get("kind") == "turn.running" and field(event, "turn_id") in gateway._pending_starts:
                continue
        try:
            response = gateway.client.send_event(event, instance_id=gateway.instance_id,
                                                 generation=int(row["generation"]))
        except HubError as error:
            if _rejected_event(gateway, row, error) == "obsolete":
                continue
            return
        if not _acknowledge_event(gateway, row, response):
            return
