"""Linux process, terminal and journal evidence for the deterministic TUI soak."""

from __future__ import annotations

from collections import Counter
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import select
import shlex
import signal
import sqlite3
import stat
import subprocess
import sys
import time

from tui_soak_terminal import ComposerObservation
from tui_soak_watch import JournalHints


class SoakFailure(RuntimeError):
    """A required sustained TUI operation or its evidence failed."""


def append_json(path: Path, value: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, sort_keys=True) + "\n")


def executable_evidence(command: list[str]) -> dict:
    path = Path(command[0]).resolve()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(path), "sha256": digest.hexdigest(), "command": command}


def prepare_environment(root: Path) -> tuple[dict[str, str], Path]:
    """Allowlist the child environment; user credentials and homes are excluded."""
    for name in ("workspace", "home", "runtime", "tmp", "bin"):
        (root / name).mkdir()
    fixture = Path(__file__).with_name("tui_soak_provider.py").resolve()
    provider = root / "bin" / "pi"
    provider.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(fixture)])
                        + ' "$@"\n', encoding="utf-8")
    provider.chmod(0o755)
    environment = {
        "PATH": str(root / "bin"), "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "home" / "config"),
        "XDG_CACHE_HOME": str(root / "home" / "cache"),
        "XDG_DATA_HOME": str(root / "home" / "data"),
        "PBATCH_HOME": str(root / "home" / "pbatch"),
        "PBATCH_RUNTIME_HOME": str(root / "runtime"),
        "PBATCH_SCRIPT_DIR": str(root / "workspace"),
        "TMPDIR": str(root / "tmp"), "TERM": "xterm-256color",
        "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
        "TUI_SOAK_PROVIDER_LOG": str(root / "provider.jsonl"),
        "TUI_SOAK_RELEASE_FILE": str(root / "provider-release.json"),
    }
    return environment, provider


def process_identity(pid: int) -> dict | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
        fields = stat[stat.rfind(")") + 2:].split()
        return {"pid": pid, "state": fields[0], "ppid": int(fields[1]),
                "start_ticks": int(fields[19])}
    except (OSError, ValueError, IndexError):
        return None


def is_alive(identity: dict) -> bool:
    current = process_identity(identity["pid"])
    return bool(current and current["start_ticks"] == identity["start_ticks"]
                and current["state"] != "Z")


def resource_sample(identity: dict) -> dict:
    if not is_alive(identity):
        raise SoakFailure("the original TUI process exited or its PID was reused")
    path = Path("/proc") / str(identity["pid"])
    status = dict(line.split(":", 1) for line in (path / "status").read_text().splitlines())
    return {"pid": identity["pid"], "start_ticks": identity["start_ticks"],
            "rss_kib": int(status["VmRSS"].split()[0]),
            "fds": len(list((path / "fd").iterdir())),
            "threads": int(status["Threads"])}


