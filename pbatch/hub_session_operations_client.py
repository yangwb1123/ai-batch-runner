"""Bounded, exact request-history responses without business replay."""

import json
import unicodedata
import urllib.parse

from .hub_auth_feedback import error_status
from .hub_client import HubError
from .hub_session_client import _identifier, creation_record
from .hub_session_close_client import close_record


MAX_RESPONSE_BYTES = 256 * 1024
FIELDS = {"operation", "request_id", "instance_id", "name", "status", "session_id",
          "error_code", "created_at", "updated_at"}


def visible(value):
    return "".join(f"\\u{ord(char):04x}" if unicodedata.category(char) in {"Cc", "Cf"}
                   else char for char in str(value))


def cursor(value):
    if (not isinstance(value, str) or len(value) > 2048
            or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
                   for char in value)):
        raise ValueError("history cursor must be bounded base64url text")
    return value


def _history_identifier(value):
    _identifier(value)
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("invalid history identifier") from None


def query(instance_id, before, limit):
    if instance_id != "":
        _history_identifier(instance_id)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("history limit must be between 1 and 50")
    result = {"limit": limit}
    if instance_id:
        result["instance_id"] = instance_id
    if cursor(before):
        result["before"] = before
    return urllib.parse.urlencode(result)


def operation_record(value):
    try:
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise ValueError("invalid history fields")
        for field in ("request_id", "instance_id", "session_id"):
            if field != "session_id" or value[field] != "":
                _history_identifier(value[field])
        operation = value["operation"]
        if operation == "create":
            result = creation_record(value)
            errors = {"queued": {""}, "created": {""}, "lost": {"owner_replaced"},
                      "failed": {"session_creation_failed", "session_capacity"}}
            if result["error_code"] not in errors[result["status"]]:
                raise ValueError("invalid creation outcome")
        elif operation == "close" and value["name"] == "":
            result = dict(close_record(value), name="")
        else:
            raise ValueError("invalid operation")
        return dict(result, operation=operation)
    except (KeyError, ValueError, TypeError, OverflowError, HubError):
        raise HubError("Hub returned invalid session request history", code="invalid_response") from None


def page(value, *, instance_id="", before="", limit=20):
    try:
        if not isinstance(value, dict) or set(value) != {"data", "next_cursor"}:
            raise ValueError("invalid history envelope")
        rows, next_cursor = value["data"], cursor(value["next_cursor"])
        if not isinstance(rows, list) or len(rows) > limit or (not rows and next_cursor):
            raise ValueError("invalid history page size")
        if next_cursor and next_cursor == before:
            raise ValueError("repeated history cursor")
        records = [operation_record(row) for row in rows]
        identities = {(row["operation"], row["request_id"]) for row in records}
        if len(identities) != len(records):
            raise ValueError("duplicate history request")
        if instance_id and any(row["instance_id"] != instance_id for row in records):
            raise ValueError("history instance changed")
        return {"data": records, "next_cursor": next_cursor}
    except (KeyError, ValueError, TypeError, OverflowError, HubError):
        raise HubError("Hub returned invalid session request history", code="invalid_response") from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate history field")
        result[key] = value
    return result


def read_page(client, instance_id, before, limit):
    encoded = query(instance_id, before, limit)
    raw = client._request("GET", "/session-operations?" + encoded,
                          response_limit=MAX_RESPONSE_BYTES, raw_response=True)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise HubError("Hub returned invalid session request history", code="invalid_response") from None
    return page(value, instance_id=instance_id, before=before, limit=limit)


def refreshed_record(prior, detail):
    value = dict(detail, operation=prior["operation"])
    if prior["operation"] == "close":
        value["name"] = ""
    result = operation_record(value)
    fields = ["request_id", "instance_id", "name", "created_at"]
    if prior["session_id"]:
        fields.append("session_id")
    if any(result[field] != prior[field] for field in fields):
        raise HubError("Hub changed the selected request identity", code="invalid_response")
    return result


def history_error(error):
    if isinstance(error, HubError) and error.status in {404, 405, 501}:
        return "This Hub does not support session request history. Other session controls remain available."
    return error_status("Session requests unavailable; refresh to retry", error)
