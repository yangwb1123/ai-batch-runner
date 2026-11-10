"""Transactional immutable inspection snapshots, separate from event storage."""

from __future__ import annotations

import base64
import hashlib
import json
import math

from .hub_file_snapshot_models import (
    MAX_FILE_BYTES, MAX_PATHS, SCHEMA, VERIFICATION, canonical_bytes,
    file_metadata, opaque_id, validate_upload,
)
from .hub_store_helpers import (
    HubStoreError, _TERMINAL, check_session_owner, get_session,
    get_turn_for_session, require_live_generation,
)

MAX_INSTANCE_BYTES = 16 * 1024 * 1024
MAX_GLOBAL_BYTES = 64 * 1024 * 1024
MAX_INSTANCE_FILES = 1024
MAX_GLOBAL_FILES = 4096
MAX_GLOBAL_SETS = 4096
_MANIFEST_LIMIT = 32 * 1024
STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS hub_file_snapshot_sets(
  tenant TEXT NOT NULL, instance_id TEXT NOT NULL, session_id TEXT NOT NULL,
  turn_id TEXT NOT NULL, snapshot_set_id TEXT NOT NULL,
  payload_digest TEXT NOT NULL, manifest_json TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY(tenant,turn_id), UNIQUE(tenant,snapshot_set_id));
CREATE INDEX IF NOT EXISTS hub_file_snapshot_instance
  ON hub_file_snapshot_sets(tenant,instance_id);
CREATE TABLE IF NOT EXISTS hub_file_snapshot_bytes(
  tenant TEXT NOT NULL, turn_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
  content BLOB NOT NULL, PRIMARY KEY(tenant,turn_id,snapshot_id));