class JsonTail:
    """Incremental bounded JSONL reading; a live partial tail is never consumed."""

    def __init__(self, path: Path):
        self.path, self.offset = path, 0

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        if self.path.stat().st_size < self.offset:
            raise SoakFailure(f"evidence was truncated: {self.path}")
        values = []
        with self.path.open("rb") as handle:
            handle.seek(self.offset)
            started = self.offset
            while len(values) < 128 and handle.tell() - started < 1024 * 1024:
                raw = handle.readline(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise SoakFailure(f"oversized evidence record: {self.path}")
                if not raw or not raw.endswith(b"\n"):
                    break
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise SoakFailure(f"invalid evidence JSON: {self.path}") from exc
                if not isinstance(value, dict):
                    raise SoakFailure(f"non-object evidence: {self.path}")
                values.append(value)
                self.offset = handle.tell()
        return values


class JournalEvidence:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.tails, self.counts = {}, Counter()
        self.latest = {}
        self.duplicates = self.regressions = 0
        self.database = sqlite3.connect(root.parent / "journal-index.sqlite")
        self.database.execute("PRAGMA cache_size=-1024")
        self.database.execute("PRAGMA synchronous=OFF")  # Rebuildable index; raw journals are authoritative.
        self.database.executescript(
            "CREATE TABLE events (id TEXT PRIMARY KEY, session TEXT, run TEXT, seq INTEGER);"
            "CREATE INDEX sequence_lookup ON events (session, run, seq);"
            "CREATE TABLE files (path TEXT PRIMARY KEY, bytes INTEGER, verified INTEGER DEFAULT 0);")
        self._scan = self._entries()
        self._hints = JournalHints(self.root, SoakFailure)

    def _entries(self):
        if not self.root.exists():
            return
        pending = [os.scandir(self.root)]
        try:
            while pending:
                entry = next(pending[-1], None)
                if entry is None:
                    pending.pop().close()
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if len(pending) >= 32:
                        raise SoakFailure("journal evidence exceeded its directory-depth limit")
                    pending.append(os.scandir(entry.path))
                yield Path(entry.path) if entry.name == "events.jsonl" else None
        finally:
            for iterator in pending:
                iterator.close()

    def _discover(self) -> None:
        for _ in range(128):
            try:
                path = next(self._scan)
            except StopIteration:
                self._scan = self._entries()
                return
            if path is None or path in self.tails:
                continue
            self._follow(path)

    def _follow(self, path: Path) -> JsonTail | None:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.parent.is_symlink():
            raise SoakFailure("journal evidence must be a local regular file")
        if path in self.tails:
            return self.tails[path]
        row = self.database.execute("SELECT bytes FROM files WHERE path=?", (str(path),)).fetchone()
        offset = row[0] if row else 0
        if info.st_size < offset:
            raise SoakFailure(f"evidence was truncated: {path}")
        if row and info.st_size == offset:
            if offset:
                self._hints.acknowledge(path)
            return None
        if len(self.tails) == 4:
            self.tails.pop(next(iter(self.tails)))
        tail = JsonTail(path)
        tail.offset = offset
        self.tails[path] = tail
        return tail

    def poll(self) -> None:
        for path in self._hints.poll():
            if path.exists():
                tail = self._follow(path)
                if tail is not None:
                    self._read_tail(tail)
        self._discover()
        for tail in self.tails.values():
            self._read_tail(tail)
        self.database.commit()

    def _read_tail(self, tail: JsonTail, *, follow: bool = True) -> None:
        for event in tail.read():
            self._accept(event)
        self.database.execute("INSERT OR REPLACE INTO files (path, bytes) VALUES (?, ?)",
                              (str(tail.path), tail.offset))
        if tail.offset:
            if follow:
                self._hints.observe(tail.path)
            self._hints.acknowledge(tail.path)

    def _accept(self, event: dict) -> None:
        strings = ("event_id", "session_id", "run_id", "kind")
        if (any(not isinstance(event.get(key), str) for key in strings)
                or not isinstance(event.get("payload"), dict)):
            raise SoakFailure("journal contains an incomplete runtime event")
        event_id, seq = event["event_id"], event.get("seq")
        key = (event["session_id"], event["run_id"])
        if self.database.execute("SELECT 1 FROM events WHERE id=?", (event_id,)).fetchone():
            self.duplicates += 1
            raise SoakFailure("journal contains a duplicate event identity")
        previous = self.database.execute(
            "SELECT MAX(seq) FROM events WHERE session=? AND run=?", key).fetchone()[0] or 0
        if type(seq) is not int or seq <= previous:
            self.regressions += 1
            raise SoakFailure("journal event sequence regressed")
        if event["kind"] not in self.counts and len(self.counts) >= 64:
            raise SoakFailure("too many journal event kinds")
        self.database.execute("INSERT INTO events VALUES (?, ?, ?, ?)", (event_id, *key, seq))
        self.counts[event["kind"]] += 1
        self.latest[event["kind"]] = event

    def summary(self) -> dict:
        sessions = self.database.execute("SELECT COUNT(DISTINCT session) FROM events").fetchone()[0]
        files, total_bytes = self.database.execute("SELECT COUNT(*), SUM(bytes) FROM files").fetchone()
        return {"sessions": sessions, "events": sum(self.counts.values()),
                "files": files, "bytes": total_bytes or 0,
                "kinds": dict(self.counts), "duplicate_ids": self.duplicates,
                "sequence_regressions": self.regressions}

    def finish(self, deadline: float) -> None:
        """After the writer exits, consume every file, including undiscovered ones."""
        self._scan.close()
        self.database.execute("UPDATE files SET verified=0")
        with closing(self._entries()) as entries:
            for path in entries:
                if time.monotonic() > deadline:
                    raise SoakFailure("final journal verification exceeded its deadline")
                if path is not None:
                    self._finish_file(path, deadline)
        missing = self.database.execute("SELECT path FROM files WHERE verified=0 LIMIT 1").fetchone()
        if missing:
            raise SoakFailure(f"journal disappeared before final verification: {missing[0]}")
        for name, offset in self.database.execute("SELECT path, bytes FROM files"):
            if time.monotonic() > deadline:
                raise SoakFailure("final journal verification exceeded its deadline")
            if Path(name).stat().st_size != offset:
                raise SoakFailure(f"journal has unconsumed or partial records: {name}")
        self.database.commit()

    def _finish_file(self, path: Path, deadline: float) -> None:
        tail = self._follow(path)
        if tail is not None:
            while True:
                if time.monotonic() > deadline:
                    raise SoakFailure("final journal verification exceeded its deadline")
                before = tail.offset
                self._read_tail(tail, follow=False)
                if path.stat().st_size == tail.offset:
                    break
                if tail.offset == before:
                    raise SoakFailure(f"journal has unconsumed or partial records: {path}")
        self.database.execute("UPDATE files SET verified=1 WHERE path=?", (str(path),))

    def close(self) -> None:
        self._scan.close()
        self._hints.close()
        self.database.close()


class Terminal:
    def __init__(self, command: list[str], root: Path, env: dict, provider: Path, timeout: int,
                 *, runtime: str = "pi-rpc"):
        import fcntl
        import pty
        import struct
        import termios
        self.master, slave = pty.openpty()
        self.tail, self.output_bytes, self.saved_bytes = bytearray(), 0, 0
        self.root, self.composer = root, ComposerObservation()
        self.log = (root / "terminal.log").open("wb")
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 140, 0, 0))
        try:
            self.process = subprocess.Popen(
                [*command, "tui", "--runtime", runtime, "--agent-bin", str(provider),
                 "--timeout", str(timeout), "--memory-mode", "off"],
                cwd=root / "workspace", env=env, stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True)
        except Exception:
            os.close(self.master)
            self.log.close()
            raise
        finally:
            os.close(slave)

    def pump(self, seconds: float = 0.05) -> None:
        ready, _, _ = select.select([self.master], [], [], seconds)
        if not ready:
            return
        chunks, total = [], 0
        while ready and total < 256 * 1024:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                break  # PTY raises EIO after the child closes its terminal.
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            ready, _, _ = select.select([self.master], [], [], 0)
        self._record_output(b"".join(chunks))

    def _record_output(self, chunk: bytes) -> None:
        self.composer.feed(chunk)
        self.output_bytes += len(chunk)
        self.tail.extend(chunk)
        del self.tail[:-128 * 1024]
        saved = chunk[:max(0, 8 * 1024 * 1024 - self.saved_bytes)]
        self.log.write(saved)
        self.log.flush()
        self.saved_bytes += len(saved)

    def send(self, text: str | bytes) -> None:
        os.write(self.master, text.encode() + b"\r" if isinstance(text, str) else text)

    def write(self, text: str) -> None:
        """Type without submitting, so the caller can confirm the echo first."""
        os.write(self.master, text.encode())

    def composer_text(self) -> str:
        """The currently painted composer row (the observation's last row)."""
        return "".join(self.composer.cells)

    def close(self) -> None:
        try:
            (self.root / "terminal-tail.log").write_bytes(self.tail)
        finally:
            self.log.close()
            os.close(self.master)


def kill_owned_process(identity: dict) -> None:
    if not is_alive(identity):
        return
    try:
        os.killpg(identity["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass


def kill_tui_process(launcher: dict | None, tui: dict | None) -> None:
    if launcher:
        kill_owned_process(launcher)
    if tui and is_alive(tui):
        try:
            os.kill(tui["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass


def resource_growth(samples: list[dict], max_fds: int, max_rss_mib: float) -> dict:
    baseline, final = samples[0], samples[-1]
    growth = {key: max(item[key] for item in samples) - baseline[key]
              for key in ("fds", "rss_kib", "threads")}
    if growth["fds"] > max_fds:
        raise SoakFailure(f"TUI FD growth exceeded allowance: {growth['fds']} > {max_fds}")
    if growth["rss_kib"] > max_rss_mib * 1024:
        raise SoakFailure(f"TUI RSS growth exceeded allowance: {growth['rss_kib']} KiB")
    return {"baseline": baseline, "final": final, "peak_growth": growth,
            "allowed_fd_growth": max_fds, "allowed_rss_growth_mib": max_rss_mib}
