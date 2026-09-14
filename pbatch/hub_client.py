"""Small bounded HTTP client for the remote Agent Session Hub."""

from __future__ import annotations

import ipaddress
import json
import os
import stat
import uuid
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

API_PATH = "/api/v1/agent"
DEFAULT_TIMEOUT = 5.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_PROMPT_BYTES = 16 * 1024
MAX_EVENT_BYTES = 32 * 1024
MAX_TOKEN_BYTES = 16 * 1024


class HubError(RuntimeError):
    """An HTTP, protocol, or response validation error from the hub."""

    def __init__(self, message: str, *, code: str = "hub_error",
                 status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects can move a bearer token to an untrusted origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def validate_hub_url(value: str) -> str:
    """Allow TLS, or cleartext HTTP only to a literal loopback address."""
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("hub URL must use https or loopback http")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("hub URL must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("hub URL must be an origin without a path")
    if parsed.scheme == "http" and not _is_loopback_literal(parsed.hostname):
        raise ValueError("plain HTTP hub URL must use a literal loopback address")
    return f"{parsed.scheme}://{parsed.netloc}"


def _is_loopback_literal(hostname: str) -> bool:
    """Reject DNS names, including localhost aliases, on cleartext transport."""
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def load_hub_token(*, environ: Optional[dict[str, str]] = None) -> str:
    """Read a bearer token from the environment or a private token file."""
    env = os.environ if environ is None else environ
    token = str(env.get("PBATCH_HUB_TOKEN", "")).strip()
    if token:
        if len(token.encode("utf-8")) > MAX_TOKEN_BYTES or "\n" in token:
            raise ValueError("PBATCH_HUB_TOKEN is invalid or too large")
        return token
    path = str(env.get("PBATCH_HUB_TOKEN_FILE", "")).strip()
    if not path:
        raise ValueError("set PBATCH_HUB_TOKEN or PBATCH_HUB_TOKEN_FILE")
    return _read_private_token(path)


def _read_private_token(path: str) -> str:
    """Read one regular, private token file without following a symlink."""
    path = os.path.expanduser(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot open hub token file: {exc.strerror}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("hub token file must be regular and mode 0600")
        raw = os.read(descriptor, MAX_TOKEN_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_TOKEN_BYTES:
        raise ValueError("hub token file exceeds the size limit")
    token = raw.decode("utf-8").strip()
    if not token or "\n" in token:
        raise ValueError("hub token file must contain one non-empty token")
    return token


class HubClient:
    """Authenticated JSON client with bounded responses and no redirects."""

    def __init__(self, base_url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT,
                 max_response_bytes: int = MAX_RESPONSE_BYTES):
        self.base_url = validate_hub_url(base_url)
        self._token = _checked_token(token)
        self._token_file = ""
        if timeout <= 0 or timeout > 120:
            raise ValueError("hub timeout must be between 0 and 120 seconds")
        if not 1024 <= max_response_bytes <= 64 * 1024 * 1024:
            raise ValueError("hub response limit is outside the supported range")
        self.timeout = float(timeout)
        self.max_response_bytes = int(max_response_bytes)
        self._opener = urllib.request.build_opener(_NoRedirect())

    def __repr__(self) -> str:
        return f"HubClient(base_url={self.base_url!r}, token=<redacted>)"

    @classmethod
    def from_env(cls, hub_url: str = "", *, environ=None,
                 timeout: float = DEFAULT_TIMEOUT) -> "HubClient":
        env = os.environ if environ is None else environ
        endpoint = hub_url or str(env.get("PBATCH_HUB_URL", "")).strip()
        if not endpoint:
            raise ValueError("set --hub or PBATCH_HUB_URL")
        client = cls(endpoint, load_hub_token(environ=env), timeout=timeout)
        if not str(env.get("PBATCH_HUB_TOKEN", "")).strip():
            client._token_file = os.path.expanduser(env.get("PBATCH_HUB_TOKEN_FILE", ""))
        return client

    def _authorization_token(self) -> str:
        if not self._token_file:
            return self._token
        try:
            return _checked_token(_read_private_token(self._token_file))
        except (ValueError, OSError) as exc:
            raise HubError("private Hub credential file is unavailable",
                           code="credential_unavailable", status=401) from exc

    def list_instances(self, *, after: str = "", limit: int = 100,
                       max_pages: int = 20) -> list[dict[str, Any]]:
        return self._list_pages("/instances", "instances", {}, after,
                                limit, max_pages)

    def list_sessions(self, instance_id: str = "", *, after: str = "",
                      limit: int = 100, max_pages: int = 20) -> list[dict[str, Any]]:
        filters = {"instance_id": instance_id} if instance_id else {}
        return self._list_pages("/sessions", "sessions", filters, after,
                                limit, max_pages)

    def _list_pages(self, path: str, label: str, filters: dict[str, str],
                    after: str, limit: int, max_pages: int) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 200 or not 1 <= int(max_pages) <= 100:
            raise ValueError("list page size/count is outside the supported range")
        items: list[dict[str, Any]] = []
        cursor = str(after or "")
        for page in range(int(max_pages)):
            query = dict(filters, limit=int(limit))
            if cursor:
                query["after"] = cursor
            data, next_cursor = self._request(
                "GET", f"{path}?{urllib.parse.urlencode(query)}",
                include_cursor=True)
            items.extend(_as_list(data, label))
            if not next_cursor or next_cursor == cursor:
                return items
            cursor = next_cursor
            if page + 1 == int(max_pages):
                raise HubError(f"Hub {label} list exceeded {max_pages} pages",
                               code="pagination_limit")
        return items

    def get_session(self, session_id: str) -> dict[str, Any]:
        return _as_object(self._request("GET", f"/sessions/{_quote(session_id)}"),
                          "session")

    def send_prompt(self, session_id: str, prompt: str,
                    idempotency_key: str = "") -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ValueError("prompt exceeds the 16 KiB Hub limit")
        key = idempotency_key or uuid.uuid4().hex
        headers = {"Idempotency-Key": key}
        result = self._request("POST", f"/sessions/{_quote(session_id)}/turns",
                               {"prompt": prompt}, headers=headers, accepted=(202,))
        return _as_object(result, "turn")

    def get_turn(self, turn_id: str) -> dict[str, Any]:
        return _as_object(self._request("GET", f"/turns/{_quote(turn_id)}"), "turn")

    def cancel_turn(self, turn_id: str) -> dict[str, Any]:
        result = self._request("POST", f"/turns/{_quote(turn_id)}/cancel", {},
                               accepted=(202,))
        return _as_object(result, "turn")

    def session_events(self, session_id: str, *, after: str = "",
                       limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 100:
            raise ValueError("event limit must be between 1 and 100")
        query = {"limit": int(limit)}
        if after:
            query["after"] = after
        suffix = f"/sessions/{_quote(session_id)}/events?{urllib.parse.urlencode(query)}"
        return _as_list(self._request("GET", suffix), "events")

    def register_instance(self, *, instance_id: str, boot_id: str,
                          name: str, project_id: str) -> dict[str, Any]:
        return _as_object(self._request("POST", "/gateway/register", {
            "instance_id": instance_id, "boot_id": boot_id,
            "name": name, "project_id": project_id,
        }, accepted=(200, 201)), "instance")

    def heartbeat(self, *, instance_id: str, generation: int) -> dict[str, Any]:
        return _as_object(self._request("POST", "/gateway/heartbeat", {
            "instance_id": instance_id, "generation": generation,
        }, accepted=(200, 201)), "heartbeat")

    def register_session(self, *, instance_id: str, generation: int,
                         local_session_id: str, name: str, project_id: str,
                         status: str = "idle", controllable: bool = True) -> dict[str, Any]:
        return _as_object(self._request("POST", "/gateway/sessions", {
            "instance_id": instance_id, "generation": generation,
            "local_session_id": local_session_id, "name": name,
            "project_id": project_id, "status": status,
            "controllable": controllable,
        }, accepted=(200, 201)), "session")

    def get_commands(self, *, instance_id: str,
                     generation: int) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"instance_id": instance_id,
                                        "generation": generation})
        return _as_list(self._request("GET", f"/gateway/commands?{query}"),
                        "commands")

    def send_event(self, event: dict[str, Any], *, instance_id: str,
                   generation: int) -> dict[str, Any]:
        payload = dict(event)
        payload.update(instance_id=instance_id, generation=generation)
        if len(_encode_json(payload)) > MAX_EVENT_BYTES:
            raise ValueError("hub event exceeds the 32 KiB Hub limit")
        return _as_object(self._request("POST", "/gateway/events", payload,
                                        accepted=(200, 201)), "event")

    def _request(self, method: str, path: str, payload: Any = None, *,
                 headers: Optional[dict[str, str]] = None,
                 accepted: tuple[int, ...] = (200,),
                 include_cursor: bool = False) -> Any:
        return _request_json(self, method, path, payload, headers or {}, accepted,
                             include_cursor)


def _request_json(client: HubClient, method: str, path: str, payload: Any,
                  extra_headers: dict[str, str], accepted: tuple[int, ...],
                  include_cursor: bool) -> Any:
    body = None if payload is None else _encode_json(payload)
    from .hub_workspace import request_limit
    if body is not None and len(body) > request_limit(path):
        raise ValueError("Hub request exceeds the route request limit")
    request = urllib.request.Request(client.base_url + API_PATH + path,
                                     data=body, method=method)
    request.add_header("Authorization", "Bearer " + client._authorization_token())
    request.add_header("Accept", "application/json")
    if body is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    for name, value in extra_headers.items():
        request.add_header(name, value)
    try:
        response = client._opener.open(request, timeout=client.timeout)
    except urllib.error.HTTPError as exc:
        raw = _read_limited(exc, client.max_response_bytes)
        _raise_http_error(exc.code, raw)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise HubError(f"hub request failed: {type(exc).__name__}",
                       code="hub_unavailable") from exc
    with response:
        status = int(response.status)
        raw = _read_limited(response, client.max_response_bytes)
    if status not in accepted:
        _raise_http_error(status, raw)
    data, cursor = _decode_response(raw)
    return (data, cursor) if include_cursor else data


def _read_limited(response, maximum: int) -> bytes:
    length = response.headers.get("Content-Length", "")
    if length.isdigit() and int(length) > maximum:
        raise HubError("hub response exceeds the size limit", code="response_too_large")
    raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise HubError("hub response exceeds the size limit", code="response_too_large")
    return raw


def _decode_envelope(raw: bytes) -> Any:
    return _decode_response(raw)[0]


def _decode_response(raw: bytes) -> tuple[Any, str]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HubError("hub returned invalid JSON", code="invalid_response") from exc
    if not isinstance(value, dict) or "data" not in value:
        raise HubError("hub response is missing the data envelope",
                       code="invalid_response")
    cursor = value.get("next_cursor", "")
    if not isinstance(cursor, (str, int)):
        raise HubError("hub pagination cursor has the wrong type",
                       code="invalid_response")
    return value["data"], str(cursor)


def _raise_http_error(status: int, raw: bytes) -> None:
    try:
        body = json.loads(raw.decode("utf-8"))
        error = body.get("error", {}) if isinstance(body, dict) else {}
        code = str(error.get("code", "http_error"))
        message = str(error.get("message", f"hub returned HTTP {status}"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        code, message = "http_error", f"hub returned HTTP {status}"
    raise HubError(message[:2048], code=code[:128], status=status)


def _checked_token(token: str) -> str:
    value = str(token or "").strip()
    if not value or len(value.encode("utf-8")) > MAX_TOKEN_BYTES:
        raise ValueError("hub token is required and must be at most 16384 bytes")
    if "\r" in value or "\n" in value:
        raise ValueError("hub token must be a single line")
    return value


def _encode_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _quote(value: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 256 or "\x00" in text:
        raise ValueError("hub identifier is invalid")
    return urllib.parse.quote(text, safe="")


def _as_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise HubError(f"hub {label} response must be a list of objects",
                       code="invalid_response")
    return value


def _as_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HubError(f"hub {label} response must be an object",
                       code="invalid_response")
    return value


__all__ = ["HubClient", "HubError", "load_hub_token", "validate_hub_url"]
