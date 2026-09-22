"""Instance registration publishes bounded capabilities and fences replaced boots."""

from .hub_session_close_models import lose_pending as lose_pending_closes
from .hub_session_request_models import capacity, guard_capacity_update, lose_pending
from .hub_store_helpers import (
    MAX_METADATA_BYTES, HubStoreError, boolean, canonical_json, identifier,
    instance_record, safe_object, takeover_generation,
)


def register_instance(hub, tenant, instance, body):
    tenant, instance = identifier(tenant, "tenant"), identifier(instance, "instance")
    if not isinstance(body, dict):
        raise ValueError("instance body must be an object")
    boot_id = identifier(body.get("boot_id"), "boot_id")
    metadata = safe_object({key: body[key] for key in (
        "name", "host", "version", "capabilities", "labels", "project_id") if key in body},
        MAX_METADATA_BYTES, "instance metadata")
    metadata["session_capacity"] = capacity(body.get("session_capacity", 0))
    metadata["session_close_supported"] = boolean(
        body.get("session_close_supported", False), "session_close_supported")
    now = float(hub.clock())
    with hub._write() as conn:
        row = conn.execute("SELECT * FROM hub_instances WHERE tenant=? AND instance_id=?",
                           (tenant, instance)).fetchone()
        generation = registration_generation(hub, conn, row, tenant, instance, boot_id, now)
        guard_capacity_update(conn, tenant, instance, generation, metadata["session_capacity"])
        conn.execute(
            "INSERT INTO hub_instances(tenant,instance_id,boot_id,generation,last_seen,metadata_json) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(tenant,instance_id) DO UPDATE SET "
            "boot_id=excluded.boot_id,generation=excluded.generation,last_seen=excluded.last_seen,"
            "metadata_json=excluded.metadata_json",
            (tenant, instance, boot_id, generation, now, canonical_json(metadata)))
        return instance_record(conn.execute(
            "SELECT * FROM hub_instances WHERE tenant=? AND instance_id=?",
            (tenant, instance)).fetchone(), now, hub.offline_after)


def registration_generation(hub, conn, row, tenant, instance, boot_id, now):
    if row is None:
        return 1
    if row["boot_id"] == boot_id:
        return int(row["generation"])
    if now - float(row["last_seen"]) <= hub.offline_after:
        raise HubStoreError("instance_active", "another boot is still active")
    lose_pending(conn, tenant, instance, now)
    lose_pending_closes(conn, tenant, instance, now)
    takeover_generation(conn, tenant, instance, now)
    return int(row["generation"]) + 1
