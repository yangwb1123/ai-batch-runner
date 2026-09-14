"""Device-Aware Execution Fabric D3 control plane.

Registration, leases, evidence, reconciliation and bounded JSONL state live
here; HTTP transport is implemented by ``fabric_server_http``.
"""

from __future__ import annotations

import json
from fnmatch import fnmatchcase
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from . import config
from .config import log
from .capabilities import DATA_CLASS_LEVEL, EFFECT_ORDER
from .fabric import FABRIC_PROTOCOL
from .fabric_devices import _effective_device_state_dir
from .fabric_ledger import DeviceUsageLedger, UsageLedgerError
from .fabric_lifecycle import DeviceLifecycleStore, LifecycleError
from .fabric_server_registration import (prepare_registration_lifecycle,
                                          registration_result)
from .fabric_checkpoint_store import CheckpointStore
from .fabric_hub_compute import HubComputeMixin
from .fabric_gpu import normalize_gpu_assignment
from .fabric_workspace import workspace_evidence_error
from .fabric_task_store import TaskStateMixin, TaskStateStore, task_locked
from . import fabric_server_util
from .fabric_rpc import BODY_MAX_BYTES, verify_request
from .fabric_server_util import (CHECKPOINT_MAX_B64, evidence_digest,
                                 checkpoint_remote_error, evidence_usage,
                                 parse_network_budget,
                                 read_lines_bounded, sha256,
                                 validate_execution_evidence)
from .clock import lease_expired
# 心跳超时判定（秒）：超过该间隔未收到心跳的设备视为 stale。
HEARTBEAT_STALE_SECONDS = 90
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,63}$")
DEFAULT_DEVICE_POLICY = {
    "trust_zone": "untrusted-external", "trust_level": 0,
    "max_effect_class": "read_only", "allowed_data_classes": ["public"],
}
SUPPORTED_PROTOCOLS = (FABRIC_PROTOCOL,)
SERVER_CAPABILITIES = ("task.v1", "grant.v1", "checkpoint.v1")


def _terminal_evidence_replay(task: dict, digest: str,
                              task_id: str) -> dict | None:
    """Preserve same-digest evidence replay for already terminal tasks."""
    if (task.get("status") not in ("done", "failed", "cancelled")
            or not task.get("evidence_digest")):
        return None
    if task["evidence_digest"] != digest:
        return {"accepted": False, "reason": "evidence conflict"}
    return {"accepted": True, "duplicate": True, "task_id": task_id}


def _valid_name(name: str) -> bool:
    return bool(_ID_RE.fullmatch(name or ""))
def _normalize_device_policy(raw: dict) -> dict:
    """规范化服务端策略；坏配置降级到最小 public/read_only。"""
    raw = raw if isinstance(raw, dict) else {}
    trust = raw.get("trust") if isinstance(raw.get("trust"), dict) else {}
    zone = str(raw.get("trust_zone", trust.get("zone", "untrusted-external")))
    try:
        level = int(raw.get("trust_level", trust.get("level", 0)))
    except (TypeError, ValueError):
        level = 0
    level = level if 0 <= level <= 5 else 0
    maximum = str(raw.get(
        "max_effect_class", trust.get("max_effect_class", "read_only")))
    if maximum not in EFFECT_ORDER:
        maximum = "read_only"
    classes = raw.get("allowed_data_classes",
                      trust.get("allowed_data_classes", ["public"]))
    if not isinstance(classes, (list, tuple)):
        classes = ["public"]
    classes = [str(item).strip().lower() for item in classes
               if str(item).strip().lower() in DATA_CLASS_LEVEL]
    return {"trust_zone": zone or "untrusted-external",
            "trust_level": level, "max_effect_class": maximum,
            "allowed_data_classes": classes or ["public"]}
def _device_policy_map(explicit: Optional[dict]) -> dict:
    """合并显式策略与静态设备登记；显式项优先。"""
    policies = {}
    for key, value in (explicit or {}).items():
        policies[str(key)] = _normalize_device_policy(value)
    for entry in config.DEVICE_FABRIC_STATIC:
        if not isinstance(entry, dict):
            continue
        policy = _normalize_device_policy(entry)
        for key in (entry.get("name"), entry.get("device_id")):
            if key:
                policies.setdefault(str(key), policy)
    return policies
