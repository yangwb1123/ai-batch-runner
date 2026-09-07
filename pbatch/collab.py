"""Parallel module collaboration coordinator.

`collab` keeps the dirty source worktree as a read-only baseline and runs one
protected `isolate` child per module.  The children have independent
worktrees, runtime state, locks, and review patches; no child can merge or
commit back to the source worktree.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from . import config
from .config import log, yaml
from .isolate import SCOPE_BLOCKED_EXIT, _repo_root
from .lock import acquire_lock, release_lock
from .text_io import read_text_bounded
from .worktree_slice import (git_head, normalize_patterns, path_matches,
                             worktree_digest)

_MAX_AGENTS = 32
_MAX_PROMPT_BYTES = 64 * 1024
_DEFAULT_TIMEOUT = 14_400
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class AgentPlan:
    ident: str
    prompt: str
    seed_dirty: tuple[str, ...]
    write_scope: tuple[str, ...]
    timeout: int
    validate: str = ""


@dataclass(frozen=True)
class AgentResult:
    plan: AgentPlan
    worktree: Path
    patch: Path
    returncode: int
    timed_out: bool = False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-batch.py collab",
        description="Run protected agents concurrently in module worktrees "
                    "from a dirty source baseline")
    parser.add_argument("--plan", required=True,
                        help="YAML collaboration plan (may live outside the repository)")
    parser.add_argument("--root", default=".",
                        help="source repository/worktree (default: current directory)")
    parser.add_argument("--workers", type=int, default=0,
                        help="maximum concurrent module agents (default: plan or agent count)")
    parser.add_argument("--agent-bin", default="",
                        help="agent binary override passed to every child")
    parser.add_argument("--model", default="")
    parser.add_argument("--provider", default="")
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and print the module plan without creating files")
    return parser


def _read_plan(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"collaboration plan is missing or unsafe: {path}")
    if not yaml:
        raise ValueError("collaboration plans require PyYAML")
    try:
        data = yaml.safe_load(read_text_bounded(
            path, config.INPUT_MAX_BYTES, "collaboration plan")) or {}
    except Exception as exc:
        raise ValueError(f"invalid collaboration plan: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("collaboration plan must be a mapping")
    return data


def _path_list(value, field: str) -> tuple[str, ...]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise ValueError(f"agent {field} must be a string or list")
    try:
        return normalize_patterns(values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid agent {field}: {exc}") from exc


def _agent_plan(raw: object, index: int) -> AgentPlan:
    if not isinstance(raw, dict):
        raise ValueError(f"agent[{index}] must be a mapping")
    ident = str(raw.get("id", "")).strip()
    if not _SAFE_ID.fullmatch(ident):
        raise ValueError(f"agent[{index}] has unsafe id")
    prompt = raw.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(f"agent {ident} needs a non-empty prompt")
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        raise ValueError(f"agent {ident} prompt exceeds {_MAX_PROMPT_BYTES} bytes")
    scope = _path_list(raw.get("write_scope", raw.get("allow", [])), "write_scope")
    seed = _path_list(raw.get("seed_dirty", scope), "seed_dirty")
    timeout = raw.get("timeout", _DEFAULT_TIMEOUT)
    if type(timeout) is not int or timeout <= 0:
        raise ValueError(f"agent {ident} timeout must be positive")
    validate = raw.get("validate", "")
    if not isinstance(validate, str):
        raise ValueError(f"agent {ident} validate must be a string")
    return AgentPlan(ident, prompt, seed, scope, timeout, validate)


def _validate_unique(plans: Sequence[AgentPlan]) -> None:
    identifiers = [plan.ident for plan in plans]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("collaboration agent ids must be unique")
    scopes = {plan.ident: plan.write_scope for plan in plans}
    for left_index, left in enumerate(plans):
        for right in plans[left_index + 1:]:
            if _scope_overlap(scopes[left.ident], scopes[right.ident]):
                raise ValueError(
                    f"agent write scopes overlap: {left.ident} and {right.ident}")


def _scope_overlap(left: Sequence[str], right: Sequence[str]) -> bool:
    for first in left:
        for second in right:
            if (path_matches(first, (second,)) or
                    path_matches(second, (first,))):
                return True
    return False


def _plan_values(root: Path, data: dict) -> tuple[Path, tuple[AgentPlan, ...], int]:
    configured = data.get("workspace", "")
    if configured:
        if not isinstance(configured, str):
            raise ValueError("collaboration plan workspace must be a path")
        if Path(configured).expanduser().resolve() != root:
            raise ValueError("collaboration plan workspace does not match --root")
    raw_agents = data.get("agents")
    if not isinstance(raw_agents, list) or not raw_agents:
        raise ValueError("collaboration plan needs a non-empty agents list")
    if len(raw_agents) > _MAX_AGENTS:
        raise ValueError(f"collaboration plan exceeds {_MAX_AGENTS} agents")
    plans = tuple(_agent_plan(raw, index) for index, raw in enumerate(raw_agents))
    _validate_unique(plans)
    configured_root = data.get("worktree_root", "")
    if configured_root and not isinstance(configured_root, str):
        raise ValueError("collaboration worktree_root must be a path")
    worktree_root = (root / configured_root if configured_root and
                     not Path(configured_root).expanduser().is_absolute()
                     else Path(configured_root or
                               f"/tmp/pi-batch-collab-{root.name}")).expanduser().resolve()
    if _inside(root, worktree_root):
        raise ValueError("collaboration worktree_root must be outside source repository")
    workers = data.get("workers", len(plans))
    if type(workers) is not int or workers <= 0:
        raise ValueError("collaboration workers must be positive")
    return worktree_root, plans, min(workers, len(plans))


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _print_plan(root: Path, worktree_root: Path,
                plans: Sequence[AgentPlan], workers: int) -> None:
    print(f"Source: {root}")
    print(f"Baseline: dirty working tree (read-only)")
    print(f"Worktrees: {worktree_root}")
    print(f"Workers: {workers}")
    for plan in plans:
        print(f"  - {plan.ident}: scope={', '.join(plan.write_scope)}")
    print("Dry-run made no files, worktrees, locks, or agent calls.")


def _tool_entrypoint() -> Path:
    candidate = Path(config.TOOL_ROOT) / "pi-batch.py"
    return candidate if candidate.is_file() else Path(__file__).resolve().parents[1] / "pi-batch.py"


def _child_command(plan: AgentPlan, worktree: Path, patch: Path,
                   args: argparse.Namespace, logfile: Path, branch: str) -> list[str]:
    command = [sys.executable, str(_tool_entrypoint()), "isolate",
               "-b", branch, "-d", str(worktree)]
    for path in plan.seed_dirty:
        command.extend(["--seed-dirty", path])
    for path in plan.write_scope:
        command.extend(["--write-scope", path])
    command.extend(["--patch-file", str(patch), "--", "-p", plan.prompt,
                    "--no-git-commit", "--stream-output", "none",
                    "--log-file", str(logfile), "--timeout", str(plan.timeout)])
    if plan.validate:
        command.extend(["--validate", plan.validate])
    if args.agent_bin:
        command.extend(["--agent-bin", args.agent_bin])
    if args.model:
        command.extend(["--model", args.model])
    if args.provider:
        command.extend(["--provider", args.provider])
    if args.retries:
        command.extend(["--retries", str(args.retries)])
    return command


def _kill_process(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def _run_agent(root: Path, state_root: Path, worktree_root: Path,
               plan: AgentPlan, args: argparse.Namespace, run_id: str) -> AgentResult:
    worktree = worktree_root / plan.ident
    agent_state = state_root / plan.ident
    agent_state.mkdir(parents=True, exist_ok=True)
    patch = agent_state / f"{plan.ident}.patch"
    logfile = agent_state / "run.log"
    branch = f"pbatch/collab/{run_id}/{plan.ident}"
    env = os.environ.copy()
    env["PBATCH_DATA_DIR"] = str(agent_state / "data")
    env["PBATCH_LOCK_FILE"] = str(agent_state / "lock")
    command = _child_command(plan, worktree, patch, args, logfile, branch)
    log.info("COLLAB: starting %s in %s", plan.ident, worktree)
    process = subprocess.Popen(command, cwd=str(root), env=env,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,
                               start_new_session=True)
    timed_out = False
    try:
        returncode = process.wait(timeout=plan.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process(process)
        returncode = -1
    return AgentResult(plan, worktree, patch, returncode, timed_out)


def _prepare_worktrees(root: Path, worktree_root: Path,
                       plans: Sequence[AgentPlan]) -> None:
    worktree_root.mkdir(parents=True, exist_ok=True)
    for plan in plans:
        worktree = worktree_root / plan.ident
        if worktree.exists() and any(worktree.iterdir()):
            raise ValueError(f"collaboration worktree is not empty: {worktree}")


def _run_agents(root: Path, worktree_root: Path, plans: Sequence[AgentPlan],
                workers: int, args: argparse.Namespace, run_id: str) -> list[AgentResult]:
    state_root = worktree_root / f".collab-{run_id}"
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_run_agent, root, state_root, worktree_root,
                               plan, args, run_id) for plan in plans]
        for future in as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda result: result.plan.ident)


def _report_results(results: Sequence[AgentResult]) -> None:
    for result in results:
        state = "TIMEOUT" if result.timed_out else str(result.returncode)
        print(f"{result.plan.ident}: exit={state} worktree={result.worktree} "
              f"patch={result.patch}")


def _source_lock_present(root: Path) -> bool:
    """Do not take over a source tree already carrying its runtime lock."""
    return (root / ".pi-batch.lock").exists()


def _acquire_coordinator_lock(root: Path, worktree_root: Path):
    """Serialize collab coordinators without creating a source lock file."""
    lock_file = os.environ.get("PBATCH_LOCK_FILE", "").strip()
    lock_file = lock_file or str(worktree_root / ".collab.lock")
    candidate = Path(lock_file).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if _inside(root, candidate.resolve()):
        raise ValueError("PBATCH_LOCK_FILE must be outside source repository")
    previous = os.environ.get("PBATCH_LOCK_FILE")
    os.environ["PBATCH_LOCK_FILE"] = str(candidate.resolve())
    try:
        return acquire_lock(str(root))
    finally:
        if previous is None:
            os.environ.pop("PBATCH_LOCK_FILE", None)
        else:
            os.environ["PBATCH_LOCK_FILE"] = previous


def run_collab(root: Path, data: dict, args: argparse.Namespace) -> int:
    worktree_root, plans, configured_workers = _plan_values(root, data)
    workers = args.workers or configured_workers
    if workers <= 0 or args.retries < 0:
        raise ValueError("--workers and --retries must be non-negative/positive")
    if args.dry_run:
        _print_plan(root, worktree_root, plans, min(workers, len(plans)))
        return 0
    if _source_lock_present(root):
        raise ValueError("source worktree already has .pi-batch.lock; refusing to start")
    source_head = git_head(root)
    source_digest = worktree_digest(root)
    coordinator_lock = _acquire_coordinator_lock(root, worktree_root)
    try:
        return _run_collab_locked(
            root, worktree_root, plans, min(workers, len(plans)), args,
            source_head, source_digest)
    finally:
        release_lock(coordinator_lock)


def _run_collab_locked(root: Path, worktree_root: Path,
                       plans: Sequence[AgentPlan], workers: int,
                       args: argparse.Namespace, source_head: str,
                       source_digest: str) -> int:
    _prepare_worktrees(root, worktree_root, plans)
    run_id = f"{time.time_ns()}"
    results = _run_agents(root, worktree_root, plans, workers, args, run_id)
    _report_results(results)
    if git_head(root) != source_head or worktree_digest(root) != source_digest:
        log.error("COLLAB: source worktree changed during module execution")
        return SCOPE_BLOCKED_EXIT
    if any(result.timed_out for result in results):
        return 1
    if any(result.returncode == SCOPE_BLOCKED_EXIT for result in results):
        return SCOPE_BLOCKED_EXIT
    return 0 if all(result.returncode == 0 for result in results) else 1


def collab_main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    requested_root = Path(args.root).expanduser().resolve()
    root = _repo_root(requested_root)
    try:
        if root is None:
            raise ValueError(f"not a Git worktree: {requested_root}")
        data = _read_plan(Path(args.plan).expanduser().resolve())
        return run_collab(root, data, args)
    except (OSError, ValueError) as exc:
        log.error("COLLAB config: %s", exc)
        return 2
