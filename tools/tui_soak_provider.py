"""Deterministic Pi RPC fixture for the standalone TUI soak, never a live model."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def record(kind: str, **fields) -> None:
    path = Path(os.environ["TUI_SOAK_PROVIDER_LOG"])
    value = {"kind": kind, "pid": os.getpid(), "ppid": os.getppid(), **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value) + "\n")
        handle.flush()


def emit(value: dict) -> None:
    print(json.dumps(value), flush=True)


def response(command: dict, data: dict | None = None) -> None:
    emit({"id": command["id"], "type": "response", "command": command["type"],
          "success": True, "data": data or {}})


def assistant(text: str, stop: str = "stop") -> dict:
    value = {"role": "assistant", "content": [{"type": "text", "text": text}],
             "stopReason": stop, "usage": {"input": 1, "output": 1, "totalTokens": 2}}
    if stop == "error":
        value["errorMessage"] = "soak injected provider failure"
    return value


def prompt(command: dict) -> None:
    message = command.get("message", "")
    record("prompt", message=message)
    response(command)
    emit({"type": "agent_start"})
    if "soak:timeout:" in message or "soak:cancel:" in message:
        return  # Read controls, but never claim a terminal result for these turns.
    await_ui_busy(message)
    failed = "soak:failure:" in message
    final = assistant("soak reply: " + message, "error" if failed else "stop")
    emit({"type": "message_start", "message": assistant("")})
    emit({"type": "message_end", "message": final})
    emit({"type": "agent_end", "messages": [final], "willRetry": False})
    emit({"type": "agent_settled"})


def await_ui_busy(message: str) -> None:
    """Release a fast reply only after the harness observes a real busy frame."""
    path = Path(os.environ["TUI_SOAK_RELEASE_FILE"])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with path.open("rb") as handle:
                token = json.loads(handle.read(4096))
        except (OSError, ValueError):
            token = None
        if token == {"message": message, "pid": os.getpid()}:
            record("released", message=message)
            return
        time.sleep(0.01)
    raise RuntimeError("soak harness never observed a busy composer for this turn")


def serve() -> None:
    session = 1
    record("started")
    for line in sys.stdin:
        command = json.loads(line)
        kind = command["type"]
        if kind == "prompt":
            prompt(command)
            continue
        record("control", command=kind)
        if kind == "new_session":
            session += 1
        data = {}
        if kind == "get_state":
            data = {"sessionFile": f"/soak/{os.getpid()}/{session}.jsonl",
                    "sessionId": f"soak-{os.getpid()}-{session}",
                    "isStreaming": False, "messageCount": 0, "pendingMessageCount": 0}
        if kind == "get_available_models":
            data = {"models": []}
        if kind == "set_session_name":
            emit({"type": "session_info_changed", "sessionName": command.get("name", "")})
        response(command, data)


if __name__ == "__main__":
    if "--version" in sys.argv:
        print("0.87.1")
    else:
        serve()
