"""Validate bounded session-close responses and provide fixed user diagnostics."""

import math

from .hub_auth_feedback import error_status
from .hub_client import HubError
from .hub_session_client import _identifier, creation_key


ADMISSION_ERRORS = {
    "session_close_unsupported": "This instance does not support closing sessions.",
    "session_busy": "The session has an active prompt or computation; wait for it to finish.",
    "session_close_in_progress": "This session already has a close request; refresh its status.",
    "session_closed": "This session is already closed; its history is retained.",
    "session_close_failed": "Closing failed; the Gateway must restart before reclaiming the slot.",
    "session_unavailable": "This session is unavailable for closing.",
    "instance_offline": "The instance is offline.",
    "not_found": "The session or request was not found.",
}


def close_error(context, error):
    message = ADMISSION_ERRORS.get(getattr(error, "code", ""))
    return f"{context}. {message}" if message else error_status(context, error)


def close_key(value):
    try:
        return creation_key(value)
    except ValueError:
        raise ValueError("close key must contain 1 to 256 visible ASCII characters") from None


def close_target(value):
    _identifier(value)
    return value


def close_record(value, *, session_id="", instance_id="", request_id="") -> dict:
    try:
        result = _record(value)
        for field, expected in (("session_id", session_id), ("instance_id", instance_id),
                                ("request_id", request_id)):
            if expected and result[field] != expected:
                raise ValueError("response identity changed")
        return result
    except (KeyError, TypeError, ValueError, OverflowError):
        raise HubError("Hub returned an invalid session close response",
                       code="invalid_response") from None


def _record(value):
    if not isinstance(value, dict):
        raise ValueError("close request must be an object")
    for field in ("request_id", "instance_id", "session_id"):
        _identifier(value[field])
    errors = {"queued": {""}, "closed": {""}, "lost": {"owner_replaced"},
              "failed": {"session_close_failed", "session_unavailable"}}
    if value["error_code"] not in errors.get(value["status"], set()):
        raise ValueError("invalid close request outcome")
    for field in ("created_at", "updated_at"):
        number = value[field]
        if type(number) not in {int, float} or not math.isfinite(number) or number < 0:
            raise ValueError("invalid close timestamp")
    return {field: value[field] for field in (
        "request_id", "instance_id", "session_id", "status", "error_code", "created_at", "updated_at")}
