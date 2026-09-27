"""Deterministic standalone-smoke provider; never calls a model or user tool."""

import json
import os
from pathlib import Path
import sys
import threading
import time


def write_event(root, role, kind, **fields):
    path = root / f"{role}.{kind}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": os.getpid(), "parent_pid": os.getppid(),
                                     "role": role, **fields}), encoding="utf-8")
    temporary.replace(path)


def hold(root, role):
    deadline = time.monotonic() + float(os.environ["BINARY_SMOKE_HOLD_SECONDS"])
    pause = threading.Event()
    while time.monotonic() < deadline:
        try:
            control = json.loads((root / f"{role}.control.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            control = {}
        if control.get("action") == "release":
            return
        if control.get("action") == "ping":
            write_event(root, role, "pong", token=control["token"])
        pause.wait(0.02)
    raise TimeoutError("smoke fixture did not receive its release handshake")


def main():
    role = os.environ.get("BINARY_SMOKE_LOCK_ROLE", "")
    if role:
        root = Path(os.environ["BINARY_SMOKE_LOCK_ROOT"])
        write_event(root, role, "ready")
        try:
            if role == "holder":
                hold(root, role)
        finally:
            write_event(root, role, "done")
    print(json.dumps({"ok": "smoke-invalid" not in " ".join(sys.argv[1:])}))


if __name__ == "__main__":
    main()
