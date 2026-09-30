#!/usr/bin/env python3
"""Exercise a standalone executable outside its source checkout, without a model.

Python runs this harness and a deterministic fake provider by absolute path.
The application receives an empty PATH: its bundled gates must run using the
embedded interpreter. Git isolation is checked separately with Git available.
"""

from __future__ import annotations

import argparse
from contextvars import ContextVar
import hashlib
import json
import math
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from binary_smoke_lock import check_instance_locks
from binary_smoke_yaml import check_yaml_inputs

OUTPUT_LIMIT = 1024 * 1024
CHECK_STEPS = ("quality", "registry", "refusal", "org", "capabilities",
               "graph", "tools", "knowledge", "eval")
_REPORT = ContextVar("binary_smoke_report", default=None)


class SmokeFailure(RuntimeError):
    """A required executable contract failed."""


def record_step(label: str, status: str, *, seconds: float = 0, reason: str = "") -> None:
    reason = reason[:6000]
    report = _REPORT.get()
    if report is not None:
        report["checks"].append({"name": label, "status": status,
                                 "seconds": round(seconds, 3), "reason": reason})
    heading = {"passed": "PASS", "failed": "FAIL", "skipped": "SKIP"}[status]
    print(f"{heading} {label}" + (f": {reason}" if reason else ""), flush=True)


def binary_identity(executable: Path) -> dict:
    digest = hashlib.sha256()
    with executable.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(executable), "sha256": digest.hexdigest(),
            "bytes": executable.stat().st_size}


