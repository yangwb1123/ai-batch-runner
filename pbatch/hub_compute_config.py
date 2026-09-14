"""Operator-owned per-tenant Fabric and workspace bindings."""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from .fabric_rpc import RpcClient, tls_context_for_url
from .hub_client import _read_private_token, validate_hub_url
from .hub_compute_models import fields
from .hub_store_helpers import identifier


class ComputeConnection:
    def __init__(self, settings: dict):
        fields(settings, {"tenant_id", "base_url", "credential_file", "ca_file",
                          "cert_file", "key_file", "bindings", "vault"})
        self.tenant = identifier(settings.get("tenant_id"), "tenant_id")
        self.url = validate_hub_url(settings.get("base_url", ""))
        self.credential_file = identifier(settings.get("credential_file"), "credential_file", 4096)
        self.tls = tls_context_for_url(self.url, settings.get("ca_file", ""),
                                       settings.get("cert_file", ""), settings.get("key_file", ""))
        self.bindings = _bindings(settings.get("bindings"))
        self.archive = _archive(settings.get("vault"), self.tenant)
        self.client()  # Invalid local credentials fail before the listener starts.

    def client(self):
        credential = json.loads(_read_private_token(self.credential_file))
        fields(credential, {"device_id", "secret"})
        return RpcClient(self.url, identifier(credential.get("secret"), "secret", 4096),
                         identifier(credential.get("device_id"), "device_id"), self.tls)


def _bindings(value) -> dict:
    if not isinstance(value, list) or not 1 <= len(value) <= 200:
        raise ValueError("compute bindings must contain 1..200 entries")
    result = {}
    for row in value:
        fields(row, {"device_id", "instance_id", "name", "workspaces"})
        device = identifier(row.get("device_id"), "device_id")
        instance = identifier(row.get("instance_id"), "instance_id")
        if device in result:
            raise ValueError("duplicate compute device binding")
        workspaces = row.get("workspaces")
        if not isinstance(workspaces, dict) or not 1 <= len(workspaces) <= 200:
            raise ValueError("workspaces must map project ids to absolute POSIX roots")
        for project, path in workspaces.items():
            identifier(project, "project_id")
            identifier(path, "workspace_root", 4096)
            if not PurePosixPath(path).is_absolute() or ".." in path.split("/") or "\\" in path:
                raise ValueError("workspace root must be an absolute POSIX path")
        name = identifier(row.get("name", device), "device name")
        result[device] = dict(row, instance_id=instance, name=name)
    return result


def _archive(value, tenant):
    if value is None:
        return None
    from .hub_vault import VaultArchiveSettings, VaultEvidenceArchive
    fields(value, {"base_url", "tenant_id", "token_file", "prefix"})
    if value.get("tenant_id") != tenant:
        raise ValueError("Vault tenant must match its compute connection")
    return VaultEvidenceArchive(VaultArchiveSettings(**value))


def connections(settings) -> dict:
    if not isinstance(settings, list) or len(settings) > 100:
        raise ValueError("compute must be a bounded list of tenant connections")
    result, endpoints = {}, set()
    for settings_row in settings:
        connection = ComputeConnection(settings_row)
        if connection.tenant in result or connection.url.lower() in endpoints:
            raise ValueError("each compute tenant requires a distinct Fabric endpoint")
        endpoints.add(connection.url.lower())
        result[connection.tenant] = connection
    return result