"""


def _binding(conn, tenant, body, now, offline_after):
    instance, generation = body["instance_id"], body["generation"]
    require_live_generation(conn, tenant, instance, generation, now, offline_after)
    session = get_session(conn, tenant, body["session_id"])
    check_session_owner(session, instance, generation)
    turn = get_turn_for_session(conn, tenant, body["turn_id"], body["session_id"])
    if turn["instance_id"] != instance or turn["generation"] != generation:
        raise HubStoreError("stale_generation", "snapshot turn owner is stale")
    cancelled = conn.execute("SELECT 1 FROM hub_commands WHERE tenant=? AND turn_id=? "
                             "AND kind='cancel' LIMIT 1", (tenant, body["turn_id"])).fetchone()
    if turn["status"] not in _TERMINAL or (body["files"] and (turn["status"] != "completed" or cancelled)):
        raise HubStoreError("snapshot_turn_state", "snapshot requires an eligible terminal turn")


def _capacity(conn, tenant, body):
    total = conn.execute("SELECT count(*),coalesce(sum(length(content)),0) "
                         "FROM hub_file_snapshot_bytes").fetchone()
    instance = conn.execute("SELECT count(*),coalesce(sum(length(b.content)),0) "
                            "FROM hub_file_snapshot_bytes b JOIN hub_file_snapshot_sets s "
                            "ON s.tenant=b.tenant AND s.turn_id=b.turn_id "
                            "WHERE s.tenant=? AND s.instance_id=?", (tenant, body["instance_id"])).fetchone()
    sets = conn.execute("SELECT count(*) FROM hub_file_snapshot_sets").fetchone()[0]
    files, size = len(body["files"]), sum(row["size_bytes"] for row in body["files"])
    if (total[0] + files > MAX_GLOBAL_FILES or total[1] + size > MAX_GLOBAL_BYTES
            or instance[0] + files > MAX_INSTANCE_FILES or instance[1] + size > MAX_INSTANCE_BYTES
            or sets + 1 > MAX_GLOBAL_SETS):
        raise HubStoreError("snapshot_capacity", "file snapshot storage capacity reached", 507)


def _insert(conn, tenant, body, digest, now):
    manifest = dict(body, files=[{key: value for key, value in row.items() if key != "content_b64"}
                                 for row in body["files"]])
    text = canonical_bytes(manifest).decode("utf-8")
    if len(text.encode("utf-8")) > _MANIFEST_LIMIT:
        raise ValueError("snapshot manifest exceeds its storage limit")
    duplicate = conn.execute("SELECT 1 FROM hub_file_snapshot_sets WHERE tenant=? AND snapshot_set_id=?",
                             (tenant, body["snapshot_set_id"])).fetchone()
    if duplicate:
        raise HubStoreError("snapshot_conflict", "snapshot set ID already belongs to another turn")
    conn.execute("INSERT INTO hub_file_snapshot_sets VALUES(?,?,?,?,?,?,?,?)",
                 (tenant, body["instance_id"], body["session_id"], body["turn_id"],
                  body["snapshot_set_id"], digest, text, now))
    conn.executemany("INSERT INTO hub_file_snapshot_bytes VALUES(?,?,?,?)", [
        (tenant, body["turn_id"], row["snapshot_id"], base64.b64decode(row["content_b64"], validate=True))
        for row in body["files"]])


def _stored(conn, tenant, session_id, turn_id):
    row = conn.execute("SELECT substr(instance_id,1,257) AS instance_id,"
                       "substr(session_id,1,257) AS session_id,"
                       "substr(snapshot_set_id,1,33) AS snapshot_set_id,"
                       "CASE WHEN typeof(created_at) IN ('integer','real') THEN created_at END AS created_at,"
                       "substr(payload_digest,1,65) AS payload_digest,"
                       "substr(manifest_json,1,?) AS manifest_json,"
                       "length(CAST(manifest_json AS BLOB)) AS manifest_size "
                       "FROM hub_file_snapshot_sets WHERE tenant=? AND turn_id=?",
                       (_MANIFEST_LIMIT + 1, tenant, turn_id)).fetchone()
    if row is None:
        return None
    try:
        if row["session_id"] != session_id or row["manifest_size"] > _MANIFEST_LIMIT:
            raise ValueError("invalid snapshot storage binding")
        body = _restore(conn, tenant, turn_id, row["manifest_json"])
        if (body["session_id"] != session_id or body["turn_id"] != turn_id
                or body["instance_id"] != row["instance_id"]
                or body["snapshot_set_id"] != row["snapshot_set_id"]
                or hashlib.sha256(canonical_bytes(body)).hexdigest() != row["payload_digest"]
                or not math.isfinite(row["created_at"]) or row["created_at"] < 0):
            raise ValueError("invalid snapshot storage integrity")
        return body, row["created_at"], row["payload_digest"]
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise HubStoreError("snapshot_integrity", "stored file snapshot failed integrity validation", 503) from None


def _restore(conn, tenant, turn_id, manifest):
    body = json.loads(manifest)
    rows = conn.execute("SELECT substr(snapshot_id,1,33) AS snapshot_id,length(content) AS size,"
                        "CASE WHEN typeof(content)='blob' AND length(content)=0 THEN x'' "
                        "ELSE substr(content,1,?) END AS content "
                        "FROM hub_file_snapshot_bytes WHERE tenant=? AND turn_id=? LIMIT ?",
                        (MAX_FILE_BYTES + 1, tenant, turn_id, MAX_PATHS + 1)).fetchall()
    if not isinstance(body, dict) or not isinstance(body.get("files"), list) or len(body["files"]) != len(rows):
        raise ValueError("snapshot manifest and byte rows differ")
    contents = {opaque_id(row["snapshot_id"]): row for row in rows}
    for entry in body["files"]:
        if "content_b64" in entry:
            raise ValueError("snapshot manifest contains unexpected bytes")
        item = contents[entry["snapshot_id"]]
        if item["size"] > MAX_FILE_BYTES or not isinstance(item["content"], bytes):
            raise ValueError("stored snapshot content exceeds its byte limit")
        entry["content_b64"] = base64.b64encode(item["content"]).decode("ascii")
    return validate_upload(body)


def _projection(body, created_at):
    result = {key: body[key] for key in ("schema", "instance_id", "session_id", "turn_id", "snapshot_set_id")}
    result.update(created_at=created_at, verification=VERIFICATION,
                  files=[file_metadata(row) for row in body["files"]], unavailable=body["unavailable"])
    result["state"] = ("partial" if body["unavailable"] else "ready") if body["files"] else "unavailable"
    return result


class FileSnapshotStore:
    def __init__(self, hub):
        self.hub = hub

    def put(self, tenant, body):
        body = validate_upload(body)
        digest = hashlib.sha256(canonical_bytes(body)).hexdigest()
        with self.hub._write() as conn:
            now = float(self.hub.clock())
            _binding(conn, tenant, body, now, self.hub.offline_after)
            existing = _stored(conn, tenant, body["session_id"], body["turn_id"])
            if existing:
                if existing[2] != digest:
                    raise HubStoreError("snapshot_conflict", "turn already has a different immutable snapshot set")
                return _projection(existing[0], existing[1])
            _capacity(conn, tenant, body)
            _insert(conn, tenant, body, digest, now)
            return _projection(body, now)

    def read(self, tenant, session_id, turn_id, snapshot_id=None):
        if snapshot_id is not None:
            opaque_id(snapshot_id)
        conn = self.hub._connect()
        try:
            conn.execute("BEGIN")
            session = get_session(conn, tenant, session_id)
            get_turn_for_session(conn, tenant, turn_id, session_id)
            stored = _stored(conn, tenant, session_id, turn_id)
            if stored is None:
                if snapshot_id is not None:
                    raise HubStoreError("not_found", "file snapshot not found", 404)
                return {"schema": SCHEMA, "instance_id": session["instance_id"], "session_id": session_id,
                        "turn_id": turn_id, "snapshot_set_id": None, "state": "absent", "created_at": None,
                        "files": [], "unavailable": [], "verification": VERIFICATION}
            body, created_at, _digest = stored
            if body["instance_id"] != session["instance_id"]:
                raise HubStoreError("snapshot_integrity", "stored snapshot instance binding is invalid", 503)
            result = _projection(body, created_at)
            return result if snapshot_id is None else _file_result(result, body, snapshot_id)
        finally:
            conn.close()


def _file_result(manifest, body, snapshot_id):
    for row in body["files"]:
        if row["snapshot_id"] == snapshot_id:
            common = {key: value for key, value in manifest.items() if key not in {"files", "unavailable", "state"}}
            return dict(common, **file_metadata(row), content_b64=row["content_b64"])
    raise HubStoreError("not_found", "file snapshot not found", 404)