def isolated_environment(root: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PBATCH_", "PYTHON", "_PYI_", "GIT_"))}
    empty_path = root / "empty-path"
    empty_path.mkdir()
    extraction = root / "extraction with spaces"
    extraction.mkdir()
    home = root / "home"
    home.mkdir()
    env.update(PATH=str(empty_path), PYTHONPATH="", PYTHONNOUSERSITE="1",
               TMPDIR=str(extraction), TEMP=str(extraction), TMP=str(extraction),
               HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(home / "codex"),
               XDG_CONFIG_HOME=str(home / "config"), XDG_DATA_HOME=str(home / "data"),
               XDG_CACHE_HOME=str(home / "cache"), APPDATA=str(home / "AppData/Roaming"),
               LOCALAPPDATA=str(home / "AppData/Local"),
               PBATCH_HOME=str(root / "user"),
               PBATCH_RUNTIME_HOME=str(root / "runtime"),
               PBATCH_SCRIPT_DIR=str(root / "workspace"),
               TERM="xterm-256color", NO_COLOR="1", PYTHONUTF8="1")
    return env


def _stop_process(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        if process.poll() is not None:
            return
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/taskkill.exe"
        try:
            subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
        if process.poll() is None:
            process.kill()
    process.wait(timeout=5)


def run_command(command: list[str], cwd: Path, env: dict[str, str],
                timeout: float, label: str, expected: tuple[int, ...] = (0,),
                validate: Callable[[str], None] | None = None, *,
                input_bytes: bytes | None = None, include_stderr: bool = False) -> str:
    started = time.monotonic()
    try:
        options = ({"input_bytes": input_bytes, "include_stderr": include_stderr}
                   if input_bytes is not None or include_stderr else {})
        output = _command_output(command, cwd, env, timeout, label, expected, **options)
        if validate is not None:
            validate(output)
    except Exception as exc:
        record_step(label, "failed", seconds=time.monotonic() - started, reason=str(exc))
        raise
    record_step(label, "passed", seconds=time.monotonic() - started)
    return output


def _command_output(command: list[str], cwd: Path, env: dict[str, str],
                    timeout: float, label: str, expected: tuple[int, ...], *,
                    input_bytes: bytes | None = None, include_stderr: bool = False) -> str:
    """Bound wall time and diagnostics, including commands with noisy output."""
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(command, cwd=cwd, env=env,
                                       stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr,
                                       start_new_session=os.name == "posix")
        except OSError as exc:
            raise SmokeFailure(f"{label}: cannot start: {exc}") from exc
        try:
            if input_bytes is None:
                process.wait(timeout=timeout)
            else:
                process.communicate(input=input_bytes, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _stop_process(process)
            raise SmokeFailure(f"{label}: exceeded {timeout:g}s deadline") from exc
        stdout.seek(0)
        stderr.seek(0)
        output, errors = stdout.read(OUTPUT_LIMIT + 1), stderr.read(OUTPUT_LIMIT + 1)
    if max(len(output), len(errors)) > OUTPUT_LIMIT:
        raise SmokeFailure(f"{label}: diagnostics exceeded {OUTPUT_LIMIT} bytes")
    if process.returncode not in expected:
        tail = (output + errors).decode("utf-8", "replace")[-6000:]
        raise SmokeFailure(f"{label}: exit {process.returncode}, expected {expected}\n{tail}")
    return (output + errors if include_stderr else output).decode("utf-8", "replace")


def _json_object(output: str, label: str) -> dict:
    try:
        value = json.loads(output)
    except ValueError as exc:
        raise SmokeFailure(f"{label}: invalid JSON output") from exc
    if not isinstance(value, dict):
        raise SmokeFailure(f"{label}: expected a JSON object")
    return value


def check_health(output: str) -> None:
    """Do not confuse absent source tests or a lower score with missing assets."""
    report = _json_object(output, "health")
    assets = report.get("assets", {})
    if not isinstance(assets, dict) or assets.get("healthy") is not True or assets.get("missing") != []:
        raise SmokeFailure("health: bundled assets missing or unknown")
    checkers = report.get("checkers", {})
    required = {"designintelligence", "knowledge", "backendquality", "packaged_assets"}
    if not isinstance(checkers, dict) or not required.issubset(checkers):
        raise SmokeFailure("health: required checker results missing")
    probes = {**checkers, "quality": report.get("quality_violations")}
    unknown = [name for name, value in probes.items()
               if type(value) is not int or value < 0]
    if unknown:
        raise SmokeFailure("health: unknown checker results: " + ", ".join(unknown))


def prepare_fake_agent(workspace: Path, config_output: str) -> None:
    """Reuse the binary's own validator registration, overriding only the agent."""
    report = _json_object(config_output, "config explain")
    registered = report.get("effective_file_config", {}).get("validators", {})
    if not isinstance(registered, dict) or not registered.get("refusalcheck"):
        raise SmokeFailure("config explain: bundled refusalcheck is missing")
    agent = workspace / "fake-provider.py"
    agent.write_text(Path(__file__).with_name("binary_smoke_provider.py").read_text(
        encoding="utf-8"), encoding="utf-8")
    config = {"config_version": 1, "agent": {
        "bin": sys.executable, "default_model": "", "default_timeout": 15,
        "argument_flags": {"prompt": [str(agent)]},
        "session_flags": {"start": [], "continue": [], "fork": []}},
        "validators": {"refusalcheck": registered["refusalcheck"]}}
    (workspace / "pi-batch.yaml").write_text(json.dumps(config), encoding="utf-8")
    schema = {"type": "object", "required": ["ok"],
              "properties": {"ok": {"const": True}}, "additionalProperties": False}
    (workspace / "result.schema.json").write_text(json.dumps(schema), encoding="utf-8")


def _task_flags() -> list[str]:
    return ["--agent-bin", sys.executable, "--memory-mode", "off",
            "--stream-output", "none", "--timeout", "15", "--max-rounds", "1",
            "--retries", "0", "--log-file", ""]


def check_artifact(path: Path) -> None:
    if not path.is_file() or _json_object(path.read_text(encoding="utf-8"), str(path)) != {"ok": True}:
        raise SmokeFailure(f"unexpected or missing artifact: {path}")


def check_rejection(path: Path) -> None:
    if path.exists():
        raise SmokeFailure("schema rejection left a published artifact")


def check_tasks(command: list[str], workspace: Path, env: dict, timeout: float) -> None:
    schema = workspace / "result.schema.json"
    validators = "refusalcheck,jsonschema=" + str(schema)
    common = [*_task_flags(), "--validate", validators]
    run_command([*command, "-p", "smoke-valid", "-o", "result.json", *common],
                workspace, env, timeout, "fake-provider and bundled validators",
                validate=lambda _output: check_artifact(workspace / "result.json"))
    run_command([*command, "-p", "smoke-invalid", "-o", "rejected.json", *common],
                workspace, env, timeout, "schema rejection", expected=(1,),
                validate=lambda _output: check_rejection(workspace / "rejected.json"))
    pipeline = {"stages": [
        {"name": "produce", "from_prompt": "smoke-valid", "output": "pipeline.json",
         "validate_cmd": validators}]}
    path = workspace / "pipeline.json.yaml"
    path.write_text(json.dumps(pipeline), encoding="utf-8")
    run_command([*command, str(path), *_task_flags()], workspace, env,
                timeout, "pipeline and bundled validators",
                validate=lambda _output: check_artifact(workspace / "pipeline.json"))


def _check_missing_gofmt_result(output: str, path: Path) -> None:
    if "VALIDATION FAILED" not in output or "gofmt" not in output or "Traceback" in output:
        raise SmokeFailure("missing gofmt did not produce a validator rejection")
    if path.exists() or list(path.parent.glob(path.name + ".*.tmp")):
        raise SmokeFailure("missing gofmt left a published or temporary artifact")


def check_missing_gofmt(command: list[str], workspace: Path, env: dict, timeout: float) -> None:
    output = workspace / "gofmt-rejected.go"
    run_command([*command, "-p", "smoke-valid", "-o", output.name,
                 *_task_flags(), "--validate", "gofmt"],
                workspace, env, timeout, "missing gofmt rejects artifact", expected=(1,),
                include_stderr=True,
                validate=lambda text: _check_missing_gofmt_result(text, output))


def _stdin_case(workspace: Path, environment: dict, name: str) -> tuple[Path, dict, list[str]]:
    root = workspace.parent / "stdin-checks" / name
    case_workspace = root / "workspace"
    case_workspace.mkdir(parents=True)
    agent = root / "unexpected-agent.py"
    agent.write_text("from pathlib import Path\nPath(" + repr(str(root / "agent-started"))
                     + ").write_text('started', encoding='utf-8')\nprint('{\"ok\": true}')\n",
                     encoding="utf-8")
    config = {"config_version": 1, "input": {"max_bytes": 32}, "agent": {
        "bin": sys.executable, "default_model": "", "argument_flags": {"prompt": [str(agent)]},
        "session_flags": {"start": [], "continue": [], "fork": []}}}
    (case_workspace / "pi-batch.yaml").write_text(json.dumps(config), encoding="utf-8")
    env = {**environment, "PBATCH_SCRIPT_DIR": str(case_workspace),
           "PBATCH_HOME": str(root / "user"), "PBATCH_RUNTIME_HOME": str(root / "runtime")}
    flags = ["--agent-bin", sys.executable, "--memory-mode", "off", "--stream-output", "none",
             "--timeout", "5", "--max-rounds", "1", "--retries", "0", "--log-file", str(root / "operation.log")]
    return case_workspace, env, flags


def _check_stdin_result(output: str, workspace: Path, reason: str) -> None:
    root = workspace.parent
    if (root / "agent-started").exists():
        raise SmokeFailure("stdin boundary unexpectedly started the provider")
    if not reason:
        lines = {line.strip() for line in output.splitlines()}
        if "Tasks: 1" not in lines or "[1] 中文🙂 binary-pipe..." not in lines:
            raise SmokeFailure("stdin UTF-8 dry-run did not preserve its one exact prompt")
        return
    if "Input rejected:" not in output or reason not in output or "Traceback" in output:
        raise SmokeFailure("stdin rejection did not produce its bounded input diagnostic")
    if "binary-smoke-private" in output:
        raise SmokeFailure("stdin rejection exposed the invalid input body")
    if (root / "operation.log").exists():
        raise SmokeFailure("stdin rejection created an operation log before rejecting input")
    if any(path.is_file() for path in (root / "runtime").rglob("*")):
        raise SmokeFailure("stdin rejection created runtime or lock state before rejecting input")


def check_stdin(command: list[str], workspace: Path, env: dict, timeout: float) -> None:
    cases = (("utf8", "stdin UTF-8 pipe dry-run", "中文🙂 binary-pipe".encode(), ""),
             ("oversized", "stdin oversized whitespace rejected before side effects", b" " * 33,
              "stdin prompt exceeds 32 bytes"),
             ("invalid", "stdin invalid UTF-8 rejected before side effects", b"binary-smoke-private-\xff",
              "stdin prompt is not valid UTF-8"))
    for name, label, payload, reason in cases:
        case_workspace, case_env, flags = _stdin_case(workspace, env, name)
        run_command([*command, *flags, *([] if reason else ["--dry-run"])],
                    case_workspace, case_env, timeout, label, expected=(2,) if reason else (0,),
                    input_bytes=payload, include_stderr=True,
                    validate=lambda output, cwd=case_workspace, message=reason:
                    _check_stdin_result(output, cwd, message))


def check_isolate(command: list[str], workspace: Path, env: dict, timeout: float) -> None:
    git = shutil.which("git")
    if not git:
        record_step("isolate", "skipped", reason="Git is not installed")
        return
    git_env = {**env, "PATH": os.environ.get("PATH", ""),
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    git_args = [git, "-c", "user.name=Binary Smoke", "-c", "user.email=smoke@example.invalid"]
    for args in (["init", "-q"], ["add", "pi-batch.yaml", "fake-provider.py", "result.schema.json"],
                 ["commit", "-qm", "Binary smoke fixture"]):
        run_command([*git_args, *args], workspace, git_env, timeout, "git " + args[0])
    worktree = workspace.parent / "isolated-worktree"
    run_command([*command, "isolate", "--branch", "binary-smoke", "--dir", str(worktree),
                 "--", "-p", "smoke-valid", "-o", "isolated.json", *_task_flags(),
                 "--validate", "refusalcheck,jsonschema=result.schema.json"],
                workspace, git_env, timeout, "isolated child invocation",
                validate=lambda _output: check_artifact(worktree / "isolated.json"))


def _drive_terminal(process: subprocess.Popen, terminal: int, timeout: float) -> None:
    import select
    deadline, output, quit_sent = time.monotonic() + timeout, bytearray(), False
    while process.poll() is None and time.monotonic() < deadline:
        readable, _, _ = select.select([terminal], [], [], min(0.1, max(0, deadline - time.monotonic())))
        if readable:
            try:
                chunk = os.read(terminal, 65536)
            except OSError:
                break
            output.extend(chunk)
            if len(output) > OUTPUT_LIMIT:
                raise SmokeFailure("tui: terminal output limit exceeded")
        if not quit_sent and b"Quit" in output and b"pi-batch" in output:
            os.write(terminal, b"\x11")
            quit_sent = True
    if process.poll() is None:
        try:
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise SmokeFailure("tui: startup/quit deadline exceeded") from exc
    if not quit_sent or process.returncode != 0:
        detail = bytes(output[-3000:]).decode("utf-8", "replace")
        raise SmokeFailure(f"tui: did not render and exit cleanly (exit {process.returncode})\n{detail}")


def check_tui(command: list[str], workspace: Path, env: dict, timeout: float) -> None:
    if os.name != "posix":
        record_step("tui PTY", "skipped", reason="POSIX pseudo-terminal unavailable on Windows")
        return
    started = time.monotonic()
    label = "tui PTY render and Ctrl+Q exit"
    try:
        _run_tui(command, workspace, env, timeout)
    except Exception as exc:
        record_step(label, "failed", seconds=time.monotonic() - started, reason=str(exc))
        raise
    record_step(label, "passed", seconds=time.monotonic() - started)


def _run_tui(command: list[str], workspace: Path, env: dict, timeout: float) -> None:
    import fcntl
    import pty
    import struct
    import termios
    master, slave = pty.openpty()
    process = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 120, 0, 0))
        process = subprocess.Popen(
            [*command, "tui", "--runtime", "legacy", "--agent-bin", sys.executable,
             "--memory-mode", "off", "--timeout", "15"], cwd=workspace, env=env,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        slave = -1
        _drive_terminal(process, master, timeout)
    finally:
        if process is not None:
            _stop_process(process)
        os.close(master)
        if slave >= 0:
            os.close(slave)


def run_checks(command: list[str], workspace: Path, env: dict, timeout: float) -> None:
    for label, args in (("version", ["--version"]), ("tui help", ["tui", "--help"]),
                        ("rule registry", ["rules", "--check"])):
        run_command([*command, *args], workspace, env, timeout, label)
    for step in CHECK_STEPS:
        run_command([*command, "check", "--steps", step], workspace, env, timeout, "check " + step)
    run_command([*command, "health", "--dir", str(workspace), "--json"],
                workspace, env, timeout, "health probes", expected=(0, 1), validate=check_health)
    run_command([*command, "config", "explain", "--json"], workspace,
                env, timeout, "bundled validator configuration",
                validate=lambda output: prepare_fake_agent(workspace, output))
    check_stdin(command, workspace, env, timeout)
    check_yaml_inputs(command, workspace, env, timeout, execute=run_command)
    check_tasks(command, workspace, env, timeout)
    check_missing_gofmt(command, workspace, env, timeout)
    check_instance_locks(command, workspace, env, timeout, flags=_task_flags(),
                         execute=_command_output, verify=check_artifact,
                         record=record_step, stop=_stop_process)
    check_tui(command, workspace, env, timeout)
    check_isolate(command, workspace, env, timeout)


def run_smoke(executable: Path, timeout: float, report_path: Path | None = None) -> int:
    report = {"schema_version": 1, "status": "failed", "error": "verification did not complete", "checks": [],
              "platform": {"system": platform.system(), "machine": platform.machine()},
              "scope": "deterministic local provider; no live model", "binary": {}}
    token, started = _REPORT.set(report), time.monotonic()
    try:
        report["binary"] = binary_identity(executable)
        with tempfile.TemporaryDirectory(prefix="pi-batch binary-smoke-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            run_checks([str(executable)], workspace, isolated_environment(root), timeout)
        if binary_identity(executable) != report["binary"]:
            raise SmokeFailure("executable changed during verification")
        report["status"] = "passed"
        report["error"] = ""
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"[:6000]
        record_step("binary smoke", "failed", reason=report["error"])
    finally:
        _REPORT.reset(token)
        report["actual_seconds"] = round(time.monotonic() - started, 3)
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if report["status"] != "passed":
        return 1
    print("PASS binary smoke (deterministic provider; no live model)", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("--timeout", type=float, default=120, help="deadline in seconds per command")
    parser.add_argument("--report", type=Path, help="write JSON evidence on success or failed checks")
    args = parser.parse_args(argv)
    executable = args.executable.expanduser().resolve()
    if not executable.is_file():
        parser.error(f"executable does not exist: {executable}")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if args.report is not None:
        args.report = args.report.expanduser().resolve()
        if args.report == executable or (args.report.exists() and args.report.samefile(executable)):
            parser.error("--report must not overwrite the executable")
    return run_smoke(executable, args.timeout, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
