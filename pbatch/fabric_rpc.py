"""Device-Aware Execution Fabric — D3: control-plane RPC 通道.

设备 Runner 与控制平面的请求/响应通道：注册、心跳、租约、证据、对账。
纯 stdlib（http.client + hmac），签名请求 = HMAC-SHA256 over
`method|path|ts|nonce` + payload 序列化字节；服务端校验时间窗（±300s）
与 nonce 防重放。

安全说明（AADM-D §20/§21）：
- /register 是 TOFU（trust-on-first-use）：设备自生成 secret 并首次
  声称身份；此后所有请求必须携带正确签名（fail closed）
- 默认仍绑定 127.0.0.1；`serve --cert-file --key-file --ca-file
  --require-client-cert` 可启用服务端 TLS/mTLS，证书指纹会绑定到 device_id
- HTTPS 客户端必须提供可验证 CA；mTLS 客户端还必须提供证书/私钥，缺参或
  指纹不匹配均 fail closed
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import ssl
import time
import uuid
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit

# 请求时间窗（秒）与 nonce 防重放缓存上限（有界，超限清空）。
SIGN_TIMESTAMP_WINDOW = 300
NONCE_CACHE_MAX = 10000
BODY_MAX_BYTES = 1024 * 1024


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """Control-plane credentials and signatures never follow redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_hub_transport(url: str) -> None:
    """Hub callers may use HTTPS or a literal numeric loopback URL only."""
    parsed = None
    try:
        parsed = urlsplit(str(url))
        host = parsed.hostname or ""
        if (parsed.scheme == "https" and host and not parsed.username
                and not parsed.password):
            return
        loopback = ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        loopback = False
    if (parsed is not None and parsed.scheme == "http" and loopback
            and not parsed.username
            and not parsed.password):
        return
    raise ValueError("Hub Fabric connections require HTTPS or literal loopback")


def build_tls_context(ca_file: str = "", cert_file: str = "",
                      key_file: str = "") -> ssl.SSLContext:
    """构造客户端 TLS context；证书链参数不完整时直接拒绝。"""
    if bool(cert_file) != bool(key_file):
        raise ValueError("client cert and key must be provided together")
    context = ssl.create_default_context(cafile=ca_file or None)
    if cert_file:
        context.load_cert_chain(certfile=cert_file, keyfile=key_file)
    return context


def tls_context_for_url(url: str, ca_file: str = "", cert_file: str = "",
                        key_file: str = "") -> Optional[ssl.SSLContext]:
    """HTTP 保持旧行为；HTTPS 或显式 TLS 参数走证书校验。"""
    https = str(url).lower().startswith("https://")
    if not https and (ca_file or cert_file or key_file):
        raise ValueError("TLS client files require an https control URL")
    if not https:
        return None
    return build_tls_context(ca_file, cert_file, key_file)


def _sign(secret: str, method: str, path: str, body: bytes,
          ts: int, nonce: str) -> str:
    """签名输入 = `method|path|ts|nonce` 字节 + payload 原始字节。"""
    message = f"{method}|{path}|{ts}|{nonce}".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), message,
                    hashlib.sha256).hexdigest()


