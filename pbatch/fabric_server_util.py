"""Control plane utilities (split from fabric_server.py for line budget)."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from pathlib import Path, PureWindowsPath

from .fabric_checkpoint_store import CheckpointStateError
from .fabric_task_store import task_locked
from .clock import lease_window

# 检查点上限（base64 256KB ≈ 192KB 原始状态；断点续传的小状态文件）。
CHECKPOINT_MAX_B64 = 256 * 1024
MAX_NETWORK_BUDGET_BYTES = (1 << 63) - 1


def parse_network_budget(payload: dict) -> tuple:
    """Parse an optional per-task network byte cap (zero means unlimited)."""
    raw = payload.get("network_budget_bytes", 0)
    if raw in (None, ""):
        return 0, ""
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return 0, "network budget must be a non-negative integer"
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return 0, "network budget must be a non-negative integer"
    if limit < 0 or limit > MAX_NETWORK_BUDGET_BYTES:
        return 0, "network budget must be a non-negative integer"
    return limit, ""


def checkpoint_remote_error(value) -> str:
    """Reject absolute/traversal checkpoint paths before task admission."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or "\x00" in value:
        return "checkpoint path must be a relative path"
    path = Path(value)
    windows = PureWindowsPath(value)
    if (path.is_absolute() or windows.is_absolute() or windows.drive
            or ".." in path.parts or ".." in windows.parts):
        return "checkpoint path must stay under task cwd"
    return ""


def read_lines_bounded(path: Path, max_bytes: int = 4 * 1024 * 1024) -> list:
    """有界行缓冲读取（超长行跳过，总量超限截断）。"""
    lines = []
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return lines
    with handle:
        total = 0
        for line in handle:
            total += len(line.encode("utf-8", errors="replace"))
            if total > max_bytes:
                break
            if len(line) > 256 * 1024:
                continue
            lines.append(line)
    return lines


