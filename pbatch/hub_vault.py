"""Version-pinned evidence archive client for Aero Vault's REST API."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from .memory_records import redact_content

DEFAULT_TIMEOUT = 10.0
MAX_TOKEN_BYTES = 16 * 1024
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_VERSION_ROWS = 256
MAX_JSON_NODES = 10000
MAX_JSON_DEPTH = 32
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class VaultArchiveError(RuntimeError):
    """A safe, credential-free error from the evidence archive adapter."""

    def __init__(self, message: str, *, code: str = "vault_archive_error",
                 status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class VaultArchiveSettings:
    """Private server-side credentials and bounded transfer settings."""

    base_url: str
    tenant_id: str
    token_file: str
    prefix: str = "agent-hub/evidence"
    timeout: float = DEFAULT_TIMEOUT
    max_evidence_bytes: int = MAX_EVIDENCE_BYTES
    max_response_bytes: int = MAX_RESPONSE_BYTES


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never forward an Authorization header to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


class VaultEvidenceArchive:
    """Upload and verify bounded evidence using Aero Vault object versions."""

    def __init__(self, settings: VaultArchiveSettings):
        if not isinstance(settings, VaultArchiveSettings):
            raise TypeError("settings must be VaultArchiveSettings")
        self.base_url = _validate_base_url(settings.base_url)
        self.tenant_id = _validate_tenant(settings.tenant_id)
        self.token_file = os.path.expanduser(str(settings.token_file or ""))
        if not self.token_file:
            raise ValueError("Vault token_file is required")
        self.prefix = _validate_prefix(settings.prefix)
        self.timeout = _positive_float(settings.timeout, "timeout", 120.0)
        self.max_evidence_bytes = _bounded_limit(
            settings.max_evidence_bytes, "max_evidence_bytes", 1, 8 * 1024 * 1024)
        self.max_response_bytes = _bounded_limit(
            settings.max_response_bytes, "max_response_bytes", 1024, 16 * 1024 * 1024)
        self._opener = urllib.request.build_opener(_NoRedirect())

    def __repr__(self) -> str:
        return (f"VaultEvidenceArchive(base_url={self.base_url!r}, "
                f"tenant_id={self.tenant_id!r}, token=<redacted>)")

    def archive(self, task_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
        """Persist redacted JSON once and return a verified immutable reference."""
        task_id = _validate_segment(task_id, "task_id")
        payload = _evidence_json(task_id, evidence, self.max_evidence_bytes)
        digest = hashlib.sha256(payload).hexdigest()
        key = "/".join(part for part in (self.prefix, task_id,
                                          f"{digest}.json") if part)
        self._require_versioning()
        existing = self._find_existing(key, payload, digest)
        if existing:
            return existing
        put_error = self._put(key, task_id, payload, digest)
        if put_error is not None:
            recovered = self._recover(key, payload, digest, put_error)
            if recovered:
                return recovered
            raise put_error
        result = self._find_existing(key, payload, digest)
        if result is None:
            raise VaultArchiveError("Vault upload has no readable version",
                                    code="missing_version")
        return result

    def _require_versioning(self) -> None:
        _, headers, raw = self._request(
            "GET", "/v1/buckets/default/config", max_bytes=self.max_response_bytes)
        config = _json_response(headers, raw, "bucket config")
        if not isinstance(config, dict) or config.get("versioning") is not True:
            raise VaultArchiveError("Vault default bucket versioning is disabled",
                                    code="versioning_disabled")

    def _find_existing(self, key: str, payload: bytes,
                       digest: str) -> Optional[dict[str, Any]]:
        path = _object_path(key) + "/versions"
        _, headers, raw = self._request("GET", path,
                                        max_bytes=self.max_response_bytes)
        body = _json_response(headers, raw, "version list")
        versions = _version_rows(body)
        if not versions:
            return None
        row = versions[0]  # Vault lists versions newest first.
        version_id = _text_field(row, "version_id")
        version_path = _object_path(key) + "?" + urllib.parse.urlencode(
            {"version": version_id})
        _, download_headers, stored = self._request(
            "GET", version_path, max_bytes=self.max_evidence_bytes)
        _verify_version(download_headers, stored, row, version_id, payload, digest)
        return _reference(key, version_id, row["etag"], digest, len(payload))

    def _put(self, key: str, task_id: str, payload: bytes,
             digest: str) -> Optional[VaultArchiveError]:
        idem = hashlib.sha256(
            f"{self.tenant_id}\n{task_id}\n{digest}".encode("utf-8")).hexdigest()
        try:
            self._request("PUT", _object_path(key), body=payload,
                          expected_status=201, max_bytes=64 * 1024,
                          extra_headers={"Content-Type": "application/json",
                                         "Idempotency-Key": "hub-evidence-" + idem})
        except VaultArchiveError as exc:
            return exc
        return None

    def _recover(self, key: str, payload: bytes, digest: str,
                 original: VaultArchiveError) -> Optional[dict[str, Any]]:
        try:
            return self._find_existing(key, payload, digest)
        except VaultArchiveError as exc:
            if exc.code == "conflicting_content":
                raise
            if exc.code == "transport":
                raise original from exc
            raise

    def _request(self, method: str, path: str, *, body: Optional[bytes] = None,
                 expected_status: int = 200, max_bytes: Optional[int] = None,
                 extra_headers: Optional[dict[str, str]] = None):
        token = _read_private_token(self.token_file)
        headers = {"Authorization": "Bearer " + token,
                   "X-Aero-Tenant": self.tenant_id,
                   "Accept": "application/json"}
        headers.update(extra_headers or {})
        request = urllib.request.Request(self.base_url + path, data=body,
                                         headers=headers, method=method)
        limit = min(max_bytes or self.max_response_bytes, self.max_response_bytes)
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            exc.close()
            raise VaultArchiveError(f"Vault returned HTTP {status}",
                                    code="http_error", status=status) from None
        except (urllib.error.URLError, OSError, TimeoutError,
                http.client.HTTPException) as exc:
            raise VaultArchiveError("Vault transport failed", code="transport") from exc
        with response:
            status = int(response.getcode() or 0)
            if status != expected_status:
                raise VaultArchiveError(f"Vault returned HTTP {status}",
                                        code="http_error", status=status)
            try:
                raw = _read_response(response, limit)
            except VaultArchiveError:
                raise
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                raise VaultArchiveError("Vault transport failed",
                                        code="transport") from exc
            return status, response.headers, raw


def _read_private_token(path: str) -> str:
    """Read one 0600 regular token file without following a symlink."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise VaultArchiveError("platform cannot safely read Vault token file",
                                code="credential_unavailable")
    try:
        fd = os.open(path, os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise VaultArchiveError("Vault token file is unavailable",
                                code="credential_unavailable") from exc
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise VaultArchiveError("Vault token file must be owned 0600 regular file",
                                    code="credential_unavailable")
        raw = _read_fd(fd, MAX_TOKEN_BYTES)
    finally:
        os.close(fd)
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise VaultArchiveError("Vault token file is invalid",
                                code="credential_unavailable") from exc
    if not token or "\n" in token or "\r" in token:
        raise VaultArchiveError("Vault token file must contain one token",
                                code="credential_unavailable")
    return token


def _read_fd(fd: int, limit: int) -> bytes:
    chunks = []
    size = 0
    while size <= limit:
        chunk = os.read(fd, min(4096, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    if size > limit:
        raise VaultArchiveError("Vault token file exceeds the size limit",
                                code="credential_unavailable")
    return b"".join(chunks)


def _validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("Vault base_url must be a credential-free origin")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("Vault base_url has an invalid port") from exc
    if parsed.scheme == "http" and not _loopback_literal(parsed.hostname):
        raise ValueError("Vault HTTP is allowed only on a literal loopback address")
    return f"{parsed.scheme}://{parsed.netloc}"


def _loopback_literal(hostname: str) -> bool:
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_tenant(value: str) -> str:
    tenant = str(value or "")
    if not _TENANT.fullmatch(tenant):
        raise ValueError("Vault tenant_id is invalid")
    return tenant


def _validate_prefix(value: str) -> str:
    prefix = str(value or "").strip("/")
    if not prefix:
        return ""
    parts = prefix.split("/")
    if any(not _SEGMENT.fullmatch(part) for part in parts):
        raise ValueError("Vault prefix must contain safe path segments")
    return "/".join(parts)


def _validate_segment(value: str, name: str) -> str:
    segment = str(value or "")
    if not _SEGMENT.fullmatch(segment):
        raise ValueError(f"{name} must be a safe path segment")
    return segment


def _positive_float(value: Any, name: str, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0 or number > maximum:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def _bounded_limit(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} is outside the supported range")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is outside the supported range") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"{name} is outside the supported range")
    return number


def _evidence_json(task_id: str, evidence: dict[str, Any], limit: int) -> bytes:
    if not isinstance(evidence, dict):
        raise VaultArchiveError("evidence must be a JSON object", code="invalid_evidence")
    try:
        _validate_json_tree(evidence)
    except UnicodeEncodeError as exc:
        raise VaultArchiveError("evidence contains invalid Unicode",
                                code="invalid_evidence") from exc
    try:
        value = {"task_id": task_id, "evidence": redact_content(evidence)}
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise VaultArchiveError("evidence is not bounded JSON",
                                code="invalid_evidence") from exc
    if len(raw) > limit:
        raise VaultArchiveError("redacted evidence exceeds the size limit",
                                code="evidence_too_large")
    return raw


def _validate_json_tree(value: Any) -> None:
    stack = [(value, 0)]
    nodes = 0
    text_bytes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise VaultArchiveError("evidence structure exceeds limits",
                                    code="invalid_evidence")
        if isinstance(item, dict):
            text_bytes += _push_dict_children(item, depth, stack, nodes)
        elif isinstance(item, (list, tuple)):
            _push_sequence_children(item, depth, stack, nodes)
        elif isinstance(item, str):
            if len(item) > MAX_EVIDENCE_BYTES * 2:
                raise VaultArchiveError("evidence input exceeds the size limit",
                                        code="evidence_too_large")
            text_bytes += len(item.encode("utf-8"))
        elif item is None or isinstance(item, (bool, int)):
            continue
        elif isinstance(item, float) and math.isfinite(item):
            continue
        else:
            raise VaultArchiveError("evidence contains a non-JSON value",
                                    code="invalid_evidence")
        if text_bytes > MAX_EVIDENCE_BYTES * 2:
            raise VaultArchiveError("evidence input exceeds the size limit",
                                    code="evidence_too_large")


def _push_dict_children(item: dict, depth: int, stack: list, nodes: int) -> int:
    if nodes + len(stack) + len(item) > MAX_JSON_NODES:
        raise VaultArchiveError("evidence structure exceeds limits",
                                code="invalid_evidence")
    text_bytes = 0
    for key, child in item.items():
        if not isinstance(key, str):
            raise VaultArchiveError("evidence object keys must be strings",
                                    code="invalid_evidence")
        if len(key) > MAX_EVIDENCE_BYTES * 2:
            raise VaultArchiveError("evidence input exceeds the size limit",
                                    code="evidence_too_large")
        text_bytes += len(key.encode("utf-8"))
        stack.append((child, depth + 1))
    return text_bytes


def _push_sequence_children(item: list | tuple, depth: int,
                            stack: list, nodes: int) -> None:
    if nodes + len(stack) + len(item) > MAX_JSON_NODES:
        raise VaultArchiveError("evidence structure exceeds limits",
                                code="invalid_evidence")
    stack.extend((child, depth + 1) for child in item)


def _object_path(key: str) -> str:
    return "/v1/files/" + urllib.parse.quote(key, safe="/")


def _read_response(response, limit: int) -> bytes:
    length = response.headers.get("Content-Length", "")
    if length:
        try:
            if int(length) < 0 or int(length) > limit:
                raise VaultArchiveError("Vault response exceeds the size limit",
                                        code="response_too_large")
        except ValueError as exc:
            raise VaultArchiveError("Vault response has invalid content length",
                                    code="invalid_response") from exc
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise VaultArchiveError("Vault response exceeds the size limit",
                                code="response_too_large")
    return raw


def _json_response(headers, raw: bytes, label: str) -> Any:
    content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise VaultArchiveError(f"Vault {label} response is not JSON",
                                code="invalid_response")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise VaultArchiveError(f"Vault {label} response is invalid JSON",
                                code="invalid_response") from exc


def _version_rows(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or not isinstance(body.get("versions"), list):
        raise VaultArchiveError("Vault version list has an invalid shape",
                                code="invalid_response")
    rows = body["versions"]
    if len(rows) > MAX_VERSION_ROWS:
        raise VaultArchiveError("Vault version list exceeds the row limit",
                                code="response_too_large")
    if any(not isinstance(row, dict) for row in rows):
        raise VaultArchiveError("Vault version list has an invalid row",
                                code="invalid_response")
    return rows


def _text_field(row: dict[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value or len(value) > 256:
        raise VaultArchiveError("Vault version record is incomplete",
                                code="invalid_response")
    return value


def _verify_version(headers, stored: bytes, row: dict[str, Any],
                    version_id: str, expected: bytes, digest: str) -> None:
    if headers.get("X-Version-Id", "") != version_id:
        raise VaultArchiveError("Vault returned a different object version",
                                code="conflicting_content")
    row_etag = _normalize_etag(_text_field(row, "etag"))
    response_etag = _normalize_etag(str(headers.get("ETag", "")))
    if not row_etag or row_etag != response_etag:
        raise VaultArchiveError("Vault version ETag does not match its download",
                                code="conflicting_content")
    size = row.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size != len(expected):
        raise VaultArchiveError("Vault version size does not match evidence",
                                code="conflicting_content")
    content_length = headers.get("Content-Length", "")
    if content_length and content_length != str(len(stored)):
        raise VaultArchiveError("Vault download size is inconsistent",
                                code="conflicting_content")
    if len(stored) != size or hashlib.sha256(stored).hexdigest() != digest:
        raise VaultArchiveError("Vault stored evidence conflicts with its key",
                                code="conflicting_content")
    if stored != expected:
        raise VaultArchiveError("Vault stored evidence does not match request",
                                code="conflicting_content")


def _normalize_etag(value: str) -> str:
    return str(value or "").strip().strip('"')


def _reference(key: str, version_id: str, etag: str,
               digest: str, size: int) -> dict[str, Any]:
    return {"backend": "aero-vault", "bucket": "default", "key": key,
            "version_id": version_id, "etag": _normalize_etag(etag),
            "sha256": digest, "size": size}


__all__ = ["VaultArchiveSettings", "VaultEvidenceArchive", "VaultArchiveError"]
