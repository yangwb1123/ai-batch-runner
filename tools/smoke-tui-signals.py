#!/usr/bin/env python3
"""Linux PTY signal shutdown acceptance, using only a local fake provider."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from tui_soak_support import (
    SoakFailure, Terminal, executable_evidence, is_alive, prepare_environment,
    process_identity,
)


def journal_events(root: Path, *, complete: bool = False) -> list[dict]:
    events = []
    for path in (root / "runtime").rglob("events.jsonl"):
        for line in path.read_bytes().splitlines(keepends=True):
            if not line.endswith(b"\n"):
                if complete:
                    raise SoakFailure("journal ended with an incomplete record")
                continue
            events.append(json.loads(line))
    return events


def wait_for(terminal: Terminal, predicate, label: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        terminal.pump()
        if predicate():
            return
        if terminal.process.poll() is not None:
            raise SoakFailure(f"TUI exited before {label}: {terminal.process.returncode}")
    raise SoakFailure(f"deadline waiting for {label}")


def owned_identities(root: Path) -> list[dict]:
    marker = root / "provider-pids.json"
    if not marker.exists():
        return []
    return [identity for pid in json.loads(marker.read_text())
            if (identity := process_identity(pid)) is not None]


def cleanup(terminal: Terminal, identities: list[dict]) -> None:
    for index, identity in enumerate(identities):
        if is_alive(identity):
            try:
                kill = os.killpg if index == 0 else os.kill
                kill(identity["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
    if terminal.process.poll() is None:
        os.killpg(terminal.process.pid, signal.SIGKILL)
    terminal.process.wait(timeout=3)
    terminal.close()


def validate_journal(root: Path, mode: str) -> dict:
    events = journal_events(root, complete=True)
    started = [event for event in events if event["kind"] == "turn.started"]
    if len(started) != 1:
        raise SoakFailure("expected exactly one started fixture turn")
    terminal = [event for event in events if event["kind"] in {
        "turn.completed", "turn.failed", "turn.interrupted"}]
    for event in terminal:
        if any(event[key] != started[0][key] for key in (
                "workspace_id", "session_id", "run_id", "turn_id", "actor_id")):
            raise SoakFailure("terminal outcome does not belong to the active fixture turn")
    if any(event["kind"] == "turn.completed" for event in terminal):
        raise SoakFailure("shutdown falsely recorded provider success")
    if mode != "quit" and (len(terminal) != 1 or terminal[0]["payload"].get("state") != "UNKNOWN"):
        raise SoakFailure("signal shutdown did not durably record one UNKNOWN outcome")
    return {"journal_kinds": [e["kind"] for e in events],
            "terminal_states": [e["payload"].get("state") for e in terminal]}


def validate_shutdown(root: Path, mode: str, exit_code: int,
                      identities: list[dict]) -> dict:
    expected = 0 if mode == "quit" else 128 + int(getattr(signal, mode))
    if exit_code != expected:
        raise SoakFailure(f"{mode} exit code {exit_code}; expected {expected}")
    alive = [identity["pid"] for identity in identities if is_alive(identity)]
    if len(identities) != 2 or alive:
        raise SoakFailure(f"provider process evidence missing or live descendants: {alive}")
    return {"mode": mode, "exit_code": exit_code, "live_owned_processes": alive,
            "owned_processes": identities, **validate_journal(root, mode)}


def run_case(command: list[str], root: Path, mode: str) -> dict:
    root.mkdir(parents=True)
    environment, provider = prepare_environment(root)
    fixture = Path(__file__).with_name("tui_signal_provider.py").resolve()
    provider.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(fixture)]) + ' "$@"\n')
    terminal = Terminal(command, root, environment, provider, 30)
    identities = []
    try:
        wait_for(terminal, lambda: terminal.composer.phase == "ready", "ready composer")
        terminal.send("Run the local signal shutdown fixture")
        wait_for(terminal, lambda: any(e["kind"] == "message.delta" for e in journal_events(root)),
                 "durable active provider output")
        identities = owned_identities(root)
        if len(identities) != 2 or not all(is_alive(value) for value in identities):
            raise SoakFailure("fixture provider and descendant were not both running")
        started = time.monotonic()
        if mode == "quit":
            terminal.send(b"\x11")
        else:
            os.kill(terminal.process.pid, getattr(signal, mode))
        wait_for(terminal, lambda: terminal.process.poll() is not None, "TUI exit")
        result = validate_shutdown(root, mode, terminal.process.returncode, identities)
        result["shutdown_seconds"] = round(time.monotonic() - started, 3)
        return result
    finally:
        cleanup(terminal, identities or owned_identities(root))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, help="frozen executable; default is this checkout")
    parser.add_argument("--output-dir", type=Path, help="new evidence directory")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("this acceptance requires Linux /proc and PTYs")
    root = args.output_dir or Path(tempfile.mkdtemp(prefix="pi-batch-tui-signals-"))
    root = root.expanduser().resolve()
    if args.output_dir:
        root.mkdir(parents=True)
    command = ([str(args.binary.resolve())] if args.binary else
               [sys.executable, str(Path(__file__).resolve().parents[1] / "pi-batch.py")])
    report = {"schema_version": 1, "scope": "local fake provider; SIGTERM/SIGHUP only",
              "executable": executable_evidence(command), "cases": [], "ok": False}
    try:
        for mode in ("quit", "SIGTERM", "SIGHUP"):
            report["cases"].append(run_case(command, root / mode, mode))
        report["ok"] = True
    except (OSError, ValueError, SoakFailure, subprocess.TimeoutExpired) as exc:
        report["error"] = str(exc)
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"ok": report["ok"], "report": str(root / "report.json")}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
