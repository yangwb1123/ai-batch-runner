"""Validation for the public asynchronous session-creation contract."""

from __future__ import annotations

import math
import unicodedata

from .hub_client import HubError


def session_name(value: str) -> str:
    if not isinstance(value, str) or any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise ValueError("session name must be text without control characters")
    name = value.strip()
    if not name or len(name.encode("utf-8")) > 256:
        raise ValueError("session name must contain 1 to 256 UTF-8 bytes")
    return name


def creation_key(value: str) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= 256
            or any(not 33 <= ord(char) <= 126 for char in value)):
        raise ValueError("creation key must contain 1 to 256 visible ASCII characters")
    return value


def creation_record(value, *, instance_id: str = "", request_id: str = "",
                    name: str = "") -> dict:
    try:
        result = _record(value)
        for field, expected in (("instance_id", instance_id), ("request_id", request_id),
                                ("name", name)):
            if expected and result[field] != expected:
                raise ValueError("response identity changed")
        return result
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise HubError("Hub returned an invalid session creation response",
                       code="invalid_response") from None


def _record(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("request must be an object")
    for field in ("request_id", "instance_id"):
        _identifier(value[field])
    if session_name(value["name"]) != value["name"]:
        raise ValueError("noncanonical name")
    state, session, error = value["status"], value["session_id"], value["error_code"]
    if state not in {"queued", "created", "failed", "lost"}:
        raise ValueError("invalid request status")
    if state == "created":
        _identifier(session)
    elif session != "":
        raise ValueError("unexpected session id")
    if state in {"queued", "created"} and error != "":
        raise ValueError("unexpected error")
    if state in {"failed", "lost"}:
        _identifier(error)
    for field in ("created_at", "updated_at"):
        number = value[field]
        if type(number) not in {int, float} or not math.isfinite(number) or number < 0:
            raise ValueError("invalid request timestamp")
    fields = ("request_id", "instance_id", "name", "status", "session_id",
              "error_code", "created_at", "updated_at")
    return {field: value[field] for field in fields}


def _identifier(value) -> None:
    if (not isinstance(value, str) or not 1 <= len(value) <= 256
            or value != value.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("invalid response identifier")
