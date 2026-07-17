"""Task data model + serial/parallel execution engine for pi-batch.py.

Split out of pi-batch.py (was 918 lines, over the 500-line code-file gate)
per CLAUDE.md's "先拆分,再继续" rule -- pure extraction, no behavior change.
See pi_batch_config.py for the AGENT_BIN module-attribute-access rationale.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pi_batch_config as cfg

log = cfg.log


# -- data model -------------------------------------------------------
@dataclass
class Task:
    """A single pi invocation task."""

    prompt: str
    output: str = ""
    model: str = cfg.AGENT_DEFAULT_MODEL
    provider: str = ""
    thinking: str = ""
    tools: str = ""
    exclude_tools: str = ""
    cwd: str = ""
    timeout: int = cfg.AGENT_DEFAULT_TIMEOUT
    env: dict = field(default_factory=dict)

    def to_cmd(self) -> list[str]:
        cmd = [cfg.AGENT_BIN, "-p", self.prompt]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.provider:
            cmd.extend(["--provider", self.provider])
        if self.thinking:
            cmd.extend(["--thinking", self.thinking])
        if self.tools:
            cmd.extend(["--tools", self.tools])
        if self.exclude_tools:
            cmd.extend(["--exclude-tools", self.exclude_tools])
        return cmd

    def workdir(self) -> str:
        return self.cwd or os.getcwd()

    def output_path(self) -> Optional[Path]:
        return Path(self.output).resolve() if self.output else None

    def resolve_prompt(self, base_dir: str = "") -> str:
        """Resolve @file references in the prompt to file contents.

        Supports:
          @file.md              -> loads file.md content
          @docs/analysis.md     -> loads docs/analysis.md
          prefix text @file.md  -> prepends file content before prefix text

        Returns the resolved prompt string.
        """
        def replace(match: re.Match) -> str:
            fpath = Path(match.group(1))
            if not fpath.is_absolute():
                fpath = Path(base_dir) / fpath
            if fpath.exists():
                return fpath.read_text(encoding="utf-8")
            log.warning("referenced file not found: %s", fpath)
            return match.group(0)

        return re.sub(r"@(\S+)", replace, self.prompt)


# -- task loading -----------------------------------------------------
def load_tasks(source: str) -> list[Task]:
    """Load tasks from a YAML file, JSON file, or plain text prompt.

    Supports @file.md references in the prompt field.
    """
    path = Path(source)
    if not path.exists():
        return [Task(prompt=source)]

    base_dir = str(path.parent) if path.parent else "."
    raw = path.read_text(encoding="utf-8")

    # YAML
    if cfg.yaml and (source.endswith((".yaml", ".yml")) or raw.lstrip().startswith("tasks:")):
        data = cfg.yaml.safe_load(raw)
        tasks_data = data.get("tasks", []) if isinstance(data, dict) else data
        tasks = []
        for t in tasks_data:
            task = Task(**{k: v for k, v in t.items() if k in Task.__dataclass_fields__})
            task.prompt = task.resolve_prompt(base_dir)
            tasks.append(task)
        return tasks

    # JSON
    try:
        tasks_data = json.loads(raw)
        if isinstance(tasks_data, dict):
            tasks_data = tasks_data.get("tasks", tasks_data)
        if isinstance(tasks_data, list):
            tasks = []
            for t in tasks_data:
                task = Task(**{k: v for k, v in t.items() if k in Task.__dataclass_fields__})
                task.prompt = task.resolve_prompt(base_dir)
                tasks.append(task)
            return tasks
    except json.JSONDecodeError:
        pass

    # Plain text prompt
    return [Task(prompt=raw.strip())]


def load_tasks_from_dir(directory: str, suffix: str = ".md") -> list[Task]:
    """Create one task per file in a directory.

    Each file's content becomes the prompt, and the output is saved as
    <filename>.out.md in the same directory (or specified output dir).
    """
    tasks = []
    basedir = Path(directory)
    if not basedir.is_dir():
        log.error("not a directory: %s", directory)
        return tasks

    for fpath in sorted(basedir.glob(f"*{suffix}")):
        if fpath.name.endswith(".out.md"):
            continue
        prompt = fpath.read_text(encoding="utf-8")
        out_name = fpath.stem + ".out.md"
        tasks.append(Task(
            prompt=prompt,
            output=str(basedir / out_name),
            cwd=str(basedir),
        ))
        log.info("loaded task from %s -> %s", fpath.name, out_name)

    return tasks


# -- execution engine -------------------------------------------------
@dataclass
class TaskResult:
    task: Task
    success: bool
    stdout: str = ""
    stderr: str = ""
    elapsed: float = 0.0
    returncode: int = -1


def _read_stream(stream, prefix: str, collector: list) -> None:
    """Read lines from *stream*, print them (with prefix), and collect."""
    try:
        for line in iter(stream.readline, ""):
            if prefix:
                print(f"{prefix}{line}", end="", flush=True)
            else:
                print(line, end="", flush=True)
            collector.append(line)
    except ValueError:
        # stream closed
        pass
    finally:
        stream.close()


def _stream_and_wait(proc: subprocess.Popen, prefix: str, timeout: int, start: float) -> tuple[bool, str, str, float]:
    """Read stdout/stderr concurrently via threads, wait for *proc*, and
    return (success, stdout_text, stderr_text, elapsed)."""
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    from threading import Thread
    tout = Thread(target=_read_stream, args=(proc.stdout, prefix, stdout_lines), daemon=True)
    terr = Thread(target=_read_stream, args=(proc.stderr, prefix, stderr_lines), daemon=True)
    tout.start()
    terr.start()

    tout.join(timeout=timeout)
    terr.join(timeout=timeout)
    proc.wait(timeout=max(1, timeout - (time.monotonic() - start)))

    elapsed = time.monotonic() - start
    success = proc.returncode == 0
    return success, "".join(stdout_lines), "".join(stderr_lines), elapsed


def _spawn_and_collect(cmd: list[str], workdir: str, env: dict, prefix: str, task: Task, start: float) -> TaskResult:
    """Spawn *cmd*, stream its output via `_stream_and_wait`, and turn the
    outcome (or any of the ways it can fail) into a TaskResult."""
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=workdir,
            env=env,
        )

        success, stdout_text, stderr_text, elapsed = _stream_and_wait(proc, prefix, task.timeout, start)

        if success:
            log.info("OK  done  [%.1fs]  [output=%s]", elapsed, task.output or "(stdout)")
        else:
            log.warning("FAIL  [code=%d]  [%.1fs]", proc.returncode, elapsed)

        return TaskResult(
            task=task,
            success=success,
            stdout=stdout_text,
            stderr=stderr_text,
            elapsed=elapsed,
            returncode=proc.returncode,
        )

    except subprocess.TimeoutExpired:
        proc.kill()
        elapsed = time.monotonic() - start
        log.error("TIMEOUT  [%.1fs]  [limit=%ss]", elapsed, task.timeout)
        return TaskResult(
            task=task,
            success=False,
            stderr=f"Task timed out after {task.timeout}s",
            elapsed=elapsed,
            returncode=-1,
        )

    except FileNotFoundError:
        log.error("'%s' not found in PATH. Is it installed? (configure agent.bin in pi-batch.yaml)", cfg.AGENT_BIN)
        return TaskResult(task=task, success=False, stderr=f"{cfg.AGENT_BIN} not found in PATH")

    except Exception as e:
        elapsed = time.monotonic() - start
        log.error("ERROR  [%.1fs]  [%s]", elapsed, e)
        return TaskResult(task=task, success=False, stderr=str(e), elapsed=elapsed)


def run_task(task: Task, task_index: int = 0, total: int = 0, parallel: bool = False) -> TaskResult:
    """Execute one pi task, streaming output in real-time, and return the result."""
    cmd = task.to_cmd()
    workdir = task.workdir()
    start = time.monotonic()

    prefix = f"[task-{task_index}] " if parallel and total > 1 else ""

    brief = " ".join(cmd[:4]) + ("..." if len(cmd) > 4 else "")
    log.info(">>  %s  [model=%s]  [timeout=%ss]  [dir=%s]",
             brief, task.model or "default", task.timeout, workdir)

    env = os.environ.copy()
    env.update(task.env)

    return _spawn_and_collect(cmd, workdir, env, prefix, task, start)


def save_result(task: Task, result: TaskResult) -> None:
    """Write task result to its output file, or print to stdout."""
    out_path = task.output_path()
    if out_path is None:
        sys.stdout.write(result.stdout)
        if result.stderr:
            sys.stderr.write(result.stderr)
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)

    if result.success:
        out_path.write_text(result.stdout, encoding="utf-8")
        log.info("WROTE %s  (%d bytes)", out_path, len(result.stdout))
    else:
        content = "# TASK FAILED (exit=%d, elapsed=%.1fs)\n\n" % (result.returncode, result.elapsed)
        if result.stderr:
            content += "## stderr\n\n```\n%s\n```\n\n" % result.stderr
        content += result.stdout
        out_path.write_text(content, encoding="utf-8")
        log.info("WROTE (with error info) %s", out_path)


# -- serial / parallel dispatch ---------------------------------------
def run_serial(tasks: list[Task]) -> list[TaskResult]:
    """Execute tasks one by one with real-time output streaming."""
    results = []
    total = len(tasks)
    for i, task in enumerate(tasks, 1):
        log.info("-- [%d/%d] --", i, total)
        result = run_task(task, task_index=i, total=total, parallel=False)
        save_result(task, result)
        results.append(result)
    return results


def run_parallel(tasks: list[Task], workers: int = cfg.AGENT_DEFAULT_WORKERS) -> list[TaskResult]:
    """Execute tasks concurrently with a thread pool and real-time output."""
    total = len(tasks)
    log.info("PARALLEL x%d  (%d tasks)", workers, total)

    results: list[TaskResult] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Pass task_index so parallel output lines are prefixed
        fut_map = {pool.submit(run_task, t, i, total, True): t for i, t in enumerate(tasks, 1)}
        for i, fut in enumerate(as_completed(fut_map), 1):
            task = fut_map[fut]
            result = fut.result()
            save_result(task, result)
            results.append(result)
            log.info("PROGRESS: %d/%d done", i, total)

    return results
