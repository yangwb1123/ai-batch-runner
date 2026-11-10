"""Whole-window upload deadlines and strict snapshot transport framing."""

from time import monotonic
from urllib.parse import unquote_to_bytes, urlsplit

from .hub_errors import HubAccessError

UPLOAD_SECONDS = 5.0


def is_upload_path(path):
    from .hub_service import PREFIX
    return unquote_to_bytes(urlsplit(path).path).decode("utf-8") == PREFIX + "/gateway/file-snapshots"


def validate_headers(path, headers, method):
    from .hub_file_snapshot_api import validate_read_headers
    if method == "GET":
        validate_read_headers(path, headers)
    if is_upload_path(path):
        if "?" in path or "#" in path:
            raise ValueError("snapshot upload does not accept query or fragment")
        if any(headers.get_all(name, []) for name in ("Range", "If-Range", "Idempotency-Key")):
            raise ValueError("snapshot upload does not accept range or idempotency headers")


def read_upload_bytes(handler, size):
    deadline, chunks, remaining = monotonic() + UPLOAD_SECONDS, [], size
    previous = handler.connection.gettimeout()
    try:
        while remaining:
            seconds = deadline - monotonic()
            if seconds <= 0:
                raise TimeoutError("snapshot upload deadline")
            handler.connection.settimeout(seconds)
            chunk = handler.rfile.read1(min(remaining, 16384))
            if monotonic() >= deadline:
                raise TimeoutError("snapshot upload deadline")
            if not chunk:
                raise ValueError("truncated snapshot upload")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except TimeoutError:
        handler.close_connection = True
        raise HubAccessError("request_timeout", "snapshot upload read deadline exceeded", 408) from None
    finally:
        handler.connection.settimeout(previous)
