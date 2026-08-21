"""Control-plane task snapshot recovery and fencing after restart."""

from __future__ import annotations

import threading

from pbatch import fabric, fabric_server
from pbatch.fabric_task_store import TaskStateError
from pbatch.fabric_rpc import RpcClient


def _start(state_dir, port=0):
    httpd, actual = fabric_server.start_server(
        port, str(state_dir), auto_approve=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, actual


def _stop(httpd, thread):
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def test_inflight_task_is_recovered_and_old_lease_is_fenced(tmp_path):
    state = tmp_path / "state"
    httpd, thread, port = _start(state)
    client = RpcClient(f"http://127.0.0.1:{port}", "recover-secret", "rn-recover")
    try:
        assert client.register("node-recover", fabric.local_probe())["approved"]
        task = client.submit_task(["echo", "recover"])["task"]
        old_lease = task["lease"]
        assert (state / "tasks.jsonl").exists()
    finally:
        _stop(httpd, thread)

    httpd2, thread2, port2 = _start(state, port)
    client2 = RpcClient(f"http://127.0.0.1:{port2}",
                        "recover-secret", "rn-recover")
    try:
        recovered = httpd2.control.tasks[task["task_id"]]
        assert recovered["status"] == "queued"
        assert recovered["device_id"] is None
        assert recovered["attempts"][-1]["status"] == "lost"
        stale = client2.evidence(
            task["task_id"], old_lease["lease_id"],
            old_lease["fencing_token"], {"result": "passed"},
            task_id=task["task_id"])
        assert stale["accepted"] is False
        assert "stale" in stale["reason"]
        polled = client2.poll_tasks()
        assert polled["tasks"]
        assert polled["tasks"][0]["lease"]["lease_id"] != old_lease["lease_id"]
    finally:
        _stop(httpd2, thread2)


def test_terminal_task_snapshot_survives_restart(tmp_path):
    state = tmp_path / "state"
    httpd, thread, port = _start(state)
    client = RpcClient(f"http://127.0.0.1:{port}", "done-secret", "rn-done")
    try:
        assert client.register("node-done", fabric.local_probe())["approved"]
        task = client.submit_task(["echo", "done"])["task"]
        polled = client.poll_tasks()["tasks"][0]
        evidence = fabric.LocalExecutionTarget().execute(
            fabric.ExecutionSpec(argv=["echo", "done"])).to_dict()
        assert client.evidence(
            task["task_id"], polled["lease"]["lease_id"],
            polled["lease"]["fencing_token"], evidence,
            task_id=task["task_id"])["accepted"]
        assert client.task_status(task["task_id"])["task"]["status"] == "done"
    finally:
        _stop(httpd, thread)

    httpd2, thread2, port2 = _start(state, port)
    client2 = RpcClient(f"http://127.0.0.1:{port2}", "done-secret", "rn-done")
    try:
        recovered = client2.task_status(task["task_id"])["task"]
        assert recovered["status"] == "done"
        assert recovered["evidence_digest"]
    finally:
        _stop(httpd2, thread2)


def test_checkpoint_survives_control_plane_restart(tmp_path):
    state = tmp_path / "state"
    httpd, thread, port = _start(state)
    client = RpcClient(f"http://127.0.0.1:{port}", "cp-secret", "rn-cp")
    try:
        assert client.register("node-cp", fabric.local_probe())["approved"]
        task = client.submit_task(["echo", "checkpoint"],
                                  mobility="checkpointable")["task"]
        lease = task["lease"]
        uploaded = client.checkpoint_upload(
            task["task_id"], "Y2hlY2twb2ludA==", lease["lease_id"],
            lease["fencing_token"])
        assert uploaded["ok"] is True
    finally:
        _stop(httpd, thread)

    httpd2, thread2, port2 = _start(state, port)
    try:
        # checkpointable 任务重启后按策略标 LOST，但检查点事实仍可恢复；
        # 新租约须由后续显式迁移/重试流程签发后才能通过 RPC 读取。
        assert httpd2.control.tasks[task["task_id"]]["status"] == "lost"
        checkpoint = httpd2.control.checkpoints[task["task_id"]]
        assert checkpoint["blob_b64"] == "Y2hlY2twb2ludA=="
    finally:
        _stop(httpd2, thread2)


def test_failed_task_admission_tombstones_checkpoint(tmp_path, monkeypatch):
    """任务快照提交失败时，重启后不能复活孤儿 checkpoint。"""
    state = tmp_path / "state"
    httpd, thread, port = _start(state)
    client = RpcClient(f"http://127.0.0.1:{port}", "rollback-secret",
                       "rn-rollback")
    try:
        assert client.register("node-rollback", fabric.local_probe())[
            "approved"]

        def fail_task_persist(_task):
            raise TaskStateError("injected task snapshot failure")

        monkeypatch.setattr(httpd.control, "_persist_task", fail_task_persist)
        result = client.submit_task(
            ["echo", "rollback"], checkpoint_blob="Y2hlY2s=",
            mobility="checkpointable", checkpoint_remote="state.json")
        assert result["task"] is None
        assert httpd.control.checkpoints == {}
        assert httpd.control.checkpoint_store.load() == {}
    finally:
        _stop(httpd, thread)

    httpd2, thread2, port2 = _start(state, port)
    try:
        assert httpd2.control.checkpoints == {}
    finally:
        _stop(httpd2, thread2)
