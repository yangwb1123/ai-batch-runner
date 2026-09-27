"""Narrow MCP 2025-11-25 stdio facade over the store's redacted reports.

Only initialize, ping, tools/list and tools/call are requests; tools require the
initialized notification. No batches, pagination, tasks or server requests.
Frames include the newline in the 1 MiB limit in either direction; tool report
JSON is capped at 256 KiB. Oversized/unterminated frames terminate the connection
without draining stdin or writing partial output. Store reports must contain
public metadata only; this module never opens credentials or serializes config.

Wire contract references:
https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
https://modelcontextprotocol.io/specification/2025-11-25/server/tools
"""

from __future__ import annotations

import json
import math
import sys


PROTOCOL_VERSION = "2025-11-25"
MAX_LINE_BYTES = 1024 * 1024
MAX_REPORT_BYTES = 256 * 1024
DEFAULT_EVENT_LIMIT = 20


class _RPCError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message


def _error(code, message, request_id=None):
    response = {"jsonrpc": "2.0", "error": {"code": code, "message": message}}
    # Official 2025-11-25 JSONRPCErrorResponse: id?: RequestId (string | number).
    # Omit unreadable/invalid IDs; a successfully read valid ID must be echoed.
    if request_id is not None:
        response["id"] = request_id
    return response


def _fields(value, allowed, required=()):
    if not isinstance(value, dict):
        raise _RPCError(-32602, "Invalid params")
    if set(value) - set(allowed) or set(required) - set(value):
        raise _RPCError(-32602, "Invalid params")
    return value


def _params(message, allowed=(), required=()):
    params = _fields(message.get("params", {}), {*allowed, "_meta"}, required)
    if "_meta" in params and not isinstance(params["_meta"], dict):
        raise _RPCError(-32602, "Invalid params")
    return params


def _valid_id(value):
    return type(value) in (str, int, float)


def _envelope(message):
    if not isinstance(message, dict):
        raise _RPCError(-32600, "Invalid Request")
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        raise _RPCError(-32600, "Invalid Request")
    if set(message) - {"jsonrpc", "method", "params", "id"}:
        raise _RPCError(-32600, "Invalid Request")
    if "id" in message and not _valid_id(message["id"]):
        raise _RPCError(-32600, "Invalid Request")


