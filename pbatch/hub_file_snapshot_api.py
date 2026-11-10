"""Finite file inspection routes under current session and artifact authority."""

from urllib.parse import unquote_to_bytes, urlsplit

from .hub_auth import GATEWAY_SCOPE
from .hub_errors import HubAccessError
from .hub_file_snapshot_models import READ_SCOPE
from .hub_file_snapshot_store import FileSnapshotStore


def is_route(parts):
    return len(parts) >= 5 and parts[0] == "sessions" and parts[2] == "turns" and parts[4] == "file-snapshots"


def is_path(path):
    from .hub_service import PREFIX
    decoded = unquote_to_bytes(urlsplit(path).path).decode("utf-8")
    if not decoded.startswith(PREFIX + "/"):
        return False
    return is_route(decoded[len(PREFIX) + 1:].split("/"))


def validate_read_headers(path, headers):
    if not is_path(path):
        return
    lengths = headers.get_all("Content-Length", [])
    forbidden = ("Transfer-Encoding", "Idempotency-Key", "Range", "If-Range", "If-Match",
                 "If-None-Match", "If-Modified-Since", "If-Unmodified-Since")
    if ("?" in path or "#" in path or len(lengths) > 1 or any(value != "0" for value in lengths)
            or any(headers.get_all(name, []) for name in forbidden)):
        raise ValueError("snapshot GET does not accept query, body, range or conditional headers")


def dispatch(service, method, parts, query, body, principal):
    if method != "GET":
        raise HubAccessError("method_not_allowed", "file snapshots are read-only", 405)
    if query or body or len(parts) not in {5, 6}:
        raise ValueError("invalid file snapshot read request")
    principal.require(READ_SCOPE)
    session = service._session(principal, parts[1], "agent.sessions:read")
    principal.require(READ_SCOPE, session["instance_id"])
    snapshot_id = parts[5] if len(parts) == 6 else None
    return 200, {"data": FileSnapshotStore(service.store).read(
        principal.tenant_id, parts[1], parts[3], snapshot_id)}


def gateway(service, method, query, body, principal):
    if method != "POST":
        raise HubAccessError("method_not_allowed", "file snapshot upload requires POST", 405)
    if query:
        raise ValueError("file snapshot upload does not accept query parameters")
    from .hub_file_snapshot_models import validate_upload
    body = validate_upload(body)
    principal.require(GATEWAY_SCOPE, body["instance_id"])
    return 200, {"data": FileSnapshotStore(service.store).put(principal.tenant_id, body)}
