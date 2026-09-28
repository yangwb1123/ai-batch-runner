"""Deterministic Pi RPC fixture for restart authorization, without a model."""

import json
import os
from pathlib import Path
import sys

from tui_soak_provider import await_ui_busy


def emit(value: dict) -> None:
    print(json.dumps(value), flush=True)


def serve() -> None:
    session, number = "/fixture/live-1", 1
    for line in sys.stdin:
        command = json.loads(line)
        kind = command["type"]
        with Path("../provider-controls.jsonl").open("a") as handle:
            handle.write(json.dumps({"pid": os.getpid(), **command}) + "\n")
        if kind == "switch_session":
            session = command["sessionPath"]
        elif kind == "new_session":
            number += 1
            session = f"/fixture/live-{number}"
        data = {"sessionFile": session, "isStreaming": False, "pendingMessageCount": 0}
        emit({"id": command["id"], "type": "response", "command": kind,
              "success": True, "data": data if kind == "get_state" else {}})
        if kind == "prompt":
            emit({"type": "agent_start"})
            await_ui_busy(command["message"])
            message = {"role": "assistant", "content": [{"type": "text", "text": command["message"]}],
                       "stopReason": "stop"}
            emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
            emit({"type": "message_end", "message": message})
            emit({"type": "agent_end", "messages": [message], "willRetry": False})
            emit({"type": "agent_settled"})


if __name__ == "__main__":
    if "--version" in sys.argv:
        print("0.87.1")
    else:
        serve()