def serialize_payload(payload: dict) -> bytes:
    """payload 的确定性序列化（客户端签名 / 服务端验签共用）。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def build_request(secret: str, method: str, path: str,
                  payload: dict) -> dict:
    """构造一个签名请求体：{ts, nonce, sig, payload}。"""
    ts = int(time.time())
    nonce = uuid.uuid4().hex
    body = serialize_payload(payload)
    return {"ts": ts, "nonce": nonce,
            "sig": _sign(secret, method, path, body, ts, nonce),
            "payload": payload}


def verify_request(secret: str, method: str, path: str, request: dict,
                   replay_cache: Optional[dict] = None,
                   now: Optional[int] = None) -> Optional[str]:
    """校验签名请求。返回 None = 通过；否则返回拒绝原因（fail closed）。"""
    ts = request.get("ts")
    nonce = request.get("nonce")
    sig = request.get("sig")
    payload = request.get("payload")
    if (isinstance(ts, bool) or not isinstance(ts, int)
            or not isinstance(nonce, str) or not isinstance(sig, str)
            or not isinstance(payload, dict)):
        return "malformed request"
    current = int(time.time()) if now is None else now
    if abs(current - ts) > SIGN_TIMESTAMP_WINDOW:
        return "stale timestamp"
    if replay_cache is not None:
        key = f"{nonce}"
        if key in replay_cache:
            return "replay"
        if len(replay_cache) >= NONCE_CACHE_MAX:
            replay_cache.clear()  # 有界：超限清空（等价于冷启动）
        replay_cache[key] = ts
    expected = _sign(secret, method, path, serialize_payload(payload),
                     ts, nonce)
    if not hmac.compare_digest(expected, sig):
        return "bad signature"
    return None


def rpc_post(url: str, secret: str, method: str, path: str,
             payload: dict, timeout: float = 5.0,
             ssl_context: Optional[ssl.SSLContext] = None) -> tuple:
    """发一次签名请求。返回 (status_code, response_dict)；网络错误返回
    (0, {"error": ...})，不抛异常（故障显式）。"""
    request = build_request(secret, method, path, payload)
    data = json.dumps(request).encode("utf-8")
    req = urlrequest.Request(url + path, data=data,
                             headers={"Content-Type": "application/json"},
                             method=method)
    try:
        handlers = [_NoRedirect()]
        if ssl_context is not None:
            handlers.append(urlrequest.HTTPSHandler(context=ssl_context))
        opener = urlrequest.build_opener(*handlers)
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(BODY_MAX_BYTES + 1)
            if len(body) > BODY_MAX_BYTES:
                return (0, {"error": "response too large"})
            return (resp.status, json.loads(body or b"{}"))
    except urlerror.HTTPError as exc:
        try:
            detail = json.loads(exc.read(BODY_MAX_BYTES) or b"{}")
        except (ValueError, OSError):
            detail = {"error": f"http {exc.code}"}
        return (exc.code, detail)
    except (urlerror.URLError, OSError, ValueError) as exc:
        return (0, {"error": str(exc)})


class RpcClient:
    """设备 Runner 侧客户端：注册 / 心跳 / 租约 / 证据 / 对账。"""

    def __init__(self, base_url: str, secret: str, device_id: str,
                 ssl_context: Optional[ssl.SSLContext] = None,
                 require_secure_transport: bool = False):
        if require_secure_transport:
            _validate_hub_transport(base_url)
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.device_id = device_id
        self.ssl_context = ssl_context

    def _post(self, path: str, payload: dict, timeout: float = 5.0) -> tuple:
        return rpc_post(self.base_url, self.secret, "POST", path,
                        payload, timeout=timeout,
                        ssl_context=self.ssl_context)

    def register(self, name: str, probe: Optional[dict] = None,
                 tenant: str = "", region: str = "",
                 attestation: str = "") -> dict:
        """TOFU 注册；probe 为能力快照，tenant/region/attestation 为联邦
        元数据（D6 最小件：多租户配额/跨区域/注册证明）。"""
        code, data = self._post("/register", {
            "device_id": self.device_id, "name": name,
            "secret": self.secret, "probe": probe,
            "tenant": tenant, "region": region, "attestation": attestation,
        })
        return data if code == 200 else {"approved": False, "error": data}

    def heartbeat(self, dynamic_state: dict, running_attempts: list) -> dict:
        code, data = self._post("/heartbeat", {
            "device_id": self.device_id, "dynamic_state": dynamic_state,
            "running_attempts": running_attempts,
        })
        return data if code == 200 else {"ok": False, "error": data}

    def lease(self, resources: dict, ttl_seconds: int = 300) -> dict:
        code, data = self._post("/lease", {
            "device_id": self.device_id, "resources": resources,
            "ttl_seconds": ttl_seconds,
        })
        return data if code == 200 else {"lease": None, "error": data}

    def evidence(self, attempt_id: str, lease_id: str,
                 fencing_token: int, evidence: dict,
                 task_id: str = "") -> dict:
        code, data = self._post("/evidence", {
            "device_id": self.device_id, "attempt_id": attempt_id,
            "lease_id": lease_id, "fencing_token": fencing_token,
            "evidence": evidence, "task_id": task_id,
        })
        return data if code == 200 else {
            "accepted": False, "error": data,
            "transport_error": code == 0,
        }

    def reconcile(self, running_attempts: list) -> dict:
        code, data = self._post("/reconcile", {
            "device_id": self.device_id, "running_attempts": running_attempts,
        })
        return data if code == 200 else {"error": data}

    def usage(self, task_id: str = "") -> dict:
        """查询本设备资源账本；task_id 非空时限定到单个任务。"""
        code, data = self._post("/usage", {
            "device_id": self.device_id, "task_id": task_id,
        })
        return data if code == 200 else {"usage": None, "error": data}

    def lifecycle(self, limit: int = 50) -> dict:
        """查询本设备生命周期状态与最近转移事件。"""
        code, data = self._post("/lifecycle", {
            "device_id": self.device_id, "limit": limit,
        })
        return data if code == 200 else {"lifecycle": None, "error": data}

    # -- D4: 任务调度与设备管理 --------------------------------------------

    def submit_task(self, argv: list, cwd: str = "", timeout: float = 60.0,
                    effect_class: str = "read_only",
                    mobility: str = "stateless",
                    ttl_seconds: int = 300,
                    checkpoint_blob: str = "",
                    checkpoint_remote: str = "",
                    estimated_cost: float = 0.0,
                    data_class: str = "public",
                    target_device: str = "",
                    network_budget_bytes: int = 0,
                    request_id: str = "", resources: Optional[dict] = None,
                    hub_context: Optional[dict] = None,
                    workspace: Optional[dict] = None) -> dict:
        payload = {
            "device_id": self.device_id, "argv": argv, "cwd": cwd,
            "timeout": timeout, "effect_class": effect_class,
            "data_class": data_class,
            "mobility": mobility, "ttl_seconds": ttl_seconds,
            "checkpoint_blob": checkpoint_blob,
            "checkpoint_remote": checkpoint_remote,
            "estimated_cost": estimated_cost,
            "network_budget_bytes": network_budget_bytes,
            "target_device": target_device,
        }
        if request_id:
            payload["request_id"] = request_id
        if resources is not None:
            payload["resources"] = resources
        if hub_context is not None:
            payload["hub_context"] = hub_context
        if workspace is not None:
            payload["workspace"] = workspace
        code, data = self._post("/task/submit", payload)
        if code == 200:
            return data
        return {"task": None, "error": data, "status_code": code,
                "transport_error": code == 0}

    def submit_gang(self, targets: list, argv: list, cwd: str = "",
                    timeout: float = 60.0, effect_class: str = "read_only",
                    mobility: str = "stateless", checkpoint_blob: str = "",
                    checkpoint_remote: str = "", estimated_cost: float = 0.0,
                    data_class: str = "public",
                    network_budget_bytes: int = 0) -> dict:
        code, data = self._post("/task/gang-submit", {
            "device_id": self.device_id, "target_devices": targets,
            "argv": argv, "cwd": cwd, "timeout": timeout,
            "effect_class": effect_class, "data_class": data_class,
            "mobility": mobility, "checkpoint_blob": checkpoint_blob,
            "checkpoint_remote": checkpoint_remote,
            "estimated_cost": estimated_cost,
            "network_budget_bytes": network_budget_bytes,
        })
        return data if code == 200 else {"ok": False, "error": data,
                                         "tasks": []}

    def desired_reconcile(self, services: list) -> dict:
        """Desired-State Controller：提交期望状态，服务端观察并纠偏。"""
        code, data = self._post("/desired/reconcile", {
            "device_id": self.device_id, "services": services,
        })
        return data if code == 200 else {"results": [], "error": data}

    def revoke(self, reason: str = "") -> dict:
        """设备退役：吊销本设备密钥（永久失效，需重新注册+审批）。"""
        code, data = self._post("/revoke", {
            "device_id": self.device_id, "reason": reason,
        })
        return data if code == 200 else {"ok": False, "error": data}

    def rotate_certificate(self, fingerprint: str) -> dict:
        """Update the server-side certificate binding after authenticated rotation."""
        code, data = self._post("/rotate-certificate", {
            "device_id": self.device_id,
            "new_certificate_fingerprint": fingerprint,
        })
        return data if code == 200 else {"ok": False, "error": data}

    def checkpoint_download(self, task_id: str, lease_id: str = "",
                            fencing_token=None) -> dict:
        code, data = self._post("/checkpoint/download", {
            "device_id": self.device_id, "task_id": task_id,
            "lease_id": lease_id, "fencing_token": fencing_token,
        })
        return data if code == 200 else {"has_checkpoint": False,
                                         "error": data}

    def checkpoint_upload(self, task_id: str, blob_b64: str,
                          lease_id: str = "", fencing_token=None) -> dict:
        code, data = self._post("/checkpoint/upload", {
            "device_id": self.device_id, "task_id": task_id,
            "blob_b64": blob_b64, "lease_id": lease_id,
            "fencing_token": fencing_token,
        })
        return data if code == 200 else {"ok": False, "error": data}

    def poll_tasks(self, limit: Optional[int] = None) -> dict:
        payload = {"device_id": self.device_id}
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
                raise ValueError("task poll limit must be between 1 and 200")
            payload["limit"] = limit
        code, data = self._post("/task/poll", payload)
        return data if code == 200 else {"tasks": [], "error": data}

    def inventory(self, target_device_ids: list[str]) -> dict:
        """Read a bounded, signed inventory projection for requested devices."""
        code, data = self._post("/inventory", {
            "device_id": self.device_id,
            "target_device_ids": target_device_ids,
        })
        return data if code == 200 else {"devices": [], "error": data}

    def task_start(self, task_id: str, lease: dict) -> dict:
        code, data = self._post("/task/start", {
            "device_id": self.device_id, "task_id": task_id,
            "lease_id": lease.get("lease_id", ""),
            "fencing_token": lease.get("fencing_token"),
        })
        return data if code == 200 else {"ok": False, "error": data}

    def task_status(self, task_id: str) -> dict:
        code, data = self._post("/task/status", {
            "device_id": self.device_id, "task_id": task_id,
        })
        return data if code == 200 else {"task": None, "error": data}

    def task_cancel(self, task_id: str) -> dict:
        code, data = self._post("/task/cancel", {
            "device_id": self.device_id, "task_id": task_id,
        })
        return data if code == 200 else {"ok": False, "error": data}

    def task_migrate(self, task_id: str, new_device: str) -> dict:
        code, data = self._post("/task/migrate", {
            "device_id": self.device_id, "task_id": task_id,
            "target_device": new_device,
        })
        return data if code == 200 else {"ok": False, "error": data}

    def halt(self) -> dict:
        """Kill Switch：取消全部排队任务 + 全设备 cordon（异常冻结）。"""
        code, data = self._post("/halt", {"device_id": self.device_id})
        return data if code == 200 else {"ok": False, "error": data}

    def cordon(self, drain: bool = False, undo: bool = False) -> dict:
        code, data = self._post("/cordon", {
            "device_id": self.device_id, "drain": drain, "undo": undo,
        })
        return data if code == 200 else {"ok": False, "error": data}
