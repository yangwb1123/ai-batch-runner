"""Local Pi RPC fixture: remain active until the TUI closes our process group."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time


def emit(value: dict) -> None:
    print(json.dumps(value), flush=True)


def record_pids(pids: list[int]) -> None:
    pending = Path("../provider-pids.tmp")
    pending.write_text(json.dumps(pids))
    pending.replace("../provider-pids.json")


def serve() -> None:
    record_pids([os.getpid()])
    for line in sys.stdin:
        command = json.loads(line)
        kind = command["type"]
        data = ({"sessionId": "signal-fixture", "sessionFile": "/fixture/session.jsonl"}
                if kind == "get_state" else {})
        emit({"id": command["id"], "type": "response", "command": kind,
              "success": True, "data": data})
        if kind == "prompt":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            record_pids([os.getpid(), child.pid])
            emit({"type": "agent_start"})
            emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
            emit({"type": "message_update", "assistantMessageEvent": {
                "type": "text_delta", "delta": "local fixture is working"}})
            time.sleep(60)  # No terminal result and no interrupt acknowledgement.


if __name__ == "__main__":
    if "--version" in sys.argv:
        print("0.87.1")
    else:
        serve()