def _initialize(message, phase):
    if phase != "new":
        raise _RPCError(-32600, "Already initialized")
    fields = {"protocolVersion", "capabilities", "clientInfo"}
    params = _params(message, fields, fields)
    if not isinstance(params["protocolVersion"], str) or not params["protocolVersion"]:
        raise _RPCError(-32602, "Invalid params")
    if not isinstance(params["capabilities"], dict):
        raise _RPCError(-32602, "Invalid params")
    client = params["clientInfo"]
    if not isinstance(client, dict):
        raise _RPCError(-32602, "Invalid params")
    if any(not isinstance(client.get(key), str) for key in ("name", "version")):
        raise _RPCError(-32602, "Invalid params")
    # Unknown versions negotiate our supported version; optional client features
    # are ignored since the server never makes requests to the client.
    return {"protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "pbatch-llm-pool", "version": "1.0.0"}}


def _tool(name, description, properties):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": False}}


def _tools(settings):
    # Only configured identifiers are exposed, never secret values or paths.
    models = sorted(settings.models)
    model_schema = {"type": "string", "enum": models} if models else {"not": {}}
    accounts = sorted(account.id for account in settings.accounts)
    account_schema = {"type": "string", "enum": accounts} if accounts else {"not": {}}
    return {"tools": [
        _tool("pool_status", "Read pool availability; optionally filter by configured model.",
              {"model": model_schema}),
        _tool("pool_events", "Read recent pool metadata events (newest first).",
              {"limit": {"type": "integer", "minimum": 1, "maximum": 100,
                         "default": DEFAULT_EVENT_LIMIT}}),
        _tool("pool_usage", "Read cached quota usage and freshness "
              "(fresh/stale/unknown/error/refreshing); advisory only. "
              "No live refresh, network calls or secret/key access.",
              {"account": account_schema}),
    ]}


def _tool_args(params, settings):
    name = params.get("name")
    if name not in ("pool_status", "pool_events", "pool_usage"):
        raise _RPCError(-32602, "Unknown tool")
    if name in ("pool_status", "pool_usage"):
        field = "model" if name == "pool_status" else "account"
        args = _fields(params.get("arguments", {}), {field})
        if field not in args:
            return name, ""
        configured = settings.models if field == "model" else {a.id for a in settings.accounts}
        value = args[field]
        if not isinstance(value, str) or value not in configured:
            raise _RPCError(-32602, f"Invalid params: {field} must be configured")
        return name, value
    args = _fields(params.get("arguments", {}), {"limit"})
    limit = args.get("limit", DEFAULT_EVENT_LIMIT)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise _RPCError(-32602, "Invalid params: limit must be an integer in 1..100")
    return name, limit


def _json(value, max_bytes=MAX_LINE_BYTES - 1):
    encoder = json.JSONEncoder(ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    chunks, size = [], 0
    for chunk in encoder.iterencode(value):
        size += len(chunk)  # ensure_ascii makes character and UTF-8 byte counts equal.
        if size > max_bytes:
            raise ValueError
        chunks.append(chunk)
    return "".join(chunks)


def _usage(settings, store, account_id):
    """Project the redacted cached snapshot; never refresh or infer quota use."""
    configured = {account.id for account in settings.accounts}
    rows = store.status(settings.accounts, model="")["accounts"]
    if not isinstance(rows, list):
        raise ValueError
    unknown = {"state": "unknown", "checked_at": None, "fresh_until": None,
               "next_refresh_at": None, "error": None, "windows": {}}
    accounts = [{"id": row["id"], "quota_group": row["quota_group"],
                 "usage": row.get("usage", unknown)} for row in rows
                if row["id"] in configured and (not account_id or row["id"] == account_id)]
    if any(not isinstance(account["usage"], dict) for account in accounts):
        raise ValueError
    return {"source": "cache", "advisory": True, "accounts": accounts}


def _call(message, settings, store):
    params = _params(message, {"name", "arguments"}, {"name"})
    name, argument = _tool_args(params, settings)
    try:
        if name == "pool_status":
            value = store.status(settings.accounts, model=argument,
                                 require_usage=getattr(settings, "usage_enabled", False))
            if not isinstance(value, dict):
                raise ValueError
        elif name == "pool_usage":
            value = _usage(settings, store, argument)
        else:
            value = store.events(argument)
            if not isinstance(value, list) or len(value) > argument:
                raise ValueError
        text = _json(value, MAX_REPORT_BYTES)
    except Exception:
        # Do not format exceptions: they can contain credentials or file paths.
        return {"content": [{"type": "text", "text": _json({"error": "Pool read failed"})}],
                "isError": True}
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _request(message, phase, settings, store):
    method = message["method"]
    if method == "initialize":
        return _initialize(message, phase), "initializing"
    if method == "ping":
        _params(message)
        return {}, phase
    if method not in ("tools/list", "tools/call"):
        raise _RPCError(-32601, "Method not found")
    if phase != "ready":
        raise _RPCError(-32002, "Server not initialized")
    if method == "tools/list":
        _params(message)  # The complete, static list fits in a single response.
        return _tools(settings), phase
    return _call(message, settings, store), phase


def _dispatch(message, phase, settings, store):
    notification = isinstance(message, dict) and "id" not in message
    request_id = message.get("id") if isinstance(message, dict) else None
    if not _valid_id(request_id):
        request_id = None
    try:
        _envelope(message)
        if notification:
            if message["method"] == "notifications/initialized" and phase == "initializing":
                _params(message)
                phase = "ready"
            return None, phase
        result, phase = _request(message, phase, settings, store)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}, phase
    except _RPCError as exc:
        response = _error(exc.code, exc.message, request_id)
    except Exception:
        response = _error(-32603, "Internal error", request_id)
    if notification and isinstance(message.get("method"), str):
        return None, phase
    return response, phase


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def _handle_line(line, phase, settings, store):
    try:
        message = json.loads(line.decode("utf-8"), object_pairs_hook=_object,
                             parse_constant=_reject_constant, parse_float=_float)
    except (ValueError, RecursionError):
        return _error(-32700, "Parse error"), phase
    return _dispatch(message, phase, settings, store)


def _write(output, response):
    # Build and bound the complete frame before any write, including escaped IDs.
    # If an ID cannot fit, close the connection rather than omit a known ID.
    output.write((_json(response) + "\n").encode("utf-8"))
    output.flush()


def serve_mcp(settings, store, input_stream=None, output_stream=None) -> int:
    """Serve binary streams; return 0 on EOF, 1 on framing/transport failure.

    settings supplies models and accounts; store supplies redacted status/events.
    Recoverable JSON-RPC errors do not change the exit status. Streams are owned
    by the caller and are never closed here. No diagnostics are written to stdout.
    Oversized tool reports return sanitized isError; oversized wire frames close.
    """
    source = sys.stdin.buffer if input_stream is None else input_stream
    output = sys.stdout.buffer if output_stream is None else output_stream
    phase = "new"
    try:
        while True:
            line = source.readline(MAX_LINE_BYTES + 1)
            if not line:
                return 0
            if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                _write(output, _error(-32700, "Invalid frame"))
                return 1
            response, phase = _handle_line(line, phase, settings, store)
            if response is not None:
                _write(output, response)
    except (OSError, ValueError, TypeError):
        return 1
