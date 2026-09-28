#!/usr/bin/env python3
"""Finite Linux batch cancellation evidence using local commands, never models."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
CASES = ("stage_parallel", "validator_serial", "validator_parallel", "stage_parallel_sigint")
FIXTURE = """import json, os, pathlib, subprocess, sys, time
root, label = pathlib.Path(sys.argv[1]), sys.argv[2]
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
record = {'pid': os.getpid(), 'child': child.pid, 'pgid': os.getpgrp()}
temporary = root / (label + '.starting')
temporary.write_text(json.dumps(record))
temporary.rename(root / (label + '.started.json'))
time.sleep(2)
(root / (label + '.effect')).write_text('unexpected work after cancellation')
child.wait()
"""


def _live(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


def _records(root: Path) -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted(root.glob("*.started.json"))]


def _live_owned(records: list[dict]) -> list[int]:
    return [pid for record in records for pid in (record["pid"], record["child"]) if _live(pid)]


def _prepare(root: Path, entry: list[str], case: str) -> tuple[list[str], dict, int]:
    root.mkdir(parents=True, exist_ok=False)
    fixture = root / "command.py"
    fixture.write_text(FIXTURE)
    parallel_stage = case.startswith("stage_parallel")
    count = 2 if parallel_stage else (7 if case == "validator_parallel" else 1)
    commands = [shlex.join([sys.executable, str(fixture), str(root), f"command-{i}"])
                for i in range(count)]
    common = [*entry, "--agent-bin", "/bin/echo", "--memory-mode", "off",
              "--data-dir", str(root / "runtime"), "--stream-output", "none", "--timeout", "30"]
    if parallel_stage:
        pipeline = root / "pipeline.yaml"
        pipeline.write_text(json.dumps({"stages": [{
            "name": "cancellation", "from_prompt": "offline fixture",
            "output": str(root / "artifact.md"), "commands": commands,
            "commands_parallel": True, "command_timeout": 30}]}))
        arguments = ["--pipeline", str(pipeline)]
    else:
        arguments = ["-p", "offline fixture", "-o", str(root / "artifact.md"),
                     "--validate", ",".join(commands)]
    environment = dict(os.environ, PBATCH_RUNTIME_HOME=str(root / "runtime-home"),
                       PBATCH_DATA_DIR=str(root / "runtime"))
    environment.pop("PBATCH_LOCK_FILE", None)
    return common + arguments, environment, min(count, 4)


def _wait_ready(process, root: Path, count: int) -> bool:
    deadline = time.monotonic() + 10
    while process.poll() is None and time.monotonic() < deadline:
        if len(_records(root)) == count:
            return True
        time.sleep(.01)
    return False


def _observe_shutdown(process, root: Path, signum: int) -> dict:
    started = time.monotonic()
    process.send_signal(signum)
    premature_release = False
    deadline = started + 5
    lock = root / "runtime" / "locks" / ".pi-batch.lock"
    while process.poll() is None and time.monotonic() < deadline:
        if not lock.exists() and _live_owned(_records(root)):
            premature_release = True
        time.sleep(.005)
    records = _records(root)
    return {"exit_code": process.poll(), "elapsed_seconds": round(time.monotonic() - started, 3),
            "lock_released_while_children_alive": premature_release,
            "live_owned_processes": _live_owned(records), "started_commands": len(records),
            "post_signal_effects": [path.name for path in root.glob("*.effect")],
            "temporary_artifacts": [path.name for path in root.glob("artifact.md.*.tmp")],
            "lock_present": lock.exists()}


def _restart(root: Path, entry: list[str], environment: dict) -> int:
    process = subprocess.Popen(
        [*entry, "-p", "explicit recovery fixture", "-o", str(root / "restarted.md"),
         "--agent-bin", "/bin/echo", "--memory-mode", "off", "--stream-output", "none",
         "--data-dir", str(root / "runtime")], cwd=root, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)
    (root / "restart.log").write_bytes(output)
    return process.returncode


def _cleanup(process, root: Path) -> bool:
    needed = process.poll() is None or bool(_live_owned(_records(root)))
    for record in _records(root):
        try:
            os.killpg(record["pgid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=3)
    return needed


def run_case(root: Path, entry: list[str], case: str) -> dict:
    command, environment, expected = _prepare(root, entry, case)
    report = {"case": case, "ok": False}
    with (root / "run.log").open("wb") as log:
        process = subprocess.Popen(command, cwd=root, env=environment,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            if not _wait_ready(process, root, expected):
                raise RuntimeError("fixture did not reach its active commands")
            signum = signal.SIGINT if case.endswith("sigint") else signal.SIGTERM
            report.update(_observe_shutdown(process, root, signum))
            report["signal"] = signal.Signals(signum).name
            report["restart_exit"] = _restart(root, entry, environment)
            report["ok"] = _accepted(root, report, expected)
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            report["error"] = str(exc)
        finally:
            report["forced_fixture_cleanup"] = _cleanup(process, root)
            report["ok"] = report["ok"] and not report["forced_fixture_cleanup"]
    return report


def _accepted(root: Path, report: dict, expected: int) -> bool:
    rejected_artifact = (report["case"].startswith("stage_")
                         or not (root / "artifact.md").exists())
    no_command_success = "CMD OK" not in (root / "run.log").read_text()
    return (report["exit_code"] == 130 and report["restart_exit"] == 0
            and report["started_commands"] == expected and rejected_artifact and no_command_success
            and not any(report[key] for key in (
                "lock_released_while_children_alive", "live_owned_processes",
                "post_signal_effects", "temporary_artifacts", "lock_present")))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--case", choices=CASES, action="append")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("this finite process/lock check requires Linux /proc")
    entry = ([str(args.binary.resolve())] if args.binary else
             [sys.executable, str(ROOT / "pi-batch.py")])
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = [run_case(output / case, entry, case) for case in (args.case or CASES)]
    report = {"schema_version": 1, "scope": "local batch cancellation fixtures; no model calls",
              "entry": entry, "ok": all(result["ok"] for result in results), "cases": results}
    if args.binary:
        report["binary_sha256"] = hashlib.sha256(args.binary.read_bytes()).hexdigest()
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
