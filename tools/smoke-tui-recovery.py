#!/usr/bin/env python3
"""Linux PTY recovery acceptance over five seeded journal shapes; no model calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from provider_smoke_support import OwnedProcesses
from tui_soak_support import SoakFailure, Terminal, executable_evidence, prepare_environment


STATES = ("QUEUED", "RUNNING", "INTERRUPTING", "UNKNOWN", "SUCCEEDED")


def seed_sessions(root: Path, environment: dict) -> dict:
    """Only the test input is made by host Python; the tested TUI replays it."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pbatch.tui_journal import RuntimeEventJournal
    from pbatch.tui_protocol import RuntimeEvent
    with patch.dict(os.environ, environment, clear=True):
        return {state: _seed_session(root, state, RuntimeEventJournal, RuntimeEvent)
                for state in STATES}


def _seed_session(root: Path, state: str, journal_type, event_type) -> dict:
    session = "recovery-" + state.lower()
    journal = journal_type(str(root / "workspace"), session)
    for sequence, (kind, payload) in enumerate(_seed_events(state), 1):
        journal.append(_seed_event(
            root, session, sequence, kind, payload, event_type))
    return {"session_id": session, "journal": str(journal.path),
            "sha256": hashlib.sha256(journal.path.read_bytes()).hexdigest()}


def _seed_events(state: str) -> list[tuple[str, dict]]:
    events = [("session.created", {
        "provider_session_id": "/fixture/" + state.lower()}),
        ("turn.queued", {"state": "QUEUED"})]
    if state != "QUEUED":
        events.append(("turn.started", {"state": "RUNNING"}))
    if state == "INTERRUPTING":
        events.append(("turn.interrupting", {"state": state}))
    elif state in {"UNKNOWN", "SUCCEEDED"}:
        kind = "turn.failed" if state == "UNKNOWN" else "turn.completed"
        events.append((kind, {"state": state}))
    return events


def _seed_event(root: Path, session: str, sequence: int, kind: str,
                payload: dict, event_type):
    return event_type(
        event_id=f"evt_{session}_{sequence}", seq=sequence,
        occurred_at="2026-09-27T00:00:00Z",
        workspace_id=str(root / "workspace"), session_id=session,
        run_id="seeded-run",
        turn_id="" if kind.startswith("session.") else "seeded-turn",
        actor_id="agent_main", kind=kind, payload=payload,
        extensions={"runtime": "pi-rpc"})


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_bytes().splitlines(keepends=True)
            if line.endswith(b"\n")]


