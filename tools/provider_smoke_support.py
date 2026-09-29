"""Bounded journal and process evidence for explicit live-provider acceptance."""

from __future__ import annotations

import os
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from tui_soak_support import JournalEvidence, SoakFailure, is_alive, process_identity


TERMINAL_KINDS = {"turn.completed", "turn.failed", "turn.interrupted"}


def prepare_environment(root: Path) -> dict[str, str]:
    """Keep existing provider authentication; isolate only project/runner state."""
    for name in ("workspace", "home", "runtime", "tmp", "sessions", "bin"):
        (root / name).mkdir()
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("PBATCH_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
    environment.update({
        "PBATCH_HOME": str(root / "home"), "PBATCH_RUNTIME_HOME": str(root / "runtime"),
        "PBATCH_SCRIPT_DIR": str(root / "workspace"), "TMPDIR": str(root / "tmp"),
        "PI_CODING_AGENT_SESSION_DIR": str(root / "sessions"),
        "TERM": "xterm-256color", "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
    })
    (root / "workspace" / "pi-batch.yaml").write_text(
        'config_version: 1\nagent:\n  default_model: ""\n  default_provider: ""\n',
        encoding="utf-8")
    return environment


def bind_provider(root: Path, environment: dict, provider: Path, runtime: str) -> Path:
    """App Server resolves `codex` via PATH; bind it to the versioned executable."""
    alias = root / "bin" / ("codex" if runtime == "codex-app-server" else "pi")
    alias.symlink_to(provider)
    environment["PATH"] = str(alias.parent) + os.pathsep + environment.get("PATH", "")
    return alias


def provider_version(provider: Path, env: dict, cwd: Path) -> str:
    """The only provider inspection is its public version command."""
    probe = Path(__file__).with_name("provider_version_probe.py")
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen([sys.executable, str(probe), str(provider)], cwd=cwd, env=env,
                                   stdout=output, stderr=output, start_new_session=True)
        owned = OwnedProcesses(process.pid)
        try:
            _wait_version(process, owned, output, timeout=20)
        finally:
            owned.cleanup()
            process.wait(timeout=5)
        output.seek(0)
        value = output.read(4097)
    if not value.strip() or len(value) > 4096:
        raise SoakFailure("provider version probe failed or exceeded its output limit")
    result = json.loads(value)
    if process.returncode or result.get("error"):
        raise SoakFailure(result.get("error") or "provider version probe failed")
    return result["version"]


def _wait_version(process, owned, output, timeout=10) -> None:
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        owned.poll()
        if os.fstat(output.fileno()).st_size > 4096:
            raise SoakFailure("provider version output exceeded its limit")
        if time.monotonic() >= deadline:
            raise SoakFailure("provider version command exceeded its deadline")
        time.sleep(0.01)
    if owned.live():
        raise SoakFailure("provider version command left live owned processes")


class OwnedProcesses:
    """Follow descendants by PID and start ticks, including separate groups."""

    def __init__(self, pid: int):
        identity = process_identity(pid)
        if identity is None:
            raise SoakFailure("launcher disappeared before identity capture")
        self.identities = {pid: identity}
        self.forced_cleanup = []

    def poll(self) -> None:
        pending = list(self.identities.values())
        while pending:
            parent = pending.pop()
            if not is_alive(parent):
                continue
            for pid in self._child_pids(parent["pid"]):
                child = process_identity(pid)
                known = self.identities.get(pid)
                if child and (known is None or known["start_ticks"] != child["start_ticks"]):
                    if len(self.identities) >= 128:
                        raise SoakFailure("provider process tree exceeded 128 identities")
                    self.identities[child["pid"]] = child
                    pending.append(child)

    @staticmethod
    def _child_pids(pid: int) -> set[int]:
        result = set()
        tasks = Path("/proc") / str(pid) / "task"
        try:
            with os.scandir(tasks) as entries:
                for count, entry in enumerate(entries):
                    if count >= 1024:
                        raise SoakFailure("owned process exceeded 1024 threads")
                    try:
                        with (Path(entry.path) / "children").open() as handle:
                            value = handle.read(65537)
                    except FileNotFoundError:
                        continue
                    if len(value) > 65536:
                        raise SoakFailure("owned process children list exceeded its limit")
                    result.update(int(child) for child in value.split())
                    if len(result) >= 128:
                        raise SoakFailure("owned process exceeded 128 children")
        except FileNotFoundError:
            pass  # A process exit may remove task/ during open or iteration.
        return result

    def live(self) -> list[int]:
        return [pid for pid, identity in self.identities.items() if is_alive(identity)]

    def cleanup(self) -> None:
        failure = None
        try:
            self.poll()
        except (OSError, SoakFailure) as exc:
            failure = exc
        for pid in reversed(self.live()):
            identity = self.identities[pid]
            if not is_alive(identity):
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                self.forced_cleanup.append(pid)
            except ProcessLookupError:
                pass
        if failure:
            raise failure


class ProviderJournal(JournalEvidence):
    """Four turn identities, bounded text, exactly one terminal per turn."""

    def __init__(self, root: Path):
        super().__init__(root)
        self.turns = {}

    def _accept(self, event: dict) -> None:
        super()._accept(event)
        turn_id = event.get("turn_id")
        if not turn_id:
            return
        scope_keys = ("workspace_id", "session_id", "run_id", "actor_id")
        if any(not isinstance(event.get(key), str) or not event[key] for key in scope_keys):
            raise SoakFailure("turn event is missing its complete scope")
        if turn_id not in self.turns:
            if event["actor_id"] != "agent_main":
                return  # Child actors cannot create a primary acceptance turn.
            if len(self.turns) >= 4:
                raise SoakFailure("unexpected fifth turn; possible replay")
            self.turns[turn_id] = {"turn_id": turn_id, **{key: event[key] for key in scope_keys},
                                   "starts": 0, "deltas": 0, "messages": [], "terminal": None}
        turn = self.turns[turn_id]
        if any(turn[key] != event[key] for key in scope_keys[:-1]):
            raise SoakFailure("turn identity crossed workspace/session/run scope")
        if event["actor_id"] != turn["actor_id"]:
            return  # Legitimate child events remain in the raw journal only.
        self._accept_turn(turn, event)

    @staticmethod
    def _accept_turn(turn: dict, event: dict) -> None:
        kind = event["kind"]
        if turn["terminal"] is not None and kind in {
                "turn.started", "message.delta", "message.completed"}:
            raise SoakFailure("turn received mutable evidence after its terminal event")
        if kind.startswith("message.") and turn["starts"] != 1:
            raise SoakFailure("assistant message arrived before its turn started")
        if kind == "turn.started":
            ProviderJournal._accept_start(turn)
        elif kind == "message.delta":
            ProviderJournal._accept_delta(turn, event)
        elif kind == "message.completed":
            ProviderJournal._accept_message(turn, event)
        elif kind in TERMINAL_KINDS:
            ProviderJournal._accept_terminal(turn, event)

    @staticmethod
    def _accept_start(turn: dict) -> None:
        turn["starts"] += 1
        if turn["starts"] != 1:
            raise SoakFailure("turn started more than once")

    @staticmethod
    def _accept_delta(turn: dict, event: dict) -> None:
        text = event["payload"].get("text", "")
        if not isinstance(text, str) or len(text) > 65536:
            raise SoakFailure("assistant delta is not bounded text")
        turn["deltas"] += int(bool(text))

    @staticmethod
    def _accept_message(turn: dict, event: dict) -> None:
        text = event["payload"].get("text", "")
        if not isinstance(text, str) or len(text) > 65536 or len(turn["messages"]) >= 8:
            raise SoakFailure("assistant response exceeded evidence bounds")
        turn["messages"].append(text)

    @staticmethod
    def _accept_terminal(turn: dict, event: dict) -> None:
        if turn["terminal"] is not None:
            raise SoakFailure("turn has multiple terminal events")
        turn["terminal"] = {
            "kind": event["kind"], "event_id": event["event_id"],
            "state": event["payload"].get("state", ""),
            "reason": event["payload"].get("reason", ""),
        }


def validate_success(turn: dict, expected: str) -> None:
    terminal = turn["terminal"] or {}
    if terminal.get("kind") != "turn.completed" or terminal.get("state") != "SUCCEEDED":
        raise SoakFailure("provider did not confirm a successful turn")
    if turn["starts"] != 1 or not turn["messages"] or turn["messages"][-1].strip() != expected:
        raise SoakFailure("successful turn did not return the exact requested marker")


def validate_cancel(turn: dict) -> None:
    terminal = turn["terminal"] or {}
    confirmed = terminal.get("kind") == "turn.interrupted" and terminal.get("state") == "INTERRUPTED"
    unknown = (terminal.get("kind") == "turn.failed" and terminal.get("state") == "UNKNOWN"
               and "cancel" in terminal.get("reason", "").lower())
    if turn["starts"] != 1 or not turn["deltas"] or not turn.get("cancel_requested"):
        raise SoakFailure("cancellation lacks an observed active model response")
    if not (confirmed or unknown):
        raise SoakFailure("cancellation was not confirmed or honestly recorded UNKNOWN")


def write_report(path: Path, report: dict) -> None:
    import json
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
