#!/usr/bin/env python3
"""Exercise one Linux TUI process through repeated turns and fault recovery.

The provider is deterministic local Pi RPC simulation, with no model or user
credentials. This produces measured evidence, not a 7x24 or live-provider claim.
Example: python tools/tui-soak.py dist/pi-batch-linux-x86_64 --cycles 10
Duration mode: --cycles 0 --duration 3600 (finishes its current cycle).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from tui_soak_support import (
    JournalEvidence, SoakFailure, Terminal, append_json, is_alive,
    executable_evidence, kill_owned_process, kill_tui_process,
    prepare_environment, process_identity, resource_growth,
    resource_sample,
)
from tui_soak_provider_log import ProviderLog


class SoakRun:
    def __init__(self, command: list[str], root: Path, *, turn_timeout: int = 5,
                 step_timeout: float = 20, max_fd_growth: int = 8,
                 max_rss_growth_mib: float = 128):
        self.command, self.root = command, root
        self.executable = executable_evidence(command)
        self.turn_timeout, self.step_timeout = turn_timeout, step_timeout
        self.max_fd_growth, self.max_rss_growth_mib = max_fd_growth, max_rss_growth_mib
        self.journal = JournalEvidence(root / "runtime")
        self.journal_report = None
        self.provider_log = ProviderLog(root / "provider.jsonl", SoakFailure)
        self.providers, self.samples = {}, []
        self.provider_count = self.sample_count = self.prompt_matches = 0
        self.expected_prompt = ""
        self.forced_cleanup = []
        self.tui_identity = self.process_identity = self.terminal = None
        self.started, self.cycles, self.recoveries = time.monotonic(), 0, 0
        self.timeouts = self.cancellations = self.failures = self.successes = 0
        self.exit_code = None
        self.requested = {}
        self.last_command, self.phase = "", "created"

    def run(self, *, cycles: int = 3, duration: float = 0) -> dict:
        self.requested = {"cycles": cycles, "duration_seconds": duration,
                          "turn_timeout_seconds": self.turn_timeout,
                          "step_timeout_seconds": self.step_timeout}
        environment, provider = prepare_environment(self.root)
        try:
            self.terminal = Terminal(self.command, self.root, environment,
                                     provider, self.turn_timeout)
            self.process_identity = process_identity(self.terminal.process.pid)
            self._wait(lambda: bool(self.journal.counts["session.created"]), "initial session")
            self._wait(lambda: self.terminal.composer.phase == "ready", "initial composer ready")
            self._sample("startup")
            while self._continue(cycles, duration):
                self._cycle(self.cycles + 1)
                self.cycles += 1
                self._sample("cycle")
                print(f"PASS soak cycle {self.cycles}; same TUI PID {self.tui_identity['pid']}", flush=True)
            self._quit()
            return self.report("passed")
        finally:
            self._cleanup()

    def _continue(self, cycles: int, duration: float) -> bool:
        if cycles and self.cycles >= cycles:
            return False
        return not duration or not self.cycles or time.monotonic() - self.started < duration

    def _poll(self) -> None:
        self.terminal.pump()
        self._read_provider_log()
        self.journal.poll()
        for pid, identity in tuple(self.providers.items()):
            if not is_alive(identity):
                append_json(self.root / "processes.jsonl", {**identity, "state": "exited"})
                del self.providers[pid]
        if self.tui_identity and not is_alive(self.tui_identity):
            raise SoakFailure("TUI exited during the sustained run")
        if self.terminal.process.poll() is not None:
            raise SoakFailure(f"TUI exited early: {self.terminal.process.returncode}")

    def _read_provider_log(self) -> None:
        events = self.provider_log.read()
        # Capture all live children before any validation can abort this batch.
        for event in events:
            if event.get("kind") == "started":
                identity = process_identity(event["pid"])
                if identity:
                    self.providers[identity["pid"]] = identity
        for event in events:
            if event.get("kind") == "started":
                self._provider_started(event)
            elif event.get("kind") == "prompt":
                if event.get("message") != self.expected_prompt:
                    raise SoakFailure("provider received an unexpected or replayed prompt")
                self.prompt_matches += 1

    def _provider_started(self, event: dict) -> None:
        parent = process_identity(event["ppid"])
        identity = process_identity(event["pid"])
        if not parent or not identity:
            raise SoakFailure("provider or TUI disappeared before process evidence was collected")
        if self.tui_identity and parent != self.tui_identity:
            # Process state can change between reads; identity uses PID and start ticks.
            if (parent["pid"], parent["start_ticks"]) != (
                    self.tui_identity["pid"], self.tui_identity["start_ticks"]):
                raise SoakFailure("recovery replaced the TUI process")
        self.tui_identity = parent
        if len(self.providers) >= 4:
            raise SoakFailure("too many live provider identities")
        self.providers[identity["pid"]] = identity
        self.provider_count += 1
        append_json(self.root / "processes.jsonl", {**identity, "state": "started"})

    def _wait(self, predicate, label: str, timeout: float = 0) -> None:
        self.phase = label
        deadline = time.monotonic() + (timeout or self.step_timeout)
        while time.monotonic() < deadline:
            self._poll()
            if predicate():
                return
        detail = self.terminal.tail.decode("utf-8", "replace")[-2000:]
        raise SoakFailure(f"deadline waiting for {label}; terminal tail: {detail}")

    def _send(self, value: str | bytes) -> None:
        self.last_command = value if isinstance(value, str) else repr(value)
        append_json(self.root / "commands.jsonl", {
            "seconds": round(time.monotonic() - self.started, 3),
            "command": self.last_command, "phase": self.phase,
            "ui": self.terminal.composer.summary()})
        self.terminal.send(value)

    def _cycle(self, number: int) -> None:
        before = time.monotonic()
        self._turn("success", f"{number}-first")
        self._turn("success", f"{number}-second")
        for fault in ("failure", "timeout", "cancel"):
            self._turn(fault, str(number))
            self._new_session()
            self._turn("success", f"{number}-after-{fault}")
            self.recoveries += 1
        append_json(self.root / "cycles.jsonl", {
            "cycle": number, "seconds": round(time.monotonic() - before, 3),
            "tui_pid": self.tui_identity["pid"], "recoveries": 3,
            "successes": 5, "provider_failures": 1, "timeouts": 1, "cancellations": 1})

    def _turn(self, scenario: str, token: str) -> None:
        terminal_kind = "turn.completed" if scenario == "success" else "turn.failed"
        initial = self.journal.counts[terminal_kind]
        started = self.journal.counts["turn.started"]
        prompt = f"soak:{scenario}:{token}"
        self.expected_prompt, self.prompt_matches = prompt, 0
        revision = self.terminal.composer.revision
        self._send(prompt)
        self._wait(lambda: self.journal.counts["turn.started"] > started, scenario + " started")
        self._wait(lambda: self.terminal.composer.busy_revision > revision, scenario + " composer busy")
        if scenario in {"success", "failure"}:
            self._release_provider(prompt)
        if scenario == "cancel":
            self._send(b"\x03")
        self._wait(lambda: self.journal.counts[terminal_kind] > initial, scenario + " terminal")
        if self.prompt_matches != 1:
            raise SoakFailure(f"prompt was lost or replayed: {prompt}")
        event = self.journal.latest[terminal_kind]
        payload = event["payload"]
        if scenario == "failure" and (payload.get("state") != "FAILED"
                or payload.get("reason") != "soak injected provider failure"):
            raise SoakFailure(f"injected provider failure was not confirmed: {payload}")
        if scenario in {"timeout", "cancel"}:
            reason = "timed out" if scenario == "timeout" else "cancelled"
            if payload.get("state") != "UNKNOWN" or reason not in payload.get("reason", ""):
                raise SoakFailure(f"{scenario} falsely claimed a confirmed provider terminal: {payload}")
            self._wait(lambda: not self._live_providers(), scenario + " process teardown")
        if scenario == "success":
            text = self.journal.latest["message.completed"]["payload"].get("text", "")
            if prompt not in text:
                raise SoakFailure("successful turn did not retain its exact provider reply")
        counter = {"success": "successes", "failure": "failures",
                   "timeout": "timeouts", "cancel": "cancellations"}[scenario]
        setattr(self, counter, getattr(self, counter) + 1)
        self._wait(lambda: self.terminal.composer.ready_after(revision), scenario + " composer ready")

    def _release_provider(self, prompt: str) -> None:
        providers = self._live_providers()
        if len(providers) != 1:
            raise SoakFailure("fixture release requires exactly one live provider")
        temporary = self.root / "provider-release.tmp"
        temporary.write_text(json.dumps({"message": prompt, "pid": providers[0]}), encoding="utf-8")
        temporary.replace(self.root / "provider-release.json")

    def _new_session(self) -> None:
        initial = self.journal.counts["session.created"]
        self._send("/new")
        self._wait(lambda: self.journal.counts["session.created"] > initial, "new session")
        if len(self._live_providers()) != 1:
            raise SoakFailure("recovery did not leave exactly one live provider")

    def _live_providers(self) -> list[int]:
        return [pid for pid, identity in self.providers.items() if is_alive(identity)]

    def _sample(self, phase: str) -> None:
        sample = {"phase": phase, "cycle": self.cycles,
                  "seconds": round(time.monotonic() - self.started, 3),
                  "live_provider_pids": self._live_providers(),
                  **resource_sample(self.tui_identity)}
        if len(sample["live_provider_pids"]) != 1:
            raise SoakFailure("a provider process leaked between cycles")
        self.sample_count += 1
        if not self.samples:
            self.samples = [sample, sample, sample]
        else:
            peak = dict(self.samples[1])
            peak.update({key: max(peak[key], sample[key]) for key in ("fds", "rss_kib", "threads")})
            self.samples = [self.samples[0], peak, sample]
        append_json(self.root / "resources.jsonl", sample)
        resource_growth(self.samples, self.max_fd_growth, self.max_rss_growth_mib)

    def _quit(self) -> None:
        self._send(b"\x11")
        deadline = time.monotonic() + self.step_timeout
        while self.terminal.process.poll() is None and time.monotonic() < deadline:
            self.terminal.pump()
        if self.terminal.process.poll() is None:
            raise SoakFailure("Ctrl+Q did not terminate the TUI within the deadline")
        self.exit_code = self.terminal.process.returncode
        if self.exit_code != 0 or self._live_providers():
            raise SoakFailure(f"unclean TUI exit: {self.exit_code}; providers: {self._live_providers()}")
        self._finish_provider_log(time.monotonic() + self.step_timeout)
        self.journal.finish(time.monotonic() + self.step_timeout)

    def _finish_provider_log(self, deadline: float) -> None:
        while True:
            if time.monotonic() > deadline:
                raise SoakFailure("final provider evidence export exceeded its deadline")
            self._read_provider_log()
            if self.provider_log.exhausted:
                return

    def _cleanup(self) -> None:
        try:
            self._read_provider_log()
        except (OSError, SoakFailure):
            pass
        try:
            for identity in self.providers.values():
                if is_alive(identity):
                    self.forced_cleanup.append(identity["pid"])
                kill_owned_process(identity)
        finally:
            try:
                kill_tui_process(self.process_identity, self.tui_identity)
                if self.terminal:
                    self.terminal.process.wait(timeout=5)
            finally:
                if self.terminal:
                    self.terminal.close()
                try:
                    self.journal_report = self.journal.summary()
                finally:
                    self.journal.close()

    def report(self, status: str, error: str = "") -> dict:
        resources = (resource_growth(self.samples, self.max_fd_growth, self.max_rss_growth_mib)
                     if self.samples and status == "passed" else {})
        resources["samples"] = self.sample_count
        return {"schema_version": 1, "status": status, "error": error,
                "executable": self.executable,
                "requested": self.requested,
                "last_command": self.last_command, "phase": self.phase,
                "ui": self.terminal.composer.summary() if self.terminal else {},
                "provider": "deterministic local Pi RPC simulation; no live model",
                "provider_event_storage": {"authoritative": "provider.sqlite committed rows",
                                           "export": "provider.jsonl written by the harness",
                                           "records_exported": self.provider_log.offset},
                "fixture_release": "success/failure replies require an observed busy composer frame",
                "platform": sys.platform, "single_tui_process": bool(self.tui_identity),
                "tui_pid": self.tui_identity["pid"] if self.tui_identity else None,
                "actual_seconds": round(time.monotonic() - self.started, 3),
                "cycles_completed": self.cycles, "recoveries": self.recoveries,
                "turns": {"succeeded": self.successes, "provider_failed": self.failures,
                          "timeout_unknown": self.timeouts, "cancel_unknown": self.cancellations},
                "provider_processes_started": self.provider_count,
                "provider_processes_alive_after_exit": self._live_providers(),
                "forced_cleanup_pids": self.forced_cleanup,
                "exit_code": self.exit_code, "resources": resources,
                "terminal_output_bytes": self.terminal.output_bytes if self.terminal else 0,
                "terminal_saved_bytes": self.terminal.saved_bytes if self.terminal else 0,
                "journal": self.journal_report or self.journal.summary(),
                "evidence_directory": str(self.root),
                "limitations": ["Not a 7x24 certification", "No live provider, network, or credentials",
                                "Linux PTY and /proc only", "Resource allowances are bounded observations"]}


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--cycles", type=int, default=3, help="0 for duration-only mode")
    parser.add_argument("--duration", type=float, default=0, help="seconds; finish the current cycle")
    parser.add_argument("--turn-timeout", type=int, default=5)
    parser.add_argument("--step-timeout", type=float, default=20)
    parser.add_argument("--max-fd-growth", type=int, default=8)
    parser.add_argument("--max-rss-growth-mib", type=float, default=128)
    parser.add_argument("--output", type=Path, help="new evidence directory; default creates /tmp directory")
    args = parser.parse_args(argv)
    args.executable = args.executable.expanduser().resolve()
    if not sys.platform.startswith("linux"):
        parser.error("this evidence harness requires Linux PTY and /proc")
    if not args.executable.is_file():
        parser.error("executable does not exist")
    if args.cycles < 0 or args.duration < 0 or not math.isfinite(args.duration):
        parser.error("cycles and duration must be finite and non-negative")
    if args.cycles == 0 and args.duration == 0:
        parser.error("specify positive cycles or duration")
    if args.turn_timeout < 1 or args.max_fd_growth < 0:
        parser.error("turn timeout must be positive; FD allowance must be non-negative")
    if not math.isfinite(args.step_timeout) or args.step_timeout <= args.turn_timeout + 3:
        parser.error("step timeout must be finite and exceed turn timeout by more than 3 seconds")
    if not math.isfinite(args.max_rss_growth_mib) or args.max_rss_growth_mib < 0:
        parser.error("RSS allowance must be finite and non-negative")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.output.expanduser().resolve() if args.output else Path(
        tempfile.mkdtemp(prefix="pi-batch-tui-soak-"))
    if args.output:
        root.mkdir(parents=True, exist_ok=False)
    run = SoakRun([str(args.executable)], root, turn_timeout=args.turn_timeout,
                  step_timeout=args.step_timeout, max_fd_growth=args.max_fd_growth,
                  max_rss_growth_mib=args.max_rss_growth_mib)
    try:
        report = run.run(cycles=args.cycles, duration=args.duration)
    except (SoakFailure, OSError, subprocess.TimeoutExpired) as exc:
        report = run.report("failed", str(exc))
    (root / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
