"""Real executable instance-lock handshakes, isolated from application imports."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import uuid


class LockSmokeFailure(RuntimeError):
    """An executable violated a normal instance-lock contract."""


class _Process:
    def __init__(self, command, workspace, environment, stop):
        self.directory = tempfile.TemporaryDirectory(prefix="pi-batch-lock-process-")
        self.paths = [Path(self.directory.name) / name for name in ("stdout", "stderr")]
        self.stdout, self.stderr = [path.open("wb") for path in self.paths]
        self.stop = stop
        try:
            self.process = subprocess.Popen(
                command, cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
                stdout=self.stdout, stderr=self.stderr,
                start_new_session=os.name == "posix")
        except BaseException:
            self.stdout.close()
            self.stderr.close()
            self.directory.cleanup()
            raise

    def diagnostics(self):
        result = []
        for path in self.paths:
            if path.stat().st_size > 1024 * 1024:
                raise LockSmokeFailure("lock subprocess diagnostics exceeded 1 MiB")
            with path.open("rb") as stream:
                result.append(stream.read(1024 * 1024 + 1).decode("utf-8", "replace"))
        return "".join(result)

    def require_running(self):
        if self.process.poll() is not None:
            raise LockSmokeFailure(f"lock subprocess exited early: {self.process.returncode}\n"
                                   + self.diagnostics()[-6000:])

    def finish(self, timeout):
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise LockSmokeFailure("lock subprocess exceeded completion deadline") from exc
        if self.process.returncode != 0:
            raise LockSmokeFailure(f"lock subprocess failed: {self.process.returncode}\n"
                                   + self.diagnostics()[-6000:])
        self.diagnostics()

    def close(self):
        try:
            self.stop(self.process)
        finally:
            self.stdout.close()
            self.stderr.close()
            self.directory.cleanup()


class _LockSmoke:
    def __init__(self, command, workspace, environment, timeout, flags,
                 execute, verify, record, stop):
        self.command, self.workspace, self.timeout = command, workspace, timeout
        self.flags = list(flags)
        self.flags[self.flags.index("--timeout") + 1] = str(max(30, math.ceil(timeout * 3)))
        self.execute, self.verify, self.record, self.stop = execute, verify, record, stop
        self.root = workspace.parent / "lock-handshakes"
        self.root.mkdir()
        self.environment = {**environment, "BINARY_SMOKE_LOCK_ROOT": str(self.root),
                            "BINARY_SMOKE_HOLD_SECONDS": str(max(30, timeout * 3))}
        self.children, self.holder, self.identity = [], None, None

    def run(self):
        try:
            self.holder = self._start("holder", "held.json", "smoke-lock")
            self.identity = self._wait(lambda: self._event("holder", "ready"), "holder ready")
            self._check("instance lock output conflict", lambda: self._conflict(
                "output-conflict", "held.json", "other-session"))
            self._check("instance lock session conflict", lambda: self._conflict(
                "session-conflict", "session-conflict.json", "smoke-lock"))
            self._check("instance lock read-only ps", self._check_ps)
            self._check("instance lock queued handoff and release", self._handoff)
        finally:
            self._cleanup()

    def _check(self, label, action):
        started = time.monotonic()
        try:
            action()
        except Exception as exc:
            self.record(label, "failed", seconds=time.monotonic() - started, reason=str(exc))
            raise
        self.record(label, "passed", seconds=time.monotonic() - started)

    def _argv(self, output, session, *extra):
        return [*self.command, "-p", "smoke-valid", "-o", output,
                *self.flags, "--session-name", session, *extra]

    def _env(self, role):
        return {**self.environment, "BINARY_SMOKE_LOCK_ROLE": role}

    def _start(self, role, output, session, *extra):
        child = _Process(self._argv(output, session, *extra), self.workspace,
                         self._env(role), self.stop)
        self.children.append(child)
        return child

    def _event(self, role, kind):
        try:
            value = json.loads((self.root / f"{role}.{kind}.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, dict) or value.get("role") != role:
            raise LockSmokeFailure("invalid provider handshake")
        return value

    def _control(self, action, token=""):
        path = self.root / "holder.control.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"action": action, "token": token}), encoding="utf-8")
        temporary.replace(path)

    def _wait(self, predicate, label, *, holder=True, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        pause = threading.Event()
        while time.monotonic() < deadline:
            if holder:
                self.holder.require_running()
            result = predicate()
            if result:
                return result
            pause.wait(min(0.02, max(0, deadline - time.monotonic())))
        raise LockSmokeFailure(f"deadline waiting for {label}")

    def _ping(self):
        token = uuid.uuid4().hex
        self._control("ping", token)
        def matching_reply():
            reply = self._event("holder", "pong")
            return reply if reply and reply.get("token") == token else None
        reply = self._wait(matching_reply, "live provider response")
        if reply["pid"] != self.identity["pid"] or reply["parent_pid"] != self.identity["parent_pid"]:
            raise LockSmokeFailure("holder or provider identity changed during lock probe")

    def _conflict(self, role, output, session):
        self.execute(self._argv(output, session), self.workspace, self._env(role),
                     self.timeout, role, (5,))
        if self._event(role, "ready") or (self.workspace / output).exists():
            raise LockSmokeFailure("conflicting task started a provider or published an artifact")
        self._ping()

    def _ps(self):
        output = self.execute([*self.command, "ps", "--json"], self.workspace,
                              self.environment, self.timeout, "ps", (0,))
        runs = json.loads(output)
        if not isinstance(runs, list):
            raise LockSmokeFailure("ps did not return a run list")
        return runs

    def _check_ps(self):
        holder = [row for row in self._ps() if row.get("pid") == self.identity["parent_pid"]]
        if len(holder) != 1 or holder[0].get("status") != "running":
            raise LockSmokeFailure("ps did not preserve the running holder's identity and status")
        self._ping()

    def _handoff(self):
        waiter = self._start("waiter", "waited.json", "smoke-lock", "--wait-lock", "1")
        def queued():
            waiter.require_running()
            if self._event("waiter", "ready"):
                raise LockSmokeFailure("wait-lock provider started before the lock was released")
            return "LOCK: waiting for holder" in waiter.diagnostics()
        self._wait(queued, "waiter lock queue")
        waiter.require_running()
        rows = [row for row in self._ps() if row.get("session") == "smoke-lock"
                and row.get("pid") != self.identity["parent_pid"]]
        if len(rows) != 1 or rows[0].get("status") != "queued" or self._event("waiter", "ready"):
            raise LockSmokeFailure("wait-lock started work before the holder released its lock")
        self._ping()
        self._control("release")
        self.holder.finish(self.timeout)
        waiter.finish(self.timeout)
        for role, output in (("holder", "held.json"), ("waiter", "waited.json")):
            self.verify(self.workspace / output)
            if not self._event(role, "done"):
                raise LockSmokeFailure("provider did not finish its handshake")
        (self.workspace / "held.json").unlink()
        self.execute(self._argv("held.json", "smoke-lock"), self.workspace,
                     self._env("reuse"), self.timeout, "released lock reuse", (0,))
        self.verify(self.workspace / "held.json")
        if not self._event("reuse", "done"):
            raise LockSmokeFailure("released lock could not complete another provider turn")

    def _cleanup(self):
        failure = None
        try:
            self._control("release")
            if self._event("holder", "ready"):
                self._wait(lambda: self._event("holder", "done"), "fixture shutdown",
                           holder=False, timeout=2)
        except Exception as exc:
            failure = exc
        finally:
            for child in reversed(self.children):
                try:
                    child.close()
                except Exception as exc:
                    failure = failure or exc
        if failure:
            raise failure


def check_instance_locks(command, workspace, environment, timeout, **hooks):
    _LockSmoke(command, workspace, environment, timeout, **hooks).run()
