"""D3b TLS/mTLS 与设备证书指纹绑定回归。"""

from __future__ import annotations

import shutil
import subprocess
import threading
import hashlib

import pytest

from pbatch import fabric_server
from pbatch.fabric_rpc import RpcClient, build_tls_context


def _openssl(*args) -> None:
    subprocess.run(["openssl", *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _make_certificates(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl is required for the mTLS integration test")
    ca_key, ca_crt = tmp_path / "ca.key", tmp_path / "ca.crt"
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", str(ca_key), "-out", str(ca_crt), "-days", "1",
             "-subj", "/CN=pbatch-test-ca")
    ext = tmp_path / "server.ext"
    ext.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n",
                   encoding="utf-8")
    paths = {"ca": ca_crt}
    for label, subject in (("server", "/CN=localhost"),
                           ("client-a", "/CN=runner-a"),
                           ("client-b", "/CN=runner-b")):
        key, csr, crt = (tmp_path / f"{label}.key",
                         tmp_path / f"{label}.csr",
                         tmp_path / f"{label}.crt")
        _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout",
                 str(key), "-out", str(csr), "-subj", subject)
        command = ["x509", "-req", "-in", str(csr), "-CA", str(ca_crt),
                   "-CAkey", str(ca_key), "-CAcreateserial", "-out",
                   str(crt), "-days", "1", "-sha256"]
        if label == "server":
            command += ["-extfile", str(ext)]
        _openssl(*command)
        paths[label] = (crt, key)
    return paths


def test_mtls_binds_certificate_fingerprint(tmp_path):
    paths = _make_certificates(tmp_path)
    httpd, port = fabric_server.start_server(
        0, str(tmp_path / "state"), auto_approve=True,
        require_client_cert=True,
        tls_certfile=str(paths["server"][0]),
        tls_keyfile=str(paths["server"][1]), tls_cafile=str(paths["ca"]))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"https://localhost:{port}"
    try:
        context_a = build_tls_context(
            str(paths["ca"]), str(paths["client-a"][0]),
            str(paths["client-a"][1]))
        client_a = RpcClient(base, "secret-a", "rn-tls", context_a)
        assert client_a.register("node-tls")["approved"] is True
        context_b = build_tls_context(
            str(paths["ca"]), str(paths["client-b"][0]),
            str(paths["client-b"][1]))
        spoof = RpcClient(base, "secret-b", "rn-tls", context_b)
        assert spoof.register("node-tls")["approved"] is False
        no_cert = RpcClient(base, "secret-c", "rn-no-cert",
                            build_tls_context(str(paths["ca"])))
        assert no_cert.register("node-no-cert")["approved"] is False
        status = client_a._post("/heartbeat", {
            "device_id": client_a.device_id, "dynamic_state": {},
            "running_attempts": []})
        assert status[0] == 200
        cert_der = subprocess.check_output(
            ["openssl", "x509", "-in", str(paths["client-b"][0]),
             "-outform", "DER"])
        new_fingerprint = hashlib.sha256(cert_der).hexdigest()
        rotated = client_a.rotate_certificate(new_fingerprint)
        assert rotated["ok"] is True
        client_b_bound = RpcClient(base, "secret-a", "rn-tls", context_b)
        assert client_b_bound.register("node-tls")["approved"] is True
        old_cert = client_a._post("/heartbeat", {
            "device_id": client_a.device_id, "dynamic_state": {},
            "running_attempts": []})
        assert old_cert[0] == 401
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