class RecoveryRun:
    def __init__(self, command: list[str], root: Path):
        self.command, self.root = command, root
        self.terminal = self.processes = None
        self.sessions, self.results = {}, []
        self.retained_input = False

    def controls(self, kind: str) -> list[dict]:
        return [value for value in read_jsonl(self.root / "provider-controls.jsonl")
                if value["type"] == kind]

    def events(self, kind: str) -> list[dict]:
        return [value for path in (self.root / "runtime").rglob("events.jsonl")
                for value in read_jsonl(path) if value["kind"] == kind]

    def wait(self, predicate, label: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.terminal.pump()
            self.processes.poll()
            if predicate():
                return
            if self.terminal.process.poll() is not None:
                raise SoakFailure(f"TUI exited before {label}: {self.terminal.process.returncode}")
        raise SoakFailure(f"deadline waiting for {label}")

    def send(self, value: str) -> None:
        if self.retained_input:
            previous = self.terminal.composer.ready_revision
            self.terminal.send(b"\x05\x15")  # End, then erase; retained drafts restore at column zero.
            self.wait(lambda: self.terminal.composer.ready_revision > previous, "cleared retained input")
            self.retained_input = False
        self.terminal.tail.clear()
        self.terminal.send(value)

    def visible(self, text: str) -> bool:
        raw = self.terminal.tail.decode("utf-8", errors="replace")
        plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", raw)
        return text in " ".join(plain.split())

    def summary(self, state: str) -> dict:
        path = Path(self.sessions[state]["journal"]).with_name("summary.json")
        return json.loads(path.read_text()) if path.exists() else {}

    def check_unknown(self, state: str) -> None:
        self.send("/resume " + self.sessions[state]["session_id"])
        self.wait(lambda: self.summary(state).get("read_only") or self.controls("switch_session")
                  or self.controls("prompt"),
                  state + " recovery decision")
        if self.controls("switch_session") or self.controls("prompt"):
            raise SoakFailure(f"{state} recovery invoked the provider")
        if self.summary(state).get("turn_state") != "UNKNOWN":
            raise SoakFailure(f"{state} recovery did not preserve UNKNOWN")
        self.send("THIS_UNKNOWN_TURN_MUST_NOT_EXECUTE")
        self.wait(lambda: self.visible("Recovered history is read-only") or self.controls("prompt"),
                  state + " blocked submission")
        if self.controls("switch_session") or self.controls("prompt"):
            raise SoakFailure(f"{state} accepted a turn before /new")
        self.retained_input = True
        self.results.append({"seeded_state": state, "recovered": "UNKNOWN",
                             "provider_switches": 0, "provider_prompts": 0})

    def successful_turn(self, session: str, marker: str) -> None:
        before = {event["event_id"] for event in self.events("turn.completed")}
        revision = self.terminal.composer.revision
        self.send(marker)
        self.wait(lambda: self.terminal.composer.busy_revision > revision and self.controls("prompt"),
                  "busy composer before fixture reply")
        release = {"message": marker, "pid": self.controls("prompt")[-1]["pid"]}
        (self.root / "provider-release.json").write_text(json.dumps(release))
        self.wait(lambda: any(event["event_id"] not in before for event in self.events("turn.completed")),
                  "successful explicit fixture turn")
        new = [event for event in self.events("turn.completed") if event["event_id"] not in before]
        if len(new) != 1 or new[0]["session_id"] != session:
            raise SoakFailure("completed turn belongs to the wrong resumed/new session")
        scope = ("workspace_id", "session_id", "run_id", "turn_id", "actor_id")
        messages = [event for event in self.events("message.completed")
                    if all(event[key] == new[0][key] for key in scope)]
        if not any(event["payload"].get("text") == marker for event in messages):
            raise SoakFailure("completed turn is missing its matching fixture answer")
        self.wait(lambda: self.terminal.composer.ready_after(revision), "ready composer after turn")

    def check_new_and_succeeded(self) -> None:
        before = {event["event_id"] for event in self.events("session.created")}
        self.send("/new")
        self.wait(lambda: any(event["event_id"] not in before for event in self.events("session.created")),
                  "fresh session")
        created = [event for event in self.events("session.created") if event["event_id"] not in before]
        if len(created) != 1 or created[0]["session_id"] in {s["session_id"] for s in self.sessions.values()}:
            raise SoakFailure("/new did not create one independent session")
        self.successful_turn(created[0]["session_id"], "RECOVERY_NEW_OK")
        self.send("/resume " + self.sessions["SUCCEEDED"]["session_id"])
        self.wait(lambda: bool(self.controls("switch_session")), "confirmed session resume")
        if [value["sessionPath"] for value in self.controls("switch_session")] != ["/fixture/succeeded"]:
            raise SoakFailure("resume selected an unexpected provider session")
        self.successful_turn(self.sessions["SUCCEEDED"]["session_id"], "RECOVERY_RESUME_OK")
        if [value["message"] for value in self.controls("prompt")] != ["RECOVERY_NEW_OK", "RECOVERY_RESUME_OK"]:
            raise SoakFailure("unexpected provider prompts; possible automatic replay")
        self.results.append({"seeded_state": "SUCCEEDED", "resumed_successfully": True,
                             "fresh_session": created[0]["session_id"], "explicit_prompts": 2})

    def run(self) -> list[dict]:
        environment, provider = prepare_environment(self.root)
        fixture = Path(__file__).with_name("tui_recovery_provider.py").resolve()
        provider.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(fixture)]) + ' "$@"\n')
        self.sessions = seed_sessions(self.root, environment)
        self.terminal = Terminal(self.command, self.root, environment, provider, 30)
        self.processes = OwnedProcesses(self.terminal.process.pid)
        try:
            self.wait(lambda: self.terminal.composer.phase == "ready", "initial composer")
            for state in STATES[:-1]:
                self.check_unknown(state)
            self.check_new_and_succeeded()
            self.send("/quit")
            self.wait(lambda: self.terminal.process.poll() is not None, "clean TUI exit")
            if self.terminal.process.returncode != 0 or self.processes.live():
                raise SoakFailure("TUI exit failed or left owned processes alive")
            for state in STATES[:-1]:
                seed = self.sessions[state]
                if hashlib.sha256(Path(seed["journal"]).read_bytes()).hexdigest() != seed["sha256"]:
                    raise SoakFailure(f"{state} recovery changed trusted journal evidence")
            return self.results
        finally:
            self.processes.cleanup()
            self.terminal.process.wait(timeout=3)
            self.terminal.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, help="frozen executable; default is this checkout")
    parser.add_argument("--output-dir", type=Path, help="new evidence directory")
    args = parser.parse_args(argv)
    if not sys.platform.startswith("linux"):
        parser.error("Linux /proc and PTYs are required")
    root = (args.output_dir or Path(tempfile.mkdtemp(prefix="pi-batch-tui-recovery-"))).expanduser().resolve()
    if args.output_dir:
        root.mkdir(parents=True)
    command = ([str(args.binary.resolve())] if args.binary else
               [sys.executable, str(Path(__file__).resolve().parents[1] / "pi-batch.py")])
    run = RecoveryRun(command, root)
    report = {"schema_version": 1, "ok": False, "executable": executable_evidence(command),
              "scope": "seeded persisted journal shapes, not actual SIGKILL; local fake provider"}
    try:
        report["cases"] = run.run()
        report["ok"] = True
    except (OSError, ValueError, SoakFailure, subprocess.TimeoutExpired) as exc:
        report.update(error=str(exc), cases=run.results)
    report["seeded_sessions"] = run.sessions
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"ok": report["ok"], "report": str(root / "report.json")}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
