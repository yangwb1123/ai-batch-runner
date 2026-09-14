"""Create explicit loopback development credentials without printing secrets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path

from .hub_auth import GATEWAY_SCOPE, USER_SCOPES


def _credential(directory: Path, name: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    path = directory / f"{name}.token"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags, 0o600), "w") as stream:
        stream.write(token + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return str(path), hashlib.sha256(token.encode()).hexdigest()


def create_development_config(directory, instances=("alpha", "beta")) -> dict:
    """Create a new private directory; existing credentials are never replaced."""
    instances = tuple(instances)
    if (not 1 <= len(instances) <= 32 or len(set(instances)) != len(instances)
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
                   for name in instances)):
        raise ValueError("provide 1..32 distinct safe instance names")
    target = Path(directory).expanduser()
    if target.is_symlink() or target.exists():
        raise ValueError("development directory must not already exist")
    target.mkdir(mode=0o700, parents=True)
    user_path, digest = _credential(target, "user")
    grants = [{"tenant_id": "development", "subject": "developer", "client_id": "fleet-cli",
               "kind": "user", "instances": list(instances), "scopes": sorted(USER_SCOPES),
               "token_sha256": digest}]
    paths = {}
    for instance in instances:
        paths[instance], digest = _credential(target, "gateway-" + instance)
        grants.append({"tenant_id": "development", "subject": "gateway-" + instance,
                       "client_id": "gateway-" + instance, "kind": "gateway", "instances": [instance],
                       "scopes": [GATEWAY_SCOPE], "token_sha256": digest})
    filename = target / "hub.json"
    with os.fdopen(os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump({"auth": {"mode": "development", "grants": grants}}, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return {"config": str(filename), "user_token_file": user_path, "gateway_token_files": paths}