class ControlPlane(HubComputeMixin, fabric_server_util.CheckpointOpsMixin,
                   TaskStateMixin):
    """设备注册表 + 租约 + 证据账本的权威状态（进程内 + 追加式持久化）。"""
    def __init__(self, state_dir: str = "", auto_approve: bool = False,
                 budget_cap: float = 0.0,
                 tenant_quotas: Optional[dict] = None,
                 device_quotas: Optional[dict] = None,
                 device_policies: Optional[dict] = None,
                 require_client_cert: bool = False):
        self.state_dir = Path(state_dir or _effective_device_state_dir())
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass
        self.auto_approve = auto_approve
        # Monotonic lease deadlines are valid only within this server clock
        # epoch; persisted/audited wall timestamps remain the restart fallback.
        self.clock_id = uuid.uuid4().hex
        self.device_policies = _device_policy_map(device_policies)
        self.require_client_cert = bool(require_client_cert)
        self.secrets: dict = {}          # device_id -> secret（验签）
        self.devices: dict = {}          # device_id -> {name, last_seen, state}
        self.epochs: dict = {}           # device_id -> 当前 lease epoch
        self.leases: dict = {}           # lease_id -> lease（按租约索引）
        self.current_lease: dict = {}    # device_id -> lease_id（通用租约最新）
        self.task_leases: dict = {}      # task_id -> lease_id（任务租约最新）
        self.tasks: dict = {}            # task_id -> 任务记录
        self.cordoned: dict = {}         # device_id -> {drain, since}
        self.revoked: set = set()        # 退役设备（密钥吊销，永久拒绝）
        self.lifecycle = DeviceLifecycleStore(self.state_dir)
        self.events: list = []           # 内存事件（有界；追加式落盘）
        self.device_budget_cap = float(budget_cap or 0)   # 0 = 不限
        self.device_budget_spent: dict = {}  # device_id -> 累计消耗
        self.tenant_quotas: dict = {str(k): int(v) for k, v in
                                    (tenant_quotas or {}).items()}
        self.device_quotas: dict = {str(k): int(v) for k, v in
                                    (device_quotas or {}).items()}
        self.device_running: dict = {}
        self.tenant_running: dict = {}       # tenant -> 进行中任务数
        self.checkpoint_store = CheckpointStore(self.state_dir)
        self.checkpoints: dict = self.checkpoint_store.load()
        self.usage_ledger = DeviceUsageLedger(self.state_dir)
        self.task_store = TaskStateStore(self.state_dir)
        self._task_lock = threading.RLock()
        self.replay: dict = {}           # nonce 防重放缓存（有界）
        self._load_runners()
        self._recover_tasks()
        self._prune_orphan_checkpoints()
    # -- 持久化 ------------------------------------------------------------
    def _load_runners(self) -> None:
        """启动时读回已注册设备（secret 恢复，验签可用；吊销的不恢复）。"""
        self.revoked = set(self._revocation_ids())
        path = self.state_dir / "runners.jsonl"
        if not path.exists():
            return
        for line in _read_lines_bounded(path):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if (isinstance(record, dict) and record.get("device_id")
                    and record["device_id"] not in self.revoked):
                self.secrets[record["device_id"]] = str(record.get("secret", ""))
                device = self.devices.setdefault(record["device_id"], {
                    "name": str(record.get("name", "")),
                    "last_seen": 0, "dynamic_state": {}, "heartbeats": 0,
                    "certificate_fingerprint": str(
                        record.get("certificate_fingerprint", "")),
                    "protocol_version": str(record.get("protocol_version", "")),
                })
                if record.get("certificate_fingerprint"):
                    device["certificate_fingerprint"] = str(
                        record["certificate_fingerprint"])
                self._set_device_policy(record["device_id"])
                if self.lifecycle.current(record["device_id"]) == "unregistered":
                    self.lifecycle.transition(record["device_id"], "enrolled",
                                              reason="recovered runner")

    def _transition_device(self, device_id: str, state: str,
                           reason: str) -> str:
        """生命周期转移门；非法转移直接返回错误而不修改业务状态。"""
        try:
            self.lifecycle.transition(device_id, state, reason=reason)
        except LifecycleError as exc:
            log.error("device lifecycle transition rejected: %s", exc)
            return str(exc)
        return ""

    def _persist_registration(self, device_id: str, name: str,
                              secret: str, certificate_fingerprint: str = "",
                              protocol_version: str = "") -> None:
        record = {"device_id": device_id, "name": name, "secret": secret,
                  "certificate_fingerprint": certificate_fingerprint,
                  "protocol_version": protocol_version,
                  "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        path = self.state_dir / "runners.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _approval_ids(self) -> set:
        """读回审批记录（每次注册实时读，CLI 审批无需重启服务）。"""
        approved = set()
        path = self.state_dir / "approvals.jsonl"
        if not path.exists():
            return approved
        for line in _read_lines_bounded(path):
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("device_id"):
                approved.add(record["device_id"])
        return approved

    def _approve(self, device_id: str, name: str, by: str) -> None:
        record = {"device_id": device_id, "name": name,
                  "approved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "approved_by": by}
        with (self.state_dir / "approvals.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # -- 请求处理 ----------------------------------------------------------

    def secret_for(self, device_id: str) -> Optional[str]:
        return self.secrets.get(device_id)

    def _policy_for(self, device_id: str) -> dict:
        device = self.devices.get(device_id, {})
        name = str(device.get("name", ""))
        policy = (self.device_policies.get(device_id)
                  or self.device_policies.get(name)
                  or device.get("policy") or DEFAULT_DEVICE_POLICY)
        return dict(policy)

    def _set_device_policy(self, device_id: str) -> None:
        device = self.devices.setdefault(device_id, {})
        policy = self._policy_for(device_id)
        device["policy"] = policy
        device["trust"] = {"level": policy["trust_level"],
                            "zone": policy["trust_zone"],
                            "max_effect_class": policy["max_effect_class"],
                            "allowed_data_classes": list(
                                policy["allowed_data_classes"])}

    def _task_policy_error(self, device_id: str, effect_class: str,
                           data_class: str) -> str:
        """服务端策略门：设备自报 probe 不参与授权。"""
        policy = self._policy_for(device_id)
        if effect_class not in EFFECT_ORDER:
            return f"unknown effect class: {effect_class}"
        maximum = policy["max_effect_class"]
        if EFFECT_ORDER[effect_class] > EFFECT_ORDER[maximum]:
            return f"effect {effect_class} exceeds device policy {maximum}"
        if data_class not in DATA_CLASS_LEVEL:
            return f"unknown data class: {data_class}"
        if DATA_CLASS_LEVEL[data_class] > policy["trust_level"]:
            return f"data class {data_class} exceeds trust level"
        allowed = policy["allowed_data_classes"]
        if not any(fnmatchcase(data_class, pattern) for pattern in allowed):
            return f"data class {data_class} is not allowed on device"
        return ""

    def _grant_context(self, device_id: str, task_id: str,
                       effect_class: str, data_class: str, cwd: str,
                       workspace: bool = False) -> dict:
        """构造最小任务票据上下文，供 lease/fencing 一起签发。"""
        policy = self._policy_for(device_id)
        resources = [cwd or "*"]
        if workspace and cwd:
            resources.append(cwd.rstrip("/") + "/.pbatch-workspaces/*")
        return {"agent_id": device_id, "node_id": device_id,
                "task_node_id": task_id, "actions": ["execute"],
                "grant_resources": resources,
                "environment": policy["trust_zone"],
                "allowed_data_classes": [data_class],
                "max_effect_class": effect_class,
                "capability": "device.execute"}

    def _registration_certificate_error(self, device_id: str,
                                        certificate: str) -> str:
        """mTLS 注册门：证书指纹必须稳定绑定 device_id。"""
        if self.require_client_cert and not certificate:
            return "client certificate required"
        known = str(self.devices.get(device_id, {}).get(
            "certificate_fingerprint", ""))
        if known and known != certificate:
            return "certificate mismatch"
        if self.require_client_cert and device_id in self.secrets and not known:
            return "device certificate not enrolled"
        return ""

    def _protocol_error(self, probe) -> str:
        """协议协商门：空版本兼容旧 Runner，其余必须受支持。"""
        if not isinstance(probe, dict):
            return ""
        version = str(probe.get("protocol_version", "")).strip()
        if version and version not in SUPPORTED_PROTOCOLS:
            return f"unsupported protocol: {version}"
        return ""

    def _store_registration(self, device_id: str, name: str, secret: str,
                            certificate: str, probe, known) -> None:
        probe_map = probe if isinstance(probe, dict) else {}
        protocol = str(probe_map.get(
            "protocol_version",
            self.devices.get(device_id, {}).get("protocol_version", "")))
        if known is None:
            self.secrets[device_id] = secret
            self.devices.setdefault(device_id, {
                "name": name, "last_seen": 0, "dynamic_state": {},
                "heartbeats": 0, "certificate_fingerprint": certificate,
            })["name"] = name
            self.devices[device_id]["certificate_fingerprint"] = certificate
        elif certificate:
            self.devices[device_id]["certificate_fingerprint"] = certificate
        else:
            return
        self._persist_registration(device_id, name, secret, certificate,
                                   protocol)

    def _store_probe_metadata(self, device_id: str, probe: dict,
                              payload: dict) -> None:
        if isinstance(probe, dict):
            self.devices[device_id]["probe"] = probe
            self.devices[device_id]["protocol_version"] = str(
                probe.get("protocol_version", ""))
            self.devices[device_id]["runner_capabilities"] = list(
                probe.get("capabilities", []) or [])
        for meta_key in ("tenant", "region", "attestation"):
            value = payload.get(meta_key)
            if value:
                self.devices[device_id][meta_key] = str(value)[:128]

    def handle_register(self, payload: dict) -> dict:
        """TOFU 注册：设备自生成 secret 首次声称身份；是否放行由审批决定。"""
        device_id = str(payload.get("device_id", ""))
        name = str(payload.get("name", ""))
        secret = str(payload.get("secret", ""))
        certificate = str(payload.get("_client_cert_fingerprint", ""))
        if not _valid_name(device_id) or not _valid_name(name) or not secret:
            return {"approved": False, "error": "missing or invalid fields"}
        if device_id in self.revoked:
            return {"approved": False, "error": "device revoked"}
        if self.lifecycle.current(device_id) == "retired":
            return {"approved": False, "error": "device retired"}
        known = self.secrets.get(device_id)
        if known is not None and known != secret:
            return {"approved": False, "error": "secret mismatch"}
        certificate_error = self._registration_certificate_error(
            device_id, certificate)
        if certificate_error:
            return {"approved": False, "error": certificate_error}
        probe = payload.get("probe")
        protocol_error = self._protocol_error(probe)
        if protocol_error:
            return {"approved": False, "error": protocol_error}
        self._store_registration(device_id, name, secret, certificate,
                                 probe, known)
        self._store_probe_metadata(device_id, probe, payload)
        self._set_device_policy(device_id)
        lifecycle_error = prepare_registration_lifecycle(self, device_id, probe)
        if lifecycle_error:
            return {"approved": False, "error": lifecycle_error}
        return registration_result(self, device_id, name, FABRIC_PROTOCOL,
                                   SERVER_CAPABILITIES)

    def handle_heartbeat(self, payload: dict) -> dict:
        device_id = str(payload.get("device_id", ""))
        state = payload.get("dynamic_state", {})
        if not isinstance(state, dict):
            state = {}
        device = self.devices.setdefault(device_id, {"name": "", "last_seen": 0,
                                                     "dynamic_state": {},
                                                     "heartbeats": 0})
        device["last_seen"] = time.time()
        device["heartbeats"] += 1
        device["dynamic_state"] = state
        device["running_attempts"] = payload.get("running_attempts", [])
        lifecycle_error = self._sync_lifecycle_from_heartbeat(
            device_id, device["running_attempts"])
        if lifecycle_error:
            return {"ok": False, "error": lifecycle_error}
        return {"ok": True, "server_time": time.time(),
                "heartbeats": device["heartbeats"]}

    def _sync_lifecycle_from_heartbeat(self, device_id: str,
                                       running_attempts: list) -> str:
        """心跳把在线/工作事实映射到生命周期，不解除 operator cordon。"""
        state = self.lifecycle.current(device_id)
        if state in ("offline", "stale"):
            try:
                self.lifecycle.transition_path(
                    device_id, ("probing", "ready"),
                    reason="runner heartbeat verified")
            except LifecycleError as exc:
                log.error("device lifecycle transition rejected: %s", exc)
                return str(exc)
            state = "ready"
        elif state in ("enrolled", "probing"):
            error = self._transition_device(device_id, "ready",
                                            "runner heartbeat verified")
            if error:
                return error
            state = "ready"
        if state not in ("ready", "reserved", "busy"):
            return ""
        active = any(task.get("device_id") == device_id
                     and task.get("status") in ("queued", "leased", "running")
                     for task in self.tasks.values())
        desired = "busy" if running_attempts else ("reserved" if active else "ready")
        if desired != state:
            return self._transition_device(device_id, desired,
                                           "runner heartbeat workload")
        return ""

    def handle_lease(self, payload: dict) -> dict:
        """通用租约（CLI/诊断用）：设备级最新租约。"""
        device_id = str(payload.get("device_id", ""))
        if device_id not in self._approval_ids():
            return {"lease": None, "error": "device not approved"}
        resources = payload.get("resources", {})
        if not isinstance(resources, dict):
            resources = {}
        lease = self._issue_lease(device_id, resources,
                                  int(payload.get("ttl_seconds", 300) or 300))
        self.leases[lease["lease_id"]] = lease
        self.current_lease[device_id] = lease["lease_id"]
        return {"lease": lease}

    def _evidence_lease(self, device_id: str, task_id: str,
                        lease_id: str, token):
        """验证证据提交者仍持有任务/设备的当前租约。"""
        expected = (self.task_leases.get(task_id) if task_id
                    else self.current_lease.get(device_id))
        if expected is None:
            return None, "unknown task" if task_id else "no lease"
        if lease_id != expected:
            return None, "stale fencing token"
        lease = self.leases.get(lease_id)
        if lease is None or lease.get("device_id") != device_id:
            return None, "no lease"
        if token != lease["fencing_token"]:
            return None, "stale fencing token"
        if lease_expired(lease, clock_id=self.clock_id):
            return None, "lease expired"
        return lease, ""

    def _record_evidence_usage(self, device_id: str, task_id: str,
                               lease_id: str, evidence: dict) -> dict:
        """将一次证据的观察资源写入设备账本（按尝试幂等）。"""
        attempt_id = str(evidence.get("attempt_id", "") or lease_id)
        key = f"{device_id}:{task_id or '-'}:{attempt_id}"
        return self.usage_ledger.record(
            device_id, evidence_usage(evidence), task_id=task_id,
            attempt_id=attempt_id, idempotency_key=key)

    def _store_task_evidence(self, device_id: str, task_id: str,
                             lease_id: str, task: dict, evidence: dict) -> dict:
        """校验、幂等保存任务证据并更新资源账本。"""
        gpu_error = self._gpu_evidence_assignment_error(task, evidence)
        legacy = (isinstance(evidence, dict)
                  and set(evidence) <= {"result"}
                  and not evidence.get("attempt_id"))
        denial = (isinstance(evidence, dict)
                  and evidence.get("error") == "capability grant denied")
        probe = self.devices.get(device_id, {}).get("probe", {}) or {}
        workspace_error = workspace_evidence_error(task.get("workspace"),
                                                    evidence)
        error = gpu_error or workspace_error or validate_execution_evidence(
            evidence, require_digest=not denial and not legacy,
            expected_environment_digest=str(probe.get("environment_digest", "")))
        if error:
            return {"accepted": False, "reason": error}
        digest = evidence_digest(evidence)
        replay = _terminal_evidence_replay(task, digest, task_id)
        if replay is not None:
            return replay
        if task.get("status") in ("cancelled", "lost"):
            return {"accepted": False, "reason": f"task is {task['status']}"}
        cancelled = evidence.get("cancelled") is True
        if cancelled and not task.get("cancel_requested"):
            return {"accepted": False,
                    "reason": "cancelled evidence was not requested"}
        try:
            usage = self._record_evidence_usage(
                device_id, task_id, lease_id, evidence)
        except UsageLedgerError as exc:
            return {"accepted": False, "reason": f"usage ledger: {exc}"}
        network_error = self._network_budget_error(task, evidence)
        self._finish_task_evidence(task, evidence, digest, legacy, usage,
                                   network_error, cancelled)
        lifecycle_error = self._sync_lifecycle_after_task(device_id)
        if lifecycle_error:
            return {"accepted": False, "reason": lifecycle_error}
        if state_error := self._persist_task_result(task):
            return {"accepted": False, "reason": state_error}
        self._record_event(device_id, "evidence", "task", task_id,
                           attempt=lease_id)
        return {"accepted": True, "task_id": task_id,
                "usage_ledger_id": usage["ledger_id"],
                "budget_exceeded": bool(network_error)}

    @staticmethod
    def _gpu_evidence_assignment_error(task: dict, evidence: dict) -> str:
        resources = task.get("resources") or {}
        count = resources.get("gpu_count", 0) if isinstance(resources, dict) else 0
        if not count:
            return ("CPU task evidence must not contain GPU assignment"
                    if isinstance(evidence, dict)
                    and evidence.get("gpu_assignment") is not None else "")
        if isinstance(count, bool) or not isinstance(count, int):
            return "task GPU resource count is invalid"
        memory = resources.get("gpu_memory_bytes", 0)
        expected_raw = task.get("gpu_assignment")
        actual_raw = (evidence.get("gpu_assignment")
                      if isinstance(evidence, dict) else None)
        try:
            expected = normalize_gpu_assignment(expected_raw, count, memory)
            actual = normalize_gpu_assignment(actual_raw, count, memory)
        except ValueError:
            return "GPU evidence assignment is missing or invalid"
        return ("GPU evidence assignment does not match task"
                if actual != expected else "")

    @task_locked
    def handle_evidence(self, payload: dict) -> dict:
        """证据提交门禁：仅持有最新 fencing token 的租约可提交；任务证据
        按 task_id 幂等（重复提交接受但不重复计数，AADM-D §18）。"""
        device_id = str(payload.get("device_id", ""))
        lease_id = str(payload.get("lease_id", ""))
        token = payload.get("fencing_token")
        task_id = str(payload.get("task_id", ""))
        task = self.tasks.get(task_id) if task_id else None
        if task_id and task is None:
            return {"accepted": False, "reason": "unknown task"}
        _, lease_error = self._evidence_lease(
            device_id, task_id, lease_id, token)
        if lease_error:
            return {"accepted": False, "reason": lease_error}
        evidence = payload.get("evidence", {}) or {}
        if task is not None:
            return self._store_task_evidence(
                device_id, task_id, lease_id, task, evidence)
        self.devices.setdefault(device_id, {}).setdefault(
            "evidence_count", 0)
        self.devices[device_id]["evidence_count"] += 1
        try:
            usage = self._record_evidence_usage(
                device_id, "", lease_id, evidence)
        except UsageLedgerError as exc:
            return {"accepted": False, "reason": f"usage ledger: {exc}"}
        return {"accepted": True, "attempt_id": payload.get("attempt_id", ""),
                "usage_ledger_id": usage["ledger_id"]}

    def handle_reconcile(self, payload: dict) -> dict:
        """对账：服务端视图（当前租约/epoch）返回给 Runner 比对。"""
        device_id = str(payload.get("device_id", ""))
        lease_id = self.current_lease.get(device_id)
        lease = self.leases.get(lease_id) if lease_id else None
        return {"device_id": device_id,
                "epoch": self.epochs.get(device_id, 0),
                "lease": lease,
                "server_time": time.time()}

    def handle_usage(self, payload: dict) -> dict:
        """认证后查询本设备或指定任务的资源账本。"""
        device_id = str(payload.get("device_id", ""))
        task_id = str(payload.get("task_id", ""))
        return {"usage": self.usage_view(device_id=device_id,
                                         task_id=task_id)}

    def handle_lifecycle(self, payload: dict) -> dict:
        """认证后查询本设备的当前生命周期与转移审计。"""
        device_id = str(payload.get("device_id", ""))
        try:
            limit = max(1, min(int(payload.get("limit", 50)), 200))
        except (TypeError, ValueError):
            limit = 50
        return {"lifecycle": {
            "device_id": device_id,
            "state": self.lifecycle.current(device_id),
            "events": self.lifecycle.recent(limit, device_id),
        }}

    # -- D4: 任务注册表 ------------------------------------------------------

    def _reschedule_stale(self) -> None:
        """心跳超时/从未心跳的设备：stateless/restartable 任务重新排队
        （供其他设备认领），pinned/checkpointable 标记 lost。"""
        now = time.time()
        for device_id, device in list(self.devices.items()):
            last = device.get("last_seen", 0)
            if last and (now - last) <= HEARTBEAT_STALE_SECONDS:
                continue
            if self.lifecycle.current(device_id) not in ("retired", "stale"):
                self._transition_device(device_id, "stale",
                                        "heartbeat timeout")
            for task in self.tasks.values():
                if (task.get("device_id") == device_id
                        and task.get("status") in ("queued", "leased",
                                                    "running")):
                    old_lease = str(task.get("lease_id", ""))
                    self._mark_task_attempt(
                        task, old_lease, "lost", "device heartbeat stale")
                    self._record_event(device_id, "lost", "attempt",
                                       task.get("task_id", ""), old_lease)
                    self._release_task_quota(task)
                    if task.get("mobility") in ("stateless", "restartable"):
                        task["status"] = "queued"
                        task["device_id"] = None
                        task["lease_id"] = ""
                        self.task_leases[task["task_id"]] = ""
                        task["rescheduled_at"] = time.time()
                    else:
                        task["status"] = "lost"
                        task["lost_at"] = time.time()
                    self._persist_task(task)

    def _store_checkpoint(self, task_id: str, payload: dict) -> str:
        """检查点存取（有界）；返回错误串（空 = 成功）。"""
        checkpoint = payload.get("checkpoint_blob", "")
        if not (isinstance(checkpoint, str) and checkpoint):
            return ""
        if len(checkpoint) > CHECKPOINT_MAX_B64:
            return "checkpoint too large"
        self.checkpoints[task_id] = {
            "blob_b64": checkpoint, "size": len(checkpoint),
            "digest": sha256(checkpoint), "updated_at": time.time(),
        }
        return self._persist_checkpoint(task_id)

    def _validated_task_request(self, device_id: str,
                                payload: dict) -> tuple:
        argv = payload.get("argv")
        if (not isinstance(argv, list) or not argv
                or not all(isinstance(item, str) for item in argv)):
            return None, "argv must be a string list"
        effect_class = str(payload.get("effect_class", "read_only"))
        data_class = str(payload.get("data_class", "public") or "public")
        data_class = data_class.strip().lower()
        policy_error = self._task_policy_error(device_id, effect_class,
                                               data_class)
        if policy_error:
            return None, policy_error
        network_budget, budget_error = parse_network_budget(payload)
        if budget_error:
            return None, budget_error
        checkpoint_remote = payload.get("checkpoint_remote", "")
        checkpoint_error = checkpoint_remote_error(checkpoint_remote)
        if checkpoint_error:
            return None, checkpoint_error
        mobility = str(payload.get("mobility", "stateless"))
        if checkpoint_remote and mobility != "checkpointable":
            return None, "checkpoint path requires checkpointable mobility"
        return {"argv": argv, "effect_class": effect_class,
                "data_class": data_class,
                "cwd": str(payload.get("cwd", "")),
                "network_budget_bytes": network_budget,
                "checkpoint_remote": str(checkpoint_remote or "")}, ""

    def _task_cost(self, device_id: str, payload: dict) -> tuple:
        cost = float(payload.get("estimated_cost", 0) or 0)
        cost = max(0.0, min(cost, 1000.0))
        spent = self.device_budget_spent.get(device_id, 0.0)
        if self.device_budget_cap > 0 and spent + cost > self.device_budget_cap:
            return cost, spent, (f"device budget exhausted (spent {spent:.1f} + "
                                f"{cost:.1f} > cap {self.device_budget_cap:.1f})")
        return cost, spent, ""

    def _resolve_task_target(self, actor_id: str, target_ref: str) -> str:
        """Resolve an approved target id or registered device name."""
        target_ref = str(target_ref or "").strip()
        if not target_ref or target_ref == actor_id:
            return actor_id
        if target_ref in self.devices:
            return target_ref
        for device_id, device in self.devices.items():
            if str(device.get("name", "")) == target_ref:
                return device_id
        return ""

    def _reserve_device_for_task(self, device_id: str) -> str:
        """任务准入只允许 ready/reserved/busy 设备。"""
        state = self.lifecycle.current(device_id)
        if state in ("stale", "offline", "enrolled"):
            try:
                self.lifecycle.transition_path(
                    device_id, ("probing", "ready", "reserved"),
                    reason="authenticated task submission")
            except LifecycleError as exc:
                log.error("device lifecycle transition rejected: %s", exc)
                return str(exc)
            return ""
        if state == "probing":
            try:
                self.lifecycle.transition_path(
                    device_id, ("ready", "reserved"),
                    reason="authenticated task submission")
            except LifecycleError as exc:
                log.error("device lifecycle transition rejected: %s", exc)
                return str(exc)
            return ""
        if state == "ready":
            return self._transition_device(device_id, "reserved",
                                           "task lease reserved")
        if state in ("reserved", "busy"):
            return ""
        return f"device lifecycle state {state} is not schedulable"

    def _mark_device_busy(self, device_id: str) -> str:
        """认领任务；cordon/drain 不解除 operator 状态。"""
        state = self.lifecycle.current(device_id)
        if state in ("cordoned", "draining"):
            return ""
        if state in ("reserved", "ready"):
            return self._transition_device(device_id, "busy",
                                           "task claimed")
        if state == "busy":
            return ""
        return f"device lifecycle state {state} cannot claim task"

    def _prepare_device_poll(self, device_id: str) -> str:
        """已认证轮询也证明 Runner 存活，恢复可调度生命周期。"""
        state = self.lifecycle.current(device_id)
        if state in ("offline", "stale", "enrolled"):
            try:
                self.lifecycle.transition_path(
                    device_id, ("probing", "ready"),
                    reason="authenticated task poll")
            except LifecycleError as exc:
                log.error("device lifecycle transition rejected: %s", exc)
                return str(exc)
            return ""
        if state == "probing":
            return self._transition_device(device_id, "ready",
                                           "authenticated task poll")
        if state in ("ready", "reserved", "busy", "cordoned", "draining"):
            return ""
        return f"device lifecycle state {state} cannot poll tasks"

    def _sync_lifecycle_after_task(self, device_id: str) -> str:
        """任务结束后回到 ready，或保留 reserved 等待同设备队列。"""
        state = self.lifecycle.current(device_id)
        if state not in ("reserved", "busy"):
            return ""
        active = any(task.get("device_id") == device_id
                     and task.get("status") in ("queued", "leased", "running")
                     for task in self.tasks.values())
        target = "reserved" if active else "ready"
        if target == state:
            return ""
        return self._transition_device(device_id, target,
                                       "task evidence accepted")

    @task_locked
    def handle_task_status(self, payload: dict) -> dict:
        task_id = str(payload.get("task_id", ""))
        task = self.tasks.get(task_id)
        if task is None:
            return {"task": None, "error": "unknown task"}
        if error := self._task_actor_error(task, payload.get("device_id")):
            return {"task": None, "error": error}
        return {"task": self._task_view(task)}

    @task_locked
    def handle_task_migrate(self, payload: dict) -> dict:
        """任务迁移：重新排队到新设备并签发新租约（旧 fencing token
        立即失效——被迁移任务的陈旧执行结果无法提交）。"""
        task_id = str(payload.get("task_id", ""))
        new_device = str(payload.get("target_device", ""))
        task = self.tasks.get(task_id)
        if task is None:
            return {"ok": False, "error": "unknown task"}
        if error := self._task_actor_error(task, payload.get("device_id")):
            return {"ok": False, "error": error}
        if task.get("mobility") == "pinned":
            return {"ok": False, "error": "pinned task cannot migrate"}
        if task.get("status") in ("done", "failed", "cancelled"):
            return {"ok": False, "error": f"task is {task.get('status')}"}
        old_device = str(task.get("device_id", ""))
        if new_device not in self._approval_ids():
            return {"ok": False, "error": "target device not approved"}
        if new_device in self.cordoned:
            return {"ok": False, "error": "target device cordoned"}
        effect_class = str(task.get("effect_class", "read_only"))
        data_class = str(task.get("data_class", "public"))
        policy_error = self._task_policy_error(new_device, effect_class,
                                               data_class)
        if policy_error:
            return {"ok": False, "error": policy_error}
        lifecycle_error = self._reserve_device_for_task(new_device)
        if lifecycle_error:
            return {"ok": False, "error": lifecycle_error}
        if quota_error := self._transfer_task_quota(task, new_device):
            self._sync_lifecycle_after_task(new_device)
            return {"ok": False, "error": quota_error}
        old_lease = str(task.get("lease_id", ""))
        self._mark_task_attempt(task, old_lease, "lost", "task migrated")
        lease = self._issue_lease(
            new_device, {}, 300,
            grant_context=self._grant_context(
                new_device, task_id, effect_class, data_class,
                str(task.get("cwd", ""))))
        self.leases[lease["lease_id"]] = lease
        self.task_leases[task_id] = lease["lease_id"]
        task.update({"device_id": new_device, "lease_id": lease["lease_id"],
                     "status": "queued", "migrated_at": time.time()})
        self._append_task_attempt(task, lease, "reserved")
        self._persist_task(task)
        if old_device and old_device != new_device:
            self._sync_lifecycle_after_task(old_device)
        self._record_event(str(payload.get("device_id", "")), "migrate",
                           "task", task_id)
        return {"ok": True, "task_id": task_id, "lease": lease}

    @task_locked
    def handle_halt(self, payload: dict) -> dict:
        """Kill Switch（AADM-G §1 可信内核）：取消全部排队/已认领任务，
        并 cordon 所有设备——异常时一键冻结，防止失控扩散。"""
        cancelled = 0
        for task in self.tasks.values():
            if task.get("status") in ("queued", "leased"):
                self._cancel_task_record(task, "halt", "kill switch")
                cancelled += 1
        now = time.time()
        for device_id in list(self.secrets):
            self._transition_device(device_id, "draining", "kill switch")
            self.cordoned[device_id] = {"drain": True, "since": now,
                                        "reason": "halt"}
        self._record_event(str(payload.get("device_id", "")), "halt",
                           "all_devices")
        return {"ok": True, "cancelled_tasks": cancelled,
                "cordoned_devices": len(self.cordoned),
                "message": "Kill Switch 已触发：全部新任务冻结，运行中任务"
                           "完成后不再认领"}

    def handle_cordon(self, payload: dict) -> dict:
        """Cordon/Drain（§31）：不再接收新任务；drain 同时停止新任务认领。"""
        device_id = str(payload.get("device_id", ""))
        if device_id not in self.secrets:
            return {"ok": False, "error": "unknown device"}
        if payload.get("undo"):
            lifecycle_error = self._transition_device(
                device_id, "ready", "operator cordon undo")
            if lifecycle_error:
                return {"ok": False, "error": lifecycle_error}
            self.cordoned.pop(device_id, None)
        else:
            target = "draining" if payload.get("drain") else "cordoned"
            lifecycle_error = self._transition_device(
                device_id, target, "operator drain" if payload.get("drain")
                else "operator cordon")
            if lifecycle_error:
                return {"ok": False, "error": lifecycle_error}
            self.cordoned[device_id] = {"drain": bool(payload.get("drain")),
                                        "since": time.time()}
        return {"ok": True, "cordoned": device_id in self.cordoned,
                "drain": device_id in self.cordoned
                and self.cordoned[device_id].get("drain")}

    def status_view(self) -> list:
        """设备在线视图（供 GET /status；心跳超时标 stale）。"""
        now = time.time()
        view = []
        for device_id, device in sorted(self.devices.items()):
            last = device.get("last_seen", 0)
            online = last > 0 and (now - last) <= HEARTBEAT_STALE_SECONDS
            probe = device.get("probe", {}) or {}
            item = {
                "device_id": device_id, "name": device.get("name", ""),
                "lifecycle_state": self.lifecycle.current(device_id),
                "online": online, "heartbeats": device.get("heartbeats", 0),
                "evidence_count": device.get("evidence_count", 0),
                "last_seen": last,
                "cordoned": device_id in self.cordoned,
                "draining": bool((self.cordoned.get(device_id) or {}).get("drain")),
                "static_capabilities": probe.get("static_capabilities", {}),
                "runtimes": probe.get("runtimes", {}),
                "capability_valid_until": probe.get("capability_valid_until", ""),
                "protocol_version": device.get("protocol_version",
                                                probe.get("protocol_version", "")),
                "runner_capabilities": list(device.get(
                    "runner_capabilities", [])),
                "tenant": device.get("tenant", ""),
                "region": device.get("region", ""),
                "attestation": device.get("attestation", ""),
                "trust": device.get("trust", {}),
                "trust_zone": self._policy_for(device_id)["trust_zone"],
                "trust_level": self._policy_for(device_id)["trust_level"],
                "allowed_data_classes": list(self._policy_for(device_id)[
                    "allowed_data_classes"]),
                "certificate_fingerprint": device.get(
                    "certificate_fingerprint", ""),
                "usage": self.usage_ledger.totals(device_id=device_id),
            }
            view.append(item)
        return view

    def usage_view(self, device_id: str = "", task_id: str = "") -> dict:
        """查询设备/任务资源账本（结果只含有界数值与记录数）。"""
        return self.usage_ledger.summary(device_id=device_id,
                                         task_id=task_id)
_read_lines_bounded = read_lines_bounded
def start_server(port: int = 0, state_dir: str = "", auto_approve: bool = False,
                 host: str = "127.0.0.1", budget_cap: float = 0.0,
                 tenant_quotas: Optional[dict] = None,
                 device_quotas: Optional[dict] = None,
                 device_policies: Optional[dict] = None,
                 require_client_cert: bool = False,
                 tls_certfile: str = "", tls_keyfile: str = "",
                 tls_cafile: str = ""):
    """启动控制平面（实现见 pbatch/fabric_server_http.py；re-export 兼容）。"""
    from .fabric_server_http import start_server as _start
    return _start(port, state_dir, auto_approve, host, budget_cap,
                  tenant_quotas=tenant_quotas, device_quotas=device_quotas,
                  device_policies=device_policies,
                  require_client_cert=require_client_cert,
                  tls_certfile=tls_certfile, tls_keyfile=tls_keyfile,
                  tls_cafile=tls_cafile)
