"""Control plane HTTP layer (split from fabric_server.py for line budget).

JSON RPC 处理器（除 /register 的 TOFU 首注册外全部验签）与服务器启动。
ControlPlane 状态与业务在 pbatch/fabric_server.py。
"""

from __future__ import annotations

import http.server
import hashlib
import json
import ssl
from typing import Optional

from .fabric_rpc import BODY_MAX_BYTES, verify_request


class _Handler(http.server.BaseHTTPRequestHandler):
    """JSON RPC 处理器：验签（除 /register 的 TOFU 首注册）→ 路由。"""

    server: "http.server.ThreadingHTTPServer"

    def log_message(self, fmt, *args):  # 安静：运营日志由调用方负责
        pass

    def _reply(self, code: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _peer_certificate_fingerprint(self) -> str:
        """读取 TLS peer 证书指纹；无 TLS/无证书返回空。"""
        try:
            certificate = self.connection.getpeercert(binary_form=True)
        except (AttributeError, OSError):
            return ""
        return hashlib.sha256(certificate).hexdigest() if certificate else ""

    def _read_request(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._reply(400, {"error": "bad content length"})
            return None
        if length <= 0 or length > BODY_MAX_BYTES:
            self._reply(413, {"error": "body too large"})
            return None
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw)
        except ValueError:
            self._reply(400, {"error": "bad json"})
            return None
        payload = request.get("payload", {}) if isinstance(request, dict) else {}
        if not isinstance(payload, dict):
            self._reply(400, {"error": "payload must be an object"})
            return None
        return request, dict(payload)

    def _auth_error(self, control, request: dict, path: str,
                    device_id: str, certificate: str) -> tuple:
        if control.require_client_cert and not certificate:
            return 401, "client certificate required"
        expected = control.devices.get(device_id, {}).get(
            "certificate_fingerprint", "")
        if (control.require_client_cert and path != "/register"
                and certificate != expected):
            return 401, "certificate identity mismatch"
        secret = control.secret_for(device_id)
        if path != "/register":
            if secret is None:
                return 404, "unknown device"
            reason = verify_request(secret, "POST", path, request,
                                    replay_cache=control.replay)
            if reason is not None:
                return 401, f"auth failed: {reason}"
        return 0, ""

    def do_POST(self):  # noqa: N802（http.server 协议方法名）
        parsed = self._read_request()
        if parsed is None:
            return
        request, payload = parsed
        control = self.server.control
        device_id = str(payload.get("device_id", ""))
        certificate = self._peer_certificate_fingerprint()
        code, error = self._auth_error(control, request, self.path,
                                       device_id, certificate)
        if code:
            self._reply(code, {"error": error})
            return
        payload["_client_cert_fingerprint"] = certificate
        handler = {
            "/register": control.handle_register,
            "/heartbeat": control.handle_heartbeat,
            "/lease": control.handle_lease,
            "/evidence": control.handle_evidence,
            "/usage": control.handle_usage,
            "/lifecycle": control.handle_lifecycle,
            "/reconcile": control.handle_reconcile,
            "/task/submit": control.handle_task_submit,
            "/task/gang-submit": control.handle_task_gang_submit,
            "/task/poll": control.handle_task_poll,
            "/task/start": control.handle_task_start,
            "/task/status": control.handle_task_status,
            "/task/cancel": control.handle_task_cancel,
            "/task/migrate": control.handle_task_migrate,
            "/cordon": control.handle_cordon,
            "/halt": control.handle_halt,
            "/desired/reconcile": control.handle_desired_reconcile,
            "/revoke": control.handle_revoke,
            "/rotate-certificate": control.handle_rotate_certificate,
            "/checkpoint/download": control.handle_checkpoint_download,
            "/checkpoint/upload": control.handle_checkpoint_upload,
        }.get(self.path)
        if handler is None:
            self._reply(404, {"error": "unknown path"})
            return
        self._reply(200, handler(payload))

    def do_GET(self):  # noqa: N802
        if self.path == "/status":
            if (self.server.control.require_client_cert
                    and not self._peer_certificate_fingerprint()):
                self._reply(401, {"error": "client certificate required"})
                return
            self._reply(200, {"devices": self.server.control.status_view()})
        else:
            self._reply(404, {"error": "unknown path"})


def _enable_tls(httpd, certfile: str, keyfile: str, cafile: str,
                require_client_cert: bool) -> None:
    """启用服务端 TLS；mTLS 没有 CA 时拒绝启动。"""
    if not certfile or not keyfile:
        raise ValueError("TLS requires both server cert and key")
    if require_client_cert and not cafile:
        raise ValueError("mTLS requires a client CA file")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    if cafile:
        context.load_verify_locations(cafile=cafile)
        context.verify_mode = (ssl.CERT_REQUIRED if require_client_cert
                               else ssl.CERT_OPTIONAL)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)


def start_server(port: int = 0, state_dir: str = "",
                 auto_approve: bool = False, host: str = "127.0.0.1",
                 budget_cap: float = 0.0,
                 tenant_quotas: Optional[dict] = None,
                 device_quotas: Optional[dict] = None,
                 device_policies: Optional[dict] = None,
                 require_client_cert: bool = False,
                 tls_certfile: str = "", tls_keyfile: str = "",
                 tls_cafile: str = ""):
    """启动控制平面（测试/嵌入用）。返回 (httpd, actual_port)。"""
    from .fabric_server import ControlPlane
    control = ControlPlane(state_dir,
                           auto_approve=auto_approve, budget_cap=budget_cap,
                           tenant_quotas=tenant_quotas,
                           device_quotas=device_quotas,
                           device_policies=device_policies,
                           require_client_cert=require_client_cert)
    httpd = http.server.ThreadingHTTPServer((host, port), _Handler)
    tls_requested = bool(tls_certfile or tls_keyfile or tls_cafile
                         or require_client_cert)
    try:
        if tls_requested:
            _enable_tls(httpd, tls_certfile, tls_keyfile, tls_cafile,
                        require_client_cert)
    except (OSError, ssl.SSLError, ValueError):
        httpd.server_close()
        raise
    httpd.control = control
    httpd.tls_enabled = tls_requested
    return httpd, httpd.server_address[1]
