"""Explicit, bounded command snapshots; partial deltas never become published output."""

from __future__ import annotations

import json
import re

from .memory_records import redact_content

OUTPUT_MAX_BYTES = 8192
OUTPUT_INPUT_MAX_CHARS = 65536
EVENT_MAX_JSON_BYTES = 32768
_PRIVATE_START = re.compile(r"-----BEGIN [^-\r\n]*PRIVATE KEY", re.IGNORECASE)


def command_output_fields(payload, summary):
    """Redact a complete bounded input before producing an independently flagged preview."""
    if "aggregated_output" not in payload:
        return {}
    value = payload["aggregated_output"]
    if (not isinstance(value, str) or len(value) > OUTPUT_INPUT_MAX_CHARS
            or type(payload.get("truncated", False)) is not bool):
        return {"output_text": "", "output_truncated": True}
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return {"output_text": "", "output_truncated": True}
    text = redact_content(value)
    # The shared redactor removes complete PEM blocks. An unmatched start may
    # be an upstream-truncated private key, so its remaining suffix is private.
    private = _PRIVATE_START.search(text)
    if private:
        text = text[:private.start()] + "[REDACTED PRIVATE KEY]"
    output, cut = summary(text, OUTPUT_MAX_BYTES)
    return {"output_text": output,
            "output_truncated": cut or private is not None or payload.get("truncated") is True}


def limit_command_output(payload):
    """Keep the existing whole-event JSON budget even for escaped Unicode text."""
    if "output_text" not in payload:
        return payload
    overflow = len(json.dumps(payload).encode("utf-8")) - (EVENT_MAX_JSON_BYTES - 1)
    if overflow > 0:
        data = payload["output_text"].encode("utf-8")
        payload["output_text"] = data[:max(0, len(data) - overflow)].decode("utf-8", "ignore")
        payload["output_truncated"] = True
    return payload
