"""Registration lifecycle and approval helpers for the device control plane."""

from __future__ import annotations


def prepare_registration_lifecycle(control, device_id: str, probe) -> str:
    """Move a registered Runner through enrollment and capability probing."""
    state = control.lifecycle.current(device_id)
    if state == "unregistered":
        error = control._transition_device(device_id, "enrolled",
                                            "runner registered")
        if error:
            return error
        state = "enrolled"
    if isinstance(probe, dict) and state in ("enrolled", "offline", "stale"):
        return control._transition_device(device_id, "probing",
                                          "runner capability probe")
    return ""


def registration_result(control, device_id: str, name: str,
                        protocol: str, capabilities: tuple) -> dict:
    """Apply auto/manual approval and return the stable registration response."""
    if control.auto_approve:
        control._approve(device_id, name, "auto")
    if device_id not in control._approval_ids():
        return {"approved": False, "message": "pending approval",
                "hint": "运行 pi-batch devices approve <name>"}
    state = control.lifecycle.current(device_id)
    if state in ("enrolled", "probing", "offline", "stale"):
        error = control._transition_device(device_id, "ready", "runner approved")
        if error:
            return {"approved": False, "error": error}
    control._record_event(device_id, "register", "device")
    return {"approved": True, "message": "approved",
            "protocol_version": protocol,
            "capabilities": list(capabilities)}
