"""Shared compute API for CLI, TUI and Gateway tool delegation."""

from __future__ import annotations

from urllib.parse import quote


class ComputeClient:
    def __init__(self, client):
        self.client = client

    def devices(self, project_id="", *, after=""):
        filters = {"project_id": project_id} if project_id else {}
        return self.client._list_pages("/devices", "devices", filters, after, 100, 20)

    def tasks(self, session_id=""):
        filters = {"session_id": session_id} if session_id else {}
        return self.client._list_pages("/tasks", "tasks", filters, "", 100, 20)

    def get(self, task_id):
        return self.client._request("GET", "/tasks/" + quote(task_id, safe=""))

    def placement(self, task_id, *, after="", limit=20):
        from .hub_compute_placement_client import read
        return read(self.client, task_id, after, limit)

    def submit(self, session_id, body, key):
        return self.client._request("POST", "/sessions/" + quote(session_id, safe="") + "/tasks",
                                    body, headers={"Idempotency-Key": key}, accepted=(202,))

    def cancel(self, task_id):
        return self.client._request("POST", "/tasks/" + quote(task_id, safe="") + "/cancel",
                                    {}, accepted=(202,))

    def reschedule(self, task_id, target_device_id=""):
        if not isinstance(target_device_id, str):
            raise ValueError("target_device_id must be text")
        return self.client._request(
            "POST", "/tasks/" + quote(task_id, safe="") + "/reschedule",
            {"target_device_id": target_device_id}, accepted=(202,))

    def retry(self, task_id, target_device_id="", *, confirm_duplicate=False):
        if not isinstance(target_device_id, str):
            raise ValueError("target_device_id must be text")
        if confirm_duplicate is not True:
            raise ValueError("confirm_duplicate must be true")
        return self.client._request(
            "POST", "/tasks/" + quote(task_id, safe="") + "/retry",
            {"target_device_id": target_device_id, "confirm_duplicate": True},
            accepted=(202,))

    def delegate(self, context, operation, payload=None, key=""):
        if operation == "placement":
            from .hub_compute_placement_client import delegated_parameters, read
            return read(self.client, **delegated_parameters(payload, key), context=context)
        client = self._workspace_client() if operation in {"snapshot-upload", "workspace-output"} else self.client
        return client._request("POST", "/gateway/compute",
                                    dict(context, operation=operation, payload=payload or {}, key=key))

    def snapshots(self, session_id, *, after=""):
        return self.client._list_pages("/sessions/" + quote(session_id, safe="") + "/snapshots",
                                       "snapshots", {}, after, 100, 20)

    def upload_snapshot(self, session_id, bundle, key):
        return self._workspace_client()._request("POST", "/sessions/" + quote(session_id, safe="") + "/snapshots",
                                    {"bundle": bundle}, headers={"Idempotency-Key": key})

    def workspace_output(self, task_id):
        return self._workspace_client()._request("GET", "/tasks/" + quote(task_id, safe="") + "/workspace")

    def _workspace_client(self):
        from copy import copy
        client = copy(self.client)
        client.timeout = 120
        return client
