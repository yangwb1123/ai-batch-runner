"""Local Pi protocol fixture for the acceptance harness; never contacts a model."""

import json
from pathlib import Path
import re
import sys
import time


def emit(value):
    print(json.dumps(value), flush=True)


def respond(command, data=None):
    emit({"id": command["id"], "type": "response", "command": command["type"],
          "success": True, "data": data or {}})


def answer(text, stop="stop"):
    message = {"role": "assistant", "content": [{"type": "text", "text": text}],
               "stopReason": stop}
    emit({"type": "message_end", "message": message})
    emit({"type": "agent_end", "messages": [message], "willRetry": False})
    emit({"type": "agent_settled"})


def prompt(command, marker):
    respond(command)
    emit({"type": "agent_start"})
    emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
    time.sleep(0.5)
    text = command["message"]
    if "1000" in text:
        emit({"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "delta": "1. A triangle has three sides."}})
        return marker
    found = re.search(r"(?:RECOVERED_)?PBATCH_[A-Z0-9]+", text)
    answer(found.group() if found else marker)
    return found.group() if found else marker


def main():
    if "--version" in sys.argv:
        print("0.87.1")
        return
    Path("../provider-args.json").write_text(json.dumps(sys.argv))
    marker, session = "", 1
    for line in sys.stdin:
        command = json.loads(line)
        kind = command["type"]
        if kind == "prompt":
            marker = prompt(command, marker)
            continue
        if kind == "new_session":
            session += 1
        data = {"sessionId": f"fixture-{session}", "sessionFile": f"/fixture/{session}.jsonl"}
        respond(command, data if kind == "get_state" else {})
        if kind == "abort":
            answer("", "aborted")


if __name__ == "__main__":
    main()