def sha256(text: str) -> str:
    """短摘要（16 字符，检查点/指纹用）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def evidence_digest(evidence: dict) -> str:
    """证据规范摘要：同一 JSON 事实在重试时得到同一幂等指纹。"""
    raw = json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _evidence_shape(evidence: dict) -> tuple:
    """检查 JSON 形状并返回 (result, stdout, stderr, error)。"""
    if not isinstance(evidence, dict):
        return "", "", "", "evidence must be an object"
    result = str(evidence.get("result", ""))
    if result not in ("passed", "failed", "inconclusive"):
        return "", "", "", "invalid evidence result"
    stdout, stderr = evidence.get("stdout", ""), evidence.get("stderr", "")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return "", "", "", "evidence stdout/stderr must be strings"
    return result, stdout, stderr, ""


def _evidence_digest_error(evidence: dict, stdout: str, stderr: str,
                           expected_environment_digest: str) -> str:
    """检查必填摘要与输出内容绑定。"""
    required = ("attempt_id", "output_digest", "environment_digest",
                "runner_version", "elapsed_seconds")
    missing = [key for key in required if not str(evidence.get(key, ""))]
    if missing:
        return "missing execution evidence fields: " + ", ".join(missing)
    expected = hashlib.sha256((stdout + stderr).encode(
        "utf-8", errors="replace")).hexdigest()
    actual = str(evidence.get("output_digest", ""))
    if actual not in (expected, expected[:16]):
        return "output digest mismatch"
    if (expected_environment_digest
            and str(evidence.get("environment_digest", ""))
            not in (expected_environment_digest,
                    expected_environment_digest[:16])):
        return "environment digest mismatch"
    return ""


def _evidence_state_error(evidence: dict, result: str) -> str:
    """检查耗时、退出码与 PASS 结论是否一致。"""
    try:
        elapsed = float(evidence.get("elapsed_seconds"))
        exit_code = int(evidence.get("exit_code"))
    except (TypeError, ValueError):
        return "invalid execution evidence numbers"
    if not math.isfinite(elapsed) or elapsed < 0:
        return "invalid elapsed_seconds"
    if result == "passed" and (exit_code != 0 or evidence.get("timed_out")):
        return "passed evidence has non-success execution state"
    return ""


def validate_execution_evidence(evidence: dict, require_digest: bool = False,
                                expected_environment_digest: str = "") -> str:
    """验证 Runner 证据的最小契约；返回错误文本（空 = 通过）。"""
    result, stdout, stderr, error = _evidence_shape(evidence)
    if error:
        return error
    if not require_digest:
        return ""
    error = _evidence_digest_error(evidence, stdout, stderr,
                                   expected_environment_digest)
    return error or _evidence_state_error(evidence, result)


def evidence_usage(evidence: dict) -> dict:
    """Extract observed resource usage from bounded execution evidence."""
    stdout = evidence.get("stdout", "") if isinstance(evidence, dict) else ""
    stderr = evidence.get("stderr", "") if isinstance(evidence, dict) else ""
    output_bytes = len(str(stdout).encode("utf-8", errors="replace"))
    output_bytes += len(str(stderr).encode("utf-8", errors="replace"))
    return {
        "elapsed_seconds": evidence.get("elapsed_seconds", 0),
        "output_bytes": output_bytes,
        "input_bytes": evidence.get("input_bytes", 0),
        "network_bytes": evidence.get("network_bytes", 0),
        "estimated_cost": evidence.get("estimated_cost", 0),
        "cpu_seconds": evidence.get("cpu_seconds", 0),
    }


class CheckpointOpsMixin:
    """检查点与设备吊销处理器（从 ControlPlane 拆出以控制行数）。

    依赖 self.checkpoints / self.state_dir / self.secrets / self.tasks /
    self.cordoned / self.revoked / self.revoked.add。
    """

    def _tenant_of(self, device_id: str) -> str:
        device = self.devices.get(device_id, {})
        return str(device.get("tenant", "default"))

    def _tenant_quota_check(self, device_id: str) -> str:
        """多租户配额（D6）：进行中任务数超配额 → 拒绝新提交。"""
        tenant = self._tenant_of(device_id)
        quota = self.tenant_quotas.get(tenant)
        if quota is None:
            return ""
        running = self.tenant_running.get(tenant, 0)
        if running >= quota:
            return (f"tenant quota exhausted ({tenant}: {running}/{quota} "
                    "进行中)")
        return ""

    def _tenant_quota_inc(self, device_id: str) -> None:
        tenant = self._tenant_of(device_id)
        if tenant in self.tenant_quotas:
            self.tenant_running[tenant] = self.tenant_running.get(tenant, 0) + 1

    def _tenant_quota_dec(self, device_id: str) -> None:
        tenant = self._tenant_of(device_id)
        if tenant in self.tenant_quotas:
            self.tenant_running[tenant] = max(
                0, self.tenant_running.get(tenant, 0) - 1)


    def _device_quota_key(self, device_id: str) -> str:
        """配额键：优先 device_id（rn-xxx），其次注册名（node-xxx）。"""
        if device_id in self.device_quotas:
            return device_id
        name = self.devices.get(device_id, {}).get("name", "")
        return name if name in self.device_quotas else ""

    def _device_quota_check(self, device_id: str) -> str:
        """per-device 并发上限（D6+ 差值表：准入控制 per-device）。"""
        key = self._device_quota_key(device_id)
        if not key:
            return ""
        quota = self.device_quotas[key]
        running = self.device_running.get(key, 0)
        if running >= quota:
            return (f"device quota exhausted ({key}: "
                    f"{running}/{quota} 进行中)")
        return ""

    def _device_quota_inc(self, device_id: str) -> None:
        key = self._device_quota_key(device_id)
        if key:
            self.device_running[key] = self.device_running.get(key, 0) + 1

    def _device_quota_dec(self, device_id: str) -> None:
        key = self._device_quota_key(device_id)
        if key:
            self.device_running[key] = max(0, self.device_running.get(key, 0) - 1)

    def _record_event(self, actor: str, verb: str, target: str = "",
                      object_id: str = "", attempt: str = "") -> None:
        """追加式交互事件（AADM §4.1）：不可变、可回答谁/何时/何事。

        attempt 绑定租约编号（审计 device/attempt 维度）。"""
        record = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "actor": actor, "verb": verb, "target": target,
                  "object_id": object_id, "source": "control_plane"}
        if attempt:
            record["attempt"] = attempt
        self.events.append(record)
        if len(self.events) > 2000:
            self.events = self.events[-1000:]  # 有界内存
        try:
            with (self.state_dir / "events.jsonl").open(
                    "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
        # best-effort I/O：失败不阻塞主流程（已验证有意）
            pass

    def _issue_lease(self, device_id: str, resources: dict,
                     ttl_seconds: int = 300,
                     grant_context: dict = None) -> dict:
        """发放租约：epoch 单调递增，并携带短期任务级授权票据。"""
        ttl = max(30, min(int(ttl_seconds or 300), 86400))
        epoch = self.epochs.get(device_id, 0) + 1
        self.epochs[device_id] = epoch
        window = lease_window(ttl)
        now = window["issued_at"]
        context = dict(grant_context or {})
        expires_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(now + ttl))
        issued_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        grant = {
            "grant_id": uuid.uuid4().hex[:16],
            "agent_id": str(context.get("agent_id", device_id)),
            "node_id": str(context.get("node_id", device_id)),
            "task_node_id": str(context.get("task_node_id", "")),
            "device_id": device_id,
            "actions": list(context.get("actions", ["execute"])),
            "resources": list(context.get("grant_resources", ["*"])),
            "environment": str(context.get("environment", "")),
            "allowed_data_classes": list(
                context.get("allowed_data_classes", ["public"])),
            "max_effect_class": str(
                context.get("max_effect_class", "sandboxed")),
            "max_side_effect_class": str(
                context.get("max_effect_class", "sandboxed")),
            "capability": str(context.get("capability", "device.execute")),
            "issued_at": issued_iso, "expires_at": expires_iso,
        }
        return {
            "lease_id": uuid.uuid4().hex[:16],
            "device_id": device_id,
            "epoch": epoch,
            "fencing_token": epoch,
            "resources": resources,
            "expires_at": window["expires_at"],
            "clock_id": str(getattr(self, "clock_id", "")),
            "monotonic_expires_at": window["monotonic_expires_at"],
            # 换租约即换 fencing token 与 grant，旧任务票据随之失效。
            "grant": grant,
        }

    @task_locked
    def handle_desired_reconcile(self, payload: dict) -> dict:
        """Desired-State Controller：观察实际任务数 → 对比期望 → 补/撤
        直到 actual == desired（Kubernetes 核心模式的设备织网版）。"""
        services = payload.get("services", [])
        results = []
        for service in services if isinstance(services, list) else []:
            name = str(service.get("name", ""))
            replicas = max(0, int(service.get("replicas", 1) or 1))
            if not name:
                continue
            marker = f"__desired__:{name}"
            running = [task for task in self.tasks.values()
                       if task.get("status") in ("queued", "leased",
                                                 "running")
                       and any(marker in item
                               for item in task.get("argv", []))]
            diff = replicas - len(running)
            actions = []
            actual = len(running)
            if diff > 0:
                for _ in range(diff):
                    argv = [marker] + list(service.get("cmd", []))
                    task = self.handle_task_submit({
                        "device_id": payload.get("device_id", ""),
                        "argv": argv,
                        "effect_class": str(service.get(
                            "effect_class", "read_only")),
                        "timeout": 600,
                    }).get("task")
                    if task:
                        actions.append(f"scale_up: {task['task_id']}")
                        actual += 1
                    else:
                        actions.append("scale_up_failed: quota/budget")
                        break
            elif diff < 0:
                for task in running[:abs(diff)]:
                    self.handle_task_cancel({"device_id": payload.get(
                        "device_id", ""), "task_id": task["task_id"]})
                    actions.append(f"scale_down: {task['task_id']}")
                actual = replicas
            results.append({"service": name, "desired": replicas,
                            "actual": actual, "diff": diff,
                            "converged": actual == replicas,
                            "actions": actions})
        self._record_event(str(payload.get("device_id", "")),
                           "desired_reconcile", "services",
                           str(len(results)))
        return {"results": results, "converged": all(
            r["converged"] for r in results)}

    def handle_checkpoint_download(self, payload: dict) -> dict:
        """Runner 取任务检查点（断点续传：迁移后从上次状态继续）。"""
        device_id = str(payload.get("device_id", ""))
        task_id = str(payload.get("task_id", ""))
        lease_id = str(payload.get("lease_id", ""))
        task = self.tasks.get(task_id)
        if task is None:
            return {"has_checkpoint": False, "error": "unknown task"}
        _, error = self._evidence_lease(
            device_id, task_id, lease_id, payload.get("fencing_token"))
        if error:
            return {"has_checkpoint": False, "error": error}
        checkpoint = self.checkpoints.get(task_id)
        if checkpoint is None:
            return {"has_checkpoint": False}
        return {"has_checkpoint": True,
                "blob_b64": checkpoint["blob_b64"],
                "digest": checkpoint["digest"],
                "size": checkpoint["size"]}

    def _persist_checkpoint(self, task_id: str) -> str:
        try:
            self.checkpoint_store.save(task_id, self.checkpoints[task_id])
        except CheckpointStateError as exc:
            return str(exc)
        return ""

    def _delete_checkpoint(self, task_id: str) -> str:
        """Remove an in-memory checkpoint and durably append its tombstone."""
        self.checkpoints.pop(task_id, None)
        try:
            self.checkpoint_store.delete(task_id)
        except CheckpointStateError as exc:
            return f"checkpoint rollback: {exc}"
        return ""

    def _prune_orphan_checkpoints(self) -> None:
        """Drop checkpoint facts whose task snapshot is absent after recovery."""
        for task_id in list(self.checkpoints):
            if task_id not in self.tasks:
                self._delete_checkpoint(task_id)

    def handle_checkpoint_upload(self, payload: dict) -> dict:
        """Runner 执行后回传检查点（有界、幂等覆盖）。"""
        device_id = str(payload.get("device_id", ""))
        task_id = str(payload.get("task_id", ""))
        blob = payload.get("blob_b64", "")
        if not isinstance(blob, str) or not blob:
            return {"ok": False, "error": "missing blob"}
        if len(blob) > CHECKPOINT_MAX_B64:
            return {"ok": False, "error": "checkpoint too large"}
        if task_id not in self.tasks:
            return {"ok": False, "error": "unknown task"}
        _, error = self._evidence_lease(
            device_id, task_id, str(payload.get("lease_id", "")),
            payload.get("fencing_token"))
        if error:
            return {"ok": False, "error": error}
        previous = self.checkpoints.get(task_id)
        self.checkpoints[task_id] = {
            "blob_b64": blob, "size": len(blob),
            "digest": sha256(blob), "updated_at": time.time(),
        }
        if error := self._persist_checkpoint(task_id):
            if previous is None:
                self._delete_checkpoint(task_id)
            else:
                self.checkpoints[task_id] = previous
            return {"ok": False, "error": error}
        return {"ok": True, "digest": self.checkpoints[task_id]["digest"]}

    def _revocation_ids(self) -> list:
        """吊销记录（只追加；退役设备身份永久失效）。"""
        revoked = []
        path = self.state_dir / "revocations.jsonl"
        if not path.exists():
            return revoked
        for line in read_lines_bounded(path):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("device_id"):
                revoked.append(record["device_id"])
        return revoked

    @task_locked
    def handle_revoke(self, payload: dict) -> dict:
        """设备退役（§41.2）：吊销密钥、取消其任务、cordons 清理。"""
        device_id = str(payload.get("device_id", ""))
        if device_id not in self.secrets:
            return {"ok": False, "error": "unknown device"}
        lifecycle_error = self._transition_device(
            device_id, "retired", "operator revoke")
        if lifecycle_error:
            return {"ok": False, "error": lifecycle_error}
        record = {"device_id": device_id, "revoked_at": time.time(),
                  "reason": str(payload.get("reason", ""))[:200]}
        with (self.state_dir / "revocations.jsonl").open(
                "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.revoked.add(device_id)
        self.secrets.pop(device_id, None)
        self.cordoned.pop(device_id, None)
        cancelled = 0
        for task in self.tasks.values():
            if task.get("device_id") == device_id and task.get("status") in (
                    "queued", "leased", "running"):
                task["status"] = "lost"
                task["lost_at"] = time.time()
                self._mark_task_attempt(task, task.get("lease_id", ""),
                                        "lost", "device revoked")
                self._release_task_quota(task)
                self._persist_task(task)
                cancelled += 1
        self._record_event(device_id, "revoke", "device")
        return {"ok": True, "device_id": device_id,
                "tasks_marked_lost": cancelled,
                "message": "密钥已吊销：身份永久失效，需重新注册+审批"}

    def handle_rotate_certificate(self, payload: dict) -> dict:
        """Rotate the bound certificate fingerprint after current auth."""
        device_id = str(payload.get("device_id", ""))
        new_fingerprint = str(
            payload.get("new_certificate_fingerprint", "")).strip()
        if device_id not in self.secrets:
            return {"ok": False, "error": "unknown device"}
        if not new_fingerprint or len(new_fingerprint) > 128:
            return {"ok": False, "error": "invalid certificate fingerprint"}
        current = str(self.devices.get(device_id, {}).get(
            "certificate_fingerprint", ""))
        presented = str(payload.get("_client_cert_fingerprint", ""))
        if current == new_fingerprint:
            return {"ok": True, "device_id": device_id,
                    "certificate_fingerprint": current, "duplicate": True}
        if current and presented != current:
            return {"ok": False, "error": "current certificate mismatch"}
        device = self.devices[device_id]
        device["certificate_fingerprint"] = new_fingerprint
        self._persist_registration(
            device_id, str(device.get("name", "")), self.secrets[device_id],
            new_fingerprint, str(device.get("protocol_version", "")))
        self._record_event(device_id, "rotate_certificate", "device")
        return {"ok": True, "device_id": device_id,
                "certificate_fingerprint": new_fingerprint}
