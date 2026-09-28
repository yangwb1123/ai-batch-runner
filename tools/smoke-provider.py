#!/usr/bin/env python3
"""Explicit live-provider Linux binary acceptance: four bounded model turns.

Uses existing provider credentials, a temporary project, and isolated runner data.
This is a short real-model acceptance test, not a sustained-availability claim.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from provider_smoke_support import (
    OwnedProcesses, ProviderJournal, bind_provider, prepare_environment, provider_version,
    validate_cancel, validate_success, write_report,
)
from tui_soak_support import SoakFailure, Terminal, append_json, executable_evidence


class ProviderRun:
    def __init__(self, command: list[str], root: Path, *, provider: Path,
                 runtime: str, timeout: int = 120, overall_timeout: int = 250):
        self.command, self.root, self.provider = command, root, provider
        self.runtime, self.timeout = runtime, timeout
        self.executable = executable_evidence(command)
        self.journal = ProviderJournal(root / "runtime")
        self.terminal = self.processes = None
        self.started, self.phase, self.version = time.monotonic(), "created", ""
        self.overall_timeout = overall_timeout
        self.sent, self.results = 0, []
        self.exit_code, self.error, self.journal_report = None, "", None
        self.token = "PBATCH_" + uuid.uuid4().hex[:16].upper()

    def run(self) -> dict:
        try:
            environment = prepare_environment(self.root)
            self.version = provider_version(self.provider, environment, self.root / "workspace")
            alias = bind_provider(self.root, environment, self.provider, self.runtime)
            runtime_arg = "app-server" if self.runtime == "codex-app-server" else self.runtime
            self.terminal = Terminal(self.command, self.root, environment, alias,
                                     self.timeout, runtime=runtime_arg)
            self.processes = OwnedProcesses(self.terminal.process.pid)
            self._wait(lambda: self.journal.counts["session.created"] > 0, "initial session")
            self._wait(lambda: self.terminal.composer.phase == "ready", "initial composer")
            self._acceptance()
            self._quit()
        except (SoakFailure, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            self.error = str(exc)
        finally:
            self._cleanup()
        return self.report()

    def _poll(self) -> None:
        self.terminal.pump()
        self.processes.poll()
        self.journal.poll()
        if self.terminal.process.poll() is not None:
            raise SoakFailure(f"TUI exited during {self.phase}: {self.terminal.process.returncode}")

    def _wait(self, predicate, label: str) -> None:
        self.phase = label
        deadline = min(time.monotonic() + self.timeout + 10, self.started + self.overall_timeout)
        while time.monotonic() < deadline:
            self._poll()
            if predicate():
                return
        raise SoakFailure(f"deadline waiting for {label}")

    def _send(self, value: str | bytes) -> None:
        append_json(self.root / "commands.jsonl", {
            "seconds": round(time.monotonic() - self.started, 3),
            "command": value if isinstance(value, str) else repr(value), "phase": self.phase})
        self.terminal.send(value)

    def _start_turn(self, prompt: str) -> tuple[dict, int]:
        if self.sent >= 4:
            raise SoakFailure("model call budget exhausted")
        previous = set(self.journal.turns)
        revision = self.terminal.composer.revision
        self._send(prompt)
        self.sent += 1
        self._wait(lambda: bool(set(self.journal.turns) - previous), "new turn identity")
        new = set(self.journal.turns) - previous
        if len(new) != 1:
            raise SoakFailure("one prompt produced multiple turn identities")
        turn = self.journal.turns[new.pop()]
        self._wait(lambda: turn["starts"] == 1 or turn["terminal"], "provider turn start")
        if turn["starts"] != 1:
            raise SoakFailure("provider never started the requested turn")
        return turn, revision

    def _success(self, label: str, prompt: str, expected: str) -> dict:
        turn, revision = self._start_turn(prompt)
        self._wait(lambda: bool(turn["terminal"]), label + " terminal")
        validate_success(turn, expected)
        self._wait(lambda: self.terminal.composer.ready_after(revision), label + " composer")
        self._record(label, turn)
        return turn

    def _record(self, label: str, turn: dict) -> None:
        result = {key: value for key, value in turn.items() if key != "messages"}
        result.update({"label": label, "message_count": len(turn["messages"]),
                       "seconds": round(time.monotonic() - self.started, 3)})
        self.results.append(result)
        append_json(self.root / "turns.jsonl", result)

    def _acceptance(self) -> None:
        first = self._success("exact_reply", f"Remember this marker: {self.token}. "
                              f"Reply with exactly {self.token}. Do not use tools.", self.token)
        recalled = self._success("continuation", "Reply with exactly the marker I asked you "
                                 "to remember in the preceding turn. Do not use tools.", self.token)
        if recalled["session_id"] != first["session_id"]:
            raise SoakFailure("continuation silently changed the session")
        self._cancel()
        previous = self.journal.counts["session.created"]
        self._send("/new")
        self._wait(lambda: self.journal.counts["session.created"] > previous, "fresh session")
        self._wait(lambda: self.terminal.composer.phase == "ready", "fresh composer")
        recovered = self._success("recovery", f"Reply with exactly RECOVERED_{self.token}. "
                                  "Do not use tools.", "RECOVERED_" + self.token)
        if recovered["session_id"] == first["session_id"]:
            raise SoakFailure("/new failed to create a distinct session")

    def _cancel(self) -> None:
        turn, revision = self._start_turn(
            "Do not use tools. Write a numbered list of 1000 detailed, harmless facts about "
            "geometry, each in a separate full sentence. Start immediately with item 1.")
        self._wait(lambda: turn["deltas"] > 0 or turn["terminal"], "active model text")
        if turn["terminal"]:
            raise SoakFailure("turn finished before cancellation; cancellation was not tested")
        turn["cancel_requested"] = True
        self._send(b"\x03")
        self._wait(lambda: bool(turn["terminal"]), "cancel terminal")
        validate_cancel(turn)
        self._wait(lambda: self.terminal.composer.ready_after(revision), "cancel composer")
        self._record("cancel", turn)

    def _quit(self) -> None:
        self.phase = "normal exit"
        self._send(b"\x11")
        deadline = min(time.monotonic() + 15, self.started + self.overall_timeout)
        while time.monotonic() < deadline:
            self.terminal.pump()
            self.processes.poll()
            if self.terminal.process.poll() is not None and not self.processes.live():
                break
        self.exit_code = self.terminal.process.poll()
        if self.exit_code != 0 or self.processes.live():
            raise SoakFailure("normal TUI exit left an error or live owned processes")
        self.journal.finish(time.monotonic() + 10)
        if self.sent != 4 or len(self.journal.turns) != 4 or len(self.results) != 4:
            raise SoakFailure("turn budget or result count did not match the four-turn plan")
        if executable_evidence(self.command) != self.executable:
            raise SoakFailure("tested executable changed during acceptance")

    def _cleanup(self) -> None:
        try:
            if self.processes:
                self.processes.cleanup()
            if self.terminal:
                self.terminal.process.wait(timeout=5)
        except (OSError, SoakFailure, subprocess.TimeoutExpired) as exc:
            self.error = self.error or f"cleanup failed: {exc}"
        finally:
            self.journal_report = self.journal.summary()
            self.journal.close()
            if self.terminal:
                self.terminal.close()

    def report(self) -> dict:
        forced = self.processes.forced_cleanup if self.processes else []
        alive = self.processes.live() if self.processes else []
        return {"schema_version": 1, "kind": "live-provider-tui-acceptance",
                "status": "failed" if self.error or forced or alive else "passed",
                "error": self.error, "phase": self.phase, "executable": self.executable,
                "runtime": self.runtime, "provider": {"path": str(self.provider), "version": self.version},
                "model_selection": "existing provider account default",
                "model_prompts_sent": self.sent, "model_prompt_limit": 4, "turns": self.results,
                "prompt_count_source": "TUI composer submissions; provider starts verified per turn",
                "overall_timeout_seconds": self.overall_timeout,
                "turn_timeout_seconds": self.timeout, "actual_seconds": round(time.monotonic() - self.started, 3),
                "journal": self.journal_report, "exit_code": self.exit_code,
                "observed_process_count": len(self.processes.identities) if self.processes else 0,
                "observed_process_identities": list(self.processes.identities.values()) if self.processes else [],
                "owned_processes_alive_after_exit": alive, "forced_cleanup_pids": forced,
                "evidence_directory": str(self.root),
                "limitations": ["Short acceptance run, not a 7x24 availability certification",
                                "At most four submitted turns; provider-internal retries are not counted",
                                "Linux PTY and observed /proc descendant identities only"]}


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--runtime", choices=("codex-app-server", "pi-rpc"), required=True)
    parser.add_argument("--agent-bin", help="default: codex or pi from PATH")
    parser.add_argument("--turn-timeout", type=int, default=120)
    parser.add_argument("--overall-timeout", type=int, default=250)
    parser.add_argument("--output", type=Path, help="new evidence directory")
    parser.add_argument("--allow-model-calls", action="store_true", required=True,
                        help="explicitly permit up to four billed model requests")
    args = parser.parse_args(argv)
    args.executable = args.executable.expanduser().resolve()
    if not sys.platform.startswith("linux") or not args.executable.is_file():
        parser.error("requires Linux and an existing executable")
    if not 10 <= args.turn_timeout <= 600:
        parser.error("turn timeout must be between 10 and 600 seconds")
    if not 30 <= args.overall_timeout <= 2400:
        parser.error("overall timeout must be between 30 and 2400 seconds")
    name = args.agent_bin or ("codex" if args.runtime == "codex-app-server" else "pi")
    located = shutil.which(name)
    if not located:
        parser.error("provider executable was not found")
    args.provider = Path(located).absolute()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.output.expanduser().resolve() if args.output else Path(
        tempfile.mkdtemp(prefix="pi-batch-live-provider-"))
    if args.output:
        root.mkdir(parents=True, exist_ok=False)
    run = ProviderRun([str(args.executable)], root, provider=args.provider,
                      runtime=args.runtime, timeout=args.turn_timeout,
                      overall_timeout=args.overall_timeout)
    report = run.run()
    write_report(root / "report.json", report)
    print(f"{report['status'].upper()}: {root / 'report.json'}")
    if report["error"]:
        print(report["error"], file=sys.stderr)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
