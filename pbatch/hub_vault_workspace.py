"""Version-pinned Aero Vault storage for validated workspace bundles."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from typing import Any

from .hub_vault import (
    MAX_RESPONSE_BYTES,
    VaultArchiveError,
    VaultEvidenceArchive,
    _normalize_etag,
    _object_path,
    _reference,
    _text_field,
    _version_rows,
    _verify_version,
)

MAX_WORKSPACE_JSON_BYTES = 512 * 1024
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_REF_FIELDS = {"backend", "bucket", "key", "version_id", "etag", "sha256", "size"}


class VaultWorkspaceArchive:
    """Store and retrieve bounded workspace bundles without redacting bytes."""

    def __init__(self, archive: VaultEvidenceArchive):
        if not isinstance(archive, VaultEvidenceArchive):
            raise TypeError("archive must be VaultEvidenceArchive")
        self.archive = archive

    def put(self, object_id: str, bundle: dict[str, Any]) -> dict[str, Any]:
        """Upload a canonical validated bundle and return its exact Vault version."""
        object_id = _safe_segment(object_id)
        payload, digest = _serialize_bundle(bundle)
        key = self._key(object_id, digest)
        self.archive._require_versioning()
        existing = self._find_existing(key, payload, digest)
        if existing is not None:
            return existing
        error = self._put(key, object_id, payload, digest)
        if error is not None:
            try:
                recovered = self._find_existing(key, payload, digest)
            except VaultArchiveError as exc:
                if exc.code == "conflicting_content":
                    raise
                if exc.code == "transport":
                    raise error from exc
                raise
            if recovered is not None:
                return recovered
            raise error
        result = self._find_existing(key, payload, digest)
        if result is None:
            raise VaultArchiveError("Vault upload has no readable version",
                                    code="missing_version")
        return result

    def read(self, reference: dict[str, Any]) -> dict[str, Any]:
        """Read one exact workspace version and revalidate its bytes and schema."""
        ref = self._validate_reference(reference)
        self.archive._require_versioning()
        row = self._find_version_row(ref)
        path = _object_path(ref["key"]) + "?" + urllib.parse.urlencode(
            {"version": ref["version_id"]})
        _, headers, payload = self.archive._request(
            "GET", path, max_bytes=MAX_WORKSPACE_JSON_BYTES)
        digest = ref["sha256"]
        _verify_version(headers, payload, row, ref["version_id"], payload, digest)
        if len(payload) != ref["size"] or _normalize_etag(ref["etag"]) != _normalize_etag(
                headers.get("ETag", "")):
            raise VaultArchiveError("Vault workspace reference does not match object",
                                    code="conflicting_content")
        bundle = _parse_bundle(payload)
        canonical, actual_digest = _serialize_bundle(bundle)
        if canonical != payload or actual_digest != digest:
            raise VaultArchiveError("Vault workspace bundle is not canonical",
                                    code="conflicting_content")
        return bundle

    def _workspace_prefix(self) -> str:
        return "/".join(part for part in (self.archive.prefix, "workspaces") if part)

    def _key(self, object_id: str, digest: str) -> str:
        key = f"{self._workspace_prefix()}/{object_id}/{digest}.json"
        if len(key.encode("utf-8")) > 200:
            raise VaultArchiveError("Vault workspace key exceeds the object-key limit",
                                    code="invalid_bundle")
        return key

    def _validate_reference(self, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != _REF_FIELDS:
            raise VaultArchiveError("Vault workspace reference has an invalid shape",
                                    code="invalid_reference")
        if value.get("backend") != "aero-vault" or value.get("bucket") != "default":
            raise VaultArchiveError("Vault workspace reference has an invalid backend",
                                    code="invalid_reference")
        key = value.get("key")
        prefix = self._workspace_prefix() + "/"
        if not isinstance(key, str) or not key.startswith(prefix):
            raise VaultArchiveError("Vault workspace key is outside the configured prefix",
                                    code="invalid_reference")
        suffix = key[len(prefix):].split("/")
        if (len(suffix) != 2 or not _safe_segment_ok(suffix[0])
                or not suffix[1].endswith(".json")
                or not _DIGEST.fullmatch(suffix[1][:-5])):
            raise VaultArchiveError("Vault workspace key is invalid",
                                    code="invalid_reference")
        if suffix[1][:-5] != value.get("sha256"):
            raise VaultArchiveError("Vault workspace key digest does not match reference",
                                    code="invalid_reference")
        version = value.get("version_id")
        etag, digest, size = value.get("etag"), value.get("sha256"), value.get("size")
        if (not isinstance(version, str) or not version or len(version) > 256
                or not isinstance(etag, str) or not etag or len(etag) > 256
                or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                or isinstance(size, bool) or not isinstance(size, int)
                or size < 0 or size > MAX_WORKSPACE_JSON_BYTES):
            raise VaultArchiveError("Vault workspace reference fields are invalid",
                                    code="invalid_reference")
        return dict(value)

    def _find_version_row(self, ref: dict[str, Any]) -> dict[str, Any]:
        path = _object_path(ref["key"]) + "/versions"
        _, headers, raw = self.archive._request(
            "GET", path, max_bytes=min(MAX_RESPONSE_BYTES, self.archive.max_response_bytes))
        from .hub_vault import _json_response
        rows = _version_rows(_json_response(headers, raw, "workspace version list"))
        matches = [row for row in rows
                   if isinstance(row.get("version_id"), str)
                   and row["version_id"] == ref["version_id"]]
        if len(matches) != 1:
            raise VaultArchiveError("Vault workspace version is missing or ambiguous",
                                    code="invalid_reference")
        row = matches[0]
        if (_normalize_etag(_text_field(row, "etag")) != _normalize_etag(ref["etag"])
                or row.get("size") != ref["size"]):
            raise VaultArchiveError("Vault workspace version metadata changed",
                                    code="conflicting_content")
        if row.get("deleted_at"):
            raise VaultArchiveError("Vault workspace version was deleted",
                                    code="invalid_reference")
        return row

    def _find_existing(self, key: str, payload: bytes,
                       digest: str) -> dict[str, Any] | None:
        path = _object_path(key) + "/versions"
        _, headers, raw = self.archive._request(
            "GET", path, max_bytes=min(MAX_RESPONSE_BYTES, self.archive.max_response_bytes))
        from .hub_vault import _json_response
        rows = _version_rows(_json_response(headers, raw, "workspace version list"))
        if not rows:
            return None
        row = rows[0]
        version_id = _text_field(row, "version_id")
        version_path = _object_path(key) + "?" + urllib.parse.urlencode(
            {"version": version_id})
        _, download_headers, stored = self.archive._request(
            "GET", version_path, max_bytes=MAX_WORKSPACE_JSON_BYTES)
        _verify_version(download_headers, stored, row, version_id, payload, digest)
        return _reference(key, version_id, row["etag"], digest, len(payload))

    def _put(self, key: str, object_id: str, payload: bytes,
             digest: str) -> VaultArchiveError | None:
        idem = hashlib.sha256(("hub-workspace\n" + self.archive.tenant_id + "\n"
                               + object_id + "\n" + digest).encode("utf-8")).hexdigest()
        try:
            self.archive._request(
                "PUT", _object_path(key), body=payload, expected_status=201,
                max_bytes=64 * 1024,
                extra_headers={"Content-Type": "application/json",
                               "Idempotency-Key": "hub-workspace-" + idem})
        except VaultArchiveError as exc:
            return exc
        return None


def _serialize_bundle(bundle: dict[str, Any]) -> tuple[bytes, str]:
    validate, encode, digest_bundle = _codec()
    try:
        validate(bundle)
        payload = encode(bundle)
        digest = digest_bundle(bundle)
    except (TypeError, ValueError, RecursionError) as exc:
        raise VaultArchiveError("workspace bundle is invalid", code="invalid_bundle") from exc
    if not isinstance(payload, bytes) or len(payload) > MAX_WORKSPACE_JSON_BYTES:
        raise VaultArchiveError("workspace bundle exceeds the size limit",
                                code="bundle_too_large")
    actual = hashlib.sha256(payload).hexdigest()
    if not isinstance(digest, str) or digest != actual:
        raise VaultArchiveError("workspace bundle codec digest is inconsistent",
                                code="invalid_bundle")
    return payload, digest


def _parse_bundle(payload: bytes) -> dict[str, Any]:
    try:
        bundle = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise VaultArchiveError("Vault workspace object is not valid JSON",
                                code="invalid_bundle") from exc
    if not isinstance(bundle, dict):
        raise VaultArchiveError("Vault workspace object is not a JSON object",
                                code="invalid_bundle")
    return bundle


def _codec():
    try:
        from .workspace_bundle import bundle_bytes, bundle_digest, validate_bundle
    except ImportError as exc:
        raise VaultArchiveError("workspace bundle codec is unavailable",
                                code="codec_unavailable") from exc
    return validate_bundle, bundle_bytes, bundle_digest


def _safe_segment(value: str) -> str:
    if not _safe_segment_ok(value):
        raise VaultArchiveError("workspace object_id is invalid", code="invalid_bundle")
    return value


def _safe_segment_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value))


__all__ = ["VaultWorkspaceArchive", "MAX_WORKSPACE_JSON_BYTES"]
