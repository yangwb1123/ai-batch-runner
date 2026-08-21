#!/usr/bin/env python3
"""run-review -- fill context into an AI-SDLC review template and invoke pi.

Usage:
  # Run a specific stage with inline context
  python ai/run-review.py --stage 02 \\
    --project "Snaplink SSO" \\
    --subsystem "OIDC RP-Initiated Logout" \\
    --files "interfaces/sso/handlers.go,protocols/oidc/handle_end_session.go" \\
    --rfcs "RFC6749,OIDC Session Management,OIDC RP-Initiated Logout" \\
    --model claude-sonnet

  # Run from a context YAML file (recommended for multi-stage runs)
  python ai/run-review.py --stage 01 --context ai/examples/oidc-logout-context.yaml

  # Dry-run: print the filled prompt without invoking pi
  python ai/run-review.py --stage 02 --context ai/examples/oidc-logout-context.yaml --dry-run

  # Run all stages sequentially
  python ai/run-review.py --all --context ai/examples/oidc-logout-context.yaml

Context YAML format:
  project: "Snaplink SSO"
  subsystem: "OIDC RP-Initiated Logout"
  files:
    - interfaces/sso/handlers.go
    - protocols/oidc/handle_end_session.go
  rfcs:
    - OIDC Core (OpenID Connect Core 1.0)
    - OIDC RP-Initiated Logout 1.0
    - RFC6749
  architecture_summary: |
    The logout use case lives in protocols/oidc/handle_end_session.go.
    It coordinates session termination, front-channel logout, and back-channel logout.
  load_profile: "(not provided)"
  infra: "(not provided)"
  slo_targets: "(not provided)"
  sprint_goal: "Review the existing flow and define one evidence-backed improvement"
  team_size: "(unknown)"
"""

import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
os.environ.setdefault("PBATCH_SCRIPT_DIR", str(_ROOT))

from pbatch import config as batch_config
from pbatch.evidence import _fair_allowances
from pbatch.runner import (_kill_group, _read_stream,
                           agent_failure_reason as _batch_agent_failure_reason,
                           revalidate_existing as _revalidate_existing,
                           run_validation as _bounded_validation)
from pbatch.text_io import read_text_bounded

try:
    import yaml
except ImportError:
    yaml = None

# Fallback stage/variable schema, used if sdlc.yaml is missing (so this
# script still runs standalone with zero config). When present, sdlc.yaml
# fully replaces both dicts below -- see _load_stage_schema().
_DEFAULT_STAGES = {
    "00": "00-product-discovery.md",
    "01": "01-architecture-review.md",
    "02": "02-security-rfc-review.md",
    "03": "03-distributed-review.md",
    "04": "04-implementation-review.md",
    "05": "05-performance-review.md",
    "06": "06-production-readiness.md",
    "07": "07-sprint-planning.md",
    "08": "08-post-sprint-review.md",
    "09": "09-cto-review.md",
}

_DEFAULT_STAGE_VARS = {
    "00": {
        "PROJECT_NAME": "",
        "SUBSYSTEM": "",
        "FEATURE_DESCRIPTION": "(describe the proposed feature)",
        "BUSINESS_JUSTIFICATION": "(state the business reason)",
        "TARGET_USERS": "(list target personas)",
        "PAIN_POINT_EVIDENCE": "(cite observable evidence)",
        "COMPARABLE_IMPLEMENTATIONS": "(optional: comparable systems)",
    },
    "01": {
        "PROJECT_NAME": "",
        "SUBSYSTEM": "",
        "REPO_PATH": "",
        "PRIMARY_FILES": "",
        "ARCHITECTURE_SUMMARY": "(describe the proposed architecture)",
        "PRODUCT_DISCOVERY_OUTPUT": "(paste Stage 00 output, or 'N/A')",
    },
    "02": {
        "PROJECT_NAME": "",
        "SUBSYSTEM": "",
        "REPO_PATH": "",
        "PRIMARY_FILES": "",
        "RFC_REFERENCES": "",
        "ARCHITECTURE_OUTPUT": "(paste Stage 01 ADR, or 'N/A')",
    },
    "03": {
        "PROJECT_NAME": "",
        "SUBSYSTEM": "",
        "REPO_PATH": "",
        "PRIMARY_FILES": "",
        "STORAGE_SUMMARY": "",
        "ARCHITECTURE_OUTPUT": "(paste Stage 01 ADR, or 'N/A')",
    },
    "04": {
        "PROJECT_NAME": "",
        "SUBSYSTEM": "",
        "REPO_PATH": "",
        "PRIMARY_FILES": "",
        "PRIOR_FINDINGS": "(paste Critical/High findings from Stages 01-03, or 'N/A')",
    },
    "05": {
        "PROJECT_NAME": "",
        "SUBSYSTEM": "",
        "REPO_PATH": "",
        "PRIMARY_FILES": "",
        "LOAD_PROFILE": "",
        "INFRA_SUMMARY": "",
    },
    "06": {
        "PROJECT_NAME": "",
        "SUBSYSTEM": "",
        "REPO_PATH": "",
        "PRIMARY_FILES": "",
        "DEPLOYMENT_TARGET": "",
        "SLO_TARGETS": "",
        "PRIOR_FINDINGS": "(paste Critical/High findings from Stages 02-05, or 'N/A')",
    },
    "07": {
        "PROJECT_NAME": "",
        "SUBSYSTEM": "",
        "SPRINT_GOAL": "",
        "TEAM_SIZE": "",
        "SPRINT_DURATION": "",
        "CRITICAL_HIGH_FINDINGS": "(paste Critical/High findings from all prior stages)",
        "ARCHITECTURE_OUTPUT": "(paste Stage 01 ADR)",
        "VELOCITY": "(last sprint velocity, or 'unknown')",
    },
    "08": {
        "PROJECT_NAME": "",
        "SUBSYSTEM": "",
        "SPRINT_GOAL": "",
        "COMMITTED_STORIES": "(list stories from Stage 07)",
        "REPO_PATH": "",
        "SHIPPED_CHANGES": "(git log or PR list)",
    },
    "09": {
        "PROJECT_NAME": "",
        "SUBSYSTEM": "",
        "ALL_PRIOR_FINDINGS_SUMMARY": "(1-paragraph summary of all findings)",
        "CRITICAL_COUNT": "0",
        "HIGH_COUNT": "0",
        "GRADE_00": "N/A",
        "GRADE_01": "N/A",
        "GRADE_02": "N/A",
        "GRADE_03": "N/A",
        "GRADE_04": "N/A",
        "GRADE_05": "N/A",
        "GRADE_06": "N/A",
        "TEAM_SIZE": "",
        "AGE": "(unknown)",
    },
}


def _load_stage_schema() -> tuple[dict, dict]:
    """Load stages/vars from sdlc.yaml next to this script; fall back to
    the built-in defaults above if the file is absent or unparsable."""
    sdlc_path = Path(__file__).parent / "sdlc.yaml"
    if not yaml or not sdlc_path.exists():
        return dict(_DEFAULT_STAGES), {k: dict(v) for k, v in _DEFAULT_STAGE_VARS.items()}
    data = yaml.safe_load(read_text_bounded(
        sdlc_path, batch_config.INPUT_MAX_BYTES, "review schema")) or {}
    stages_cfg = data.get("stages")
    if not isinstance(stages_cfg, dict) or not stages_cfg:
        return dict(_DEFAULT_STAGES), {k: dict(v) for k, v in _DEFAULT_STAGE_VARS.items()}
    if not all(isinstance(sid, str) and isinstance(value, dict)
               and isinstance(value.get("template", ""), str)
               and isinstance(value.get("vars", {}), dict)
               for sid, value in stages_cfg.items()):
        return dict(_DEFAULT_STAGES), {k: dict(v) for k, v in _DEFAULT_STAGE_VARS.items()}
    stages = {sid: s.get("template", "") for sid, s in stages_cfg.items()}
    stage_vars = {sid: dict(s.get("vars", {})) for sid, s in stages_cfg.items()}
    return stages, stage_vars


def _load_agent_bin() -> str:
    """Read agent.bin from pi-batch.yaml (shared with pi-batch.py,
    which lives alongside this script's parent dir); default to 'pi' if
    absent."""
    if not yaml:
        return "pi"
    path = Path(__file__).parent.parent / "pi-batch.yaml"
    if not path.exists():
        return "pi"
    data = yaml.safe_load(read_text_bounded(
        path, batch_config.INPUT_MAX_BYTES, "agent configuration")) or {}
    return (data.get("agent") or {}).get("bin", "pi")


STAGES, STAGE_VARS = _load_stage_schema()
AGENT_BIN = _load_agent_bin()

# pi-style session flags (shared with pi-batch.py via pi-batch.yaml
# agent.session_flags when present); {session}/{name} are substituted per run.
_DEFAULT_SESSION_FLAGS = {
    "start": ["--session-id", "{session}", "--name", "{name}"],
    "continue": ["--session-id", "{session}"],
}


def _load_session_flags() -> dict:
    """Read agent.session_flags from pi-batch.yaml (shared with
    pi-batch.py); fall back to pi-style flags when absent."""
    if not yaml:
        return {k: list(v) for k, v in _DEFAULT_SESSION_FLAGS.items()}
    path = Path(__file__).parent.parent / "pi-batch.yaml"
    if not path.exists():
        return {k: list(v) for k, v in _DEFAULT_SESSION_FLAGS.items()}
    data = yaml.safe_load(read_text_bounded(
        path, batch_config.INPUT_MAX_BYTES, "agent configuration")) or {}
    cfg = (data.get("agent") or {}).get("session_flags")
    if isinstance(cfg, dict):
        return {k: list(v) for k, v in cfg.items()}
    return {k: list(v) for k, v in _DEFAULT_SESSION_FLAGS.items()}


SESSION_FLAGS = _load_session_flags()


def session_flags(key: str, session_id: str, session_name: str) -> list:
    flags = SESSION_FLAGS.get(key) or _DEFAULT_SESSION_FLAGS[key]
    return [f.replace("{session}", session_id).replace("{name}", session_name) for f in flags]


# Named engineering gates declared in pi-batch.yaml (validators key),
# shared with pi-batch.py; like engineering.yaml for cli.py.
_DEFAULT_VALIDATORS = {
    "quick": "python cli.py check",
    "gofmt": 'test -z "$(gofmt -l {output})"',
}


def _load_validators() -> dict:
    if not yaml:
        return dict(_DEFAULT_VALIDATORS)
    path = Path(__file__).parent.parent / "pi-batch.yaml"
    if not path.exists():
        return dict(_DEFAULT_VALIDATORS)
    data = yaml.safe_load(read_text_bounded(
        path, batch_config.INPUT_MAX_BYTES, "agent configuration")) or {}
    v = data.get("validators")
    if isinstance(v, dict):
        merged = dict(_DEFAULT_VALIDATORS)
        merged.update(v)
        return merged
    return dict(_DEFAULT_VALIDATORS)


VALIDATORS = _load_validators()


def resolve_validators(value: str) -> list:
    """Expand a comma-separated list into validation commands: registry names
    are replaced by their pi-batch.yaml command, anything else is used as a
    raw shell command. Empty value -> no validation."""
    out = []
    for item in [x.strip() for x in (value or "").split(",") if x.strip()]:
        out.append(VALIDATORS.get(item, item))
    return out


def load_context(path: str) -> dict:
    if not yaml:
        print("ERROR: PyYAML not installed. Install the project: uv sync (or pip install pyyaml)", file=sys.stderr)
        sys.exit(1)
    fpath = Path(path)
    if not fpath.exists():
        print(f"ERROR: context file not found: {path}", file=sys.stderr)
        sys.exit(1)
    try:
        data = yaml.safe_load(read_text_bounded(
            fpath, batch_config.INPUT_MAX_BYTES, "review context")) or {}
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict):
        print(f"ERROR: review context must be a mapping: {path}", file=sys.stderr)
        sys.exit(1)
    return data


def context_to_vars(ctx: dict, stage: str) -> dict:
    """Map context YAML keys to template {{VARIABLE}} placeholders for a given stage."""
    files = ctx.get("files", [])
    if isinstance(files, list):
        files_str = "\n".join(f"  - {f}" for f in files)
    else:
        files_str = str(files)

    rfcs = ctx.get("rfcs", [])
    if isinstance(rfcs, list):
        rfcs_str = "\n".join(f"  - {r}" for r in rfcs)
    else:
        rfcs_str = str(rfcs)

    base = {
        "PROJECT_NAME": ctx.get("project", ""),
        "SUBSYSTEM": ctx.get("subsystem", ""),
        "REPO_PATH": ctx.get("repo", os.getcwd()),
        "PRIMARY_FILES": files_str,
        "RFC_REFERENCES": rfcs_str,
        "ARCHITECTURE_SUMMARY": ctx.get("architecture_summary", "(see primary files)"),
        "STORAGE_SUMMARY": ctx.get("storage", ""),
        "LOAD_PROFILE": ctx.get("load_profile", "(not specified)"),
        "INFRA_SUMMARY": ctx.get("infra", "(not specified)"),
        "SLO_TARGETS": ctx.get("slo_targets", "(not specified)"),
        "DEPLOYMENT_TARGET": ctx.get("deployment_target", ctx.get("infra", "(not specified)")),
        "SPRINT_GOAL": ctx.get("sprint_goal", "(not specified)"),
        "TEAM_SIZE": str(ctx.get("team_size", "")),
        "SPRINT_DURATION": ctx.get("sprint_duration", ""),
        "VELOCITY": ctx.get("velocity", "(unknown)"),
        "AGE": ctx.get("age", "(unknown)"),
    }

    # Allow stage-specific overrides in context YAML under key "stage_NN"
    stage_overrides = ctx.get(f"stage_{stage}", {})
    base.update(stage_overrides)

    return base


def fill_template(template_path: Path, variables: dict) -> str:
    text = read_text_bounded(template_path, batch_config.INPUT_MAX_BYTES,
                             "review template")
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", str(value) if value else f"(not provided: {key})")
    return text


# Stage output chaining for --all: target stage -> variable -> source stages.
# Only paste-style variables get chained; explicit context values win (checked
# in main before chain_variables runs).
CHAIN_SOURCES = {
    "01": {"PRODUCT_DISCOVERY_OUTPUT": ["00"]},
    "02": {"ARCHITECTURE_OUTPUT": ["01"]},
    "03": {"ARCHITECTURE_OUTPUT": ["01"]},
    "04": {"PRIOR_FINDINGS": ["01", "02", "03"]},
    "06": {"PRIOR_FINDINGS": ["02", "03", "04", "05"]},
    "07": {
        "CRITICAL_HIGH_FINDINGS": ["00", "01", "02", "03", "04", "05", "06"],
        "ARCHITECTURE_OUTPUT": ["01"],
    },
    "08": {"COMMITTED_STORIES": ["07"]},
    "09": {"ALL_PRIOR_FINDINGS_SUMMARY": ["00", "01", "02", "03", "04", "05", "06", "07", "08"]},
}


def _derive_chain_budget() -> int:
    """Keep chained evidence below half of the configured prompt budget."""
    evidence = max(0, batch_config.EVIDENCE_MAX_BYTES)
    prompt = batch_config.PROMPT_MAX_BYTES
    return evidence if prompt <= 0 else min(evidence, prompt // 2)


# Exposed for operators/tests as the config-time default.  The function below
# recalculates it so tests and embedded callers can safely override config.
CHAIN_BUDGET = _derive_chain_budget()


def _chain_truncation_marker(stage: str, shown: int, total: int,
                             path: str = "") -> str:
    suffix = f"; full output: {path}" if path else ""
    return (f"[stage {stage} output truncated: showing {shown} of {total} "
            f"bytes{suffix}]")


def _chain_source_path(paths: Optional[dict], stage: str) -> str:
    value = (paths or {}).get(stage)
    if not value:
        return ""
    try:
        return str(Path(value).resolve())
    except (OSError, RuntimeError):
        return str(value)


def _render_chained_sources(source_ids: list[str], prior_outputs: dict,
                            budget: int, paths: Optional[dict]) -> str:
    """Render one variable with fair, byte-based head allowances."""
    sources = [(source, str(prior_outputs[source]).encode("utf-8"))
               for source in source_ids if prior_outputs.get(source)]
    if not sources:
        return ""
    allowances = _fair_allowances([len(raw) for _, raw in sources], budget)
    parts, omitted = [], 0
    for (source, raw), allowance in zip(sources, allowances):
        if allowance <= 0:
            omitted += 1
            continue
        shown_raw = raw[:allowance]
        shown = shown_raw.decode("utf-8", errors="ignore")
        shown_bytes = len(shown.encode("utf-8"))
        part = f"--- Stage {source} output ---\n{shown}"
        if shown_bytes < len(raw):
            part += "\n" + _chain_truncation_marker(
                source, shown_bytes, len(raw), _chain_source_path(paths, source))
        parts.append(part)
    if omitted:
        parts.append(f'<chained-sources-omitted count="{omitted}" />')
    return "\n\n".join(parts)


def chain_variables(prior_outputs: dict, stage: str, variables: dict,
                    schema_defaults: dict, *, budget: Optional[int] = None,
                    paths: Optional[dict] = None) -> dict:
    """Inject completed stage outputs into the current stage's paste-style
    variables. A variable is treated as explicitly provided (and left
    untouched) only when it differs from the schema placeholder default or
    the context-provided value; schema placeholders are chainable slots. The
    selected source bytes are fairly allocated and head-truncated before the
    existing stage markers are assembled."""
    candidates = []
    for var, sources in CHAIN_SOURCES.get(stage, {}).items():
        current = variables.get(var)
        if current and current != schema_defaults.get(var):
            continue
        available = [src for src in sources if prior_outputs.get(src)]
        if available:
            candidates.append((var, available))
    if not candidates:
        return {}
    total = _derive_chain_budget() if budget is None else max(0, budget)
    base, extra = divmod(total, len(candidates))
    chained = {}
    for index, (var, sources) in enumerate(candidates):
        value = _render_chained_sources(
            sources, prior_outputs, base + (index < extra), paths)
        if value:
            chained[var] = value
    return chained


def stage_out_dir(args) -> Path:
    if args.output_dir:
        return Path(args.output_dir)
    return Path(__file__).parent / "reviews" / args.context_name


def agent_failure_reason(returncode: int, output: str, bin_name: str = "") -> str:
    """Compatibility wrapper around the batch runner's single failure gate."""
    return _batch_agent_failure_reason(
        returncode, output, bin_name or getattr(batch_config, "AGENT_BIN", ""))


def _print_stage_banner(stage: str, out_file) -> None:
    print(f"\n{'='*60}", flush=True)
    print(f"  Stage {stage}: {STAGES[stage]}", flush=True)
    print(f"  Output: {out_file}", flush=True)
    print(f"{'='*60}\n", flush=True)


def _refuse_symlink_output(stage: str, out_file) -> bool:
    """M4 (round-4): refuse a pre-planted symlink on the no-validator path
    too — writing through it would overwrite the victim with
    agent-controlled bytes (mirrors runner.py's F14 probe)."""
    if out_file.is_symlink():
        print(f"\nStage {stage} REJECTED: output path {out_file} is a symlink (refusing to follow); output NOT saved", file=sys.stderr, flush=True)
        return True
    return False


def _review_agent_command(args, prompt: str, session_flags: Optional[list]) -> list[str]:
    """Build the configured review-agent invocation."""
    cmd = [args.agent_bin or AGENT_BIN, "-p", prompt]
    if args.model:
        cmd.extend(["--model", args.model])
    if session_flags:
        cmd.extend(session_flags)
    return cmd


def _stage_output_file(args, stage: str) -> Path:
    out_dir = stage_out_dir(args)
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"stage-{stage}.out.md"


def _stage_timeout(args) -> Optional[int]:
    """Return configured timeout or None when the value is unsafe."""
    value = getattr(args, "timeout", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value or 600


def _prompt_size_error(prompt: str) -> str:
    """Return a bounded-argv error before a review agent is spawned."""
    size = len((prompt or "").encode("utf-8", errors="replace"))
    limit = batch_config.PROMPT_MAX_BYTES
    return (f"prompt exceeds {limit} bytes ({size} bytes)"
            if limit > 0 and size > limit else "")


def _invoke_review_agent(stage: str, args, out_file: Path, cmd: list,
                         agent_bin: str, timeout: int) -> Optional[str]:
    """Run the agent and reject transport/provider failures before saving."""
    try:
        rc, output = _run_agent(cmd, args.repo or os.getcwd(), timeout)
    except FileNotFoundError:
        print(f"ERROR: '{agent_bin}' not found in PATH.", file=sys.stderr)
        return None
    if rc == -2:
        print(f"\nStage {stage} REJECTED: agent output exceeds "
              f"{batch_config.OUTPUT_MAX_BYTES} bytes; output NOT saved to {out_file}",
              file=sys.stderr, flush=True)
        return None
    if rc < 0:
        print(f"\nStage {stage} REJECTED: agent timed out; output NOT saved to {out_file}",
              file=sys.stderr, flush=True)
        return None
    text = "".join(output)
    reason = agent_failure_reason(rc, text, agent_bin)
    if reason:
        print(f"\nStage {stage} REJECTED: {reason}; output NOT saved to {out_file}",
              file=sys.stderr, flush=True)
        return None
    return text


def run_stage(stage: str, prompt: str, args, session_flags: Optional[list] = None) -> int:
    """Invoke the agent and persist only validated output. Output streams to
    the terminal while running; stage-NN.out.md is written only when the
    agent exits 0 and the output carries no provider/CLI failure signature.
    Rejected output is not saved and the stage fails, so quota or rate-limit
    replies cannot become committed review artifacts."""
    out_file = _stage_output_file(args, stage)

    prompt_error = _prompt_size_error(prompt)
    if prompt_error:
        print(f"\nStage {stage} REJECTED: {prompt_error}; output NOT saved to {out_file}",
              file=sys.stderr, flush=True)
        return 1

    configured_timeout = _stage_timeout(args)
    if configured_timeout is None:
        print(f"\nStage {stage} REJECTED: timeout must be non-negative; "
              f"output NOT saved to {out_file}", file=sys.stderr, flush=True)
        return 1

    agent_bin = args.agent_bin or AGENT_BIN
    cmd = _review_agent_command(args, prompt, session_flags)

    _print_stage_banner(stage, out_file)
    output = _invoke_review_agent(stage, args, out_file, cmd, agent_bin, configured_timeout)
    if output is None:
        return 1

    # Engineering gates: write to a temp file, run every resolved validator
    # (AND semantics), then atomically rename on success (or delete on
    # failure) so a result that fails the project checks never lands as a
    # review file. {output} points at the temp file so gates can inspect it.
    validate_spec = _validation_spec(args)
    commands = resolve_validators(validate_spec)
    if commands:
        return _run_validators(stage, out_file, output, commands, args.repo or os.getcwd())

    # No-validator path: still refuse a pre-planted symlink (M4).
    if _refuse_symlink_output(stage, out_file):
        return 1
    out_file.write_text(output, encoding="utf-8")
    print(f"\nWROTE: {out_file}", flush=True)
    return 0


def _validation_spec(args) -> str:
    return ",".join(value for value in (
        getattr(args, "validate", ""), getattr(args, "validate_cmd", "")) if value)



def _run_agent(cmd: list, cwd: str, timeout: int) -> tuple[int, list[str]]:
    """Run the agent, streaming output to the terminal from a reader
    thread while the main thread enforces the deadline; a hung agent (e.g.
    offline machine) must not block the runner forever. Returns (returncode,
    lines); returncode is -1 on timeout."""
    # Local fix (self-iteration round 2, F2): on ANY abnormal exit (incl.
    # KeyboardInterrupt/SIGTERM) the child process group must be killed so
    # no agent is orphaned; previously only the timeout path killed it.
    proc = None
    completed = False
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            start_new_session=True,  # own process group so the whole child tree can be killed on timeout
        )
        assert proc.stdout is not None

        lines: list[str] = []
        overflow = [False]
        reader = threading.Thread(
            target=_read_stream,
            args=(proc.stdout, "", lines, batch_config.OUTPUT_MAX_BYTES,
                  overflow, True, sys.stdout),
            daemon=True,
        )
        reader.start()
        deadline = time.monotonic() + timeout
        try:
            rc = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            reader.join(timeout=1)
            return -1, lines
        reader.join(timeout=max(0, deadline - time.monotonic()))
        if reader.is_alive():
            _kill_group(proc)
            reader.join(timeout=1)
            return -1, lines
        completed = True
        return (-2 if overflow[0] else rc), lines
    finally:
        if proc is not None and not completed:
            _kill_group(proc)


def _run_validators(stage: str, out_file, output: str, commands: list, cwd: str) -> int:
    """Run every resolved validator against a temp copy of the output
    (AND semantics); atomically rename on success, delete on failure so a
    result that fails the project checks never lands as a review file.
    Round-4 hardening (M4): mkstemp temp name (a fixed '.tmp' suffix could
    be pre-planted as a symlink), {output}/{cwd} shell-quoted (paths with
    spaces/quotes must not inject commands), and a symlink probe on the
    output path before the rename."""
    out_path = Path(out_file)
    if out_path.is_symlink():
        print(f"\nStage {stage} REJECTED: output path {out_file} is a symlink (refusing to follow); output NOT saved", file=sys.stderr, flush=True)
        return 1
    fd, tmp_name = tempfile.mkstemp(prefix=out_path.name + ".", suffix=".tmp", dir=str(out_path.parent))
    os.close(fd)
    tmp_file = Path(tmp_name)
    tmp_file.write_text(output, encoding="utf-8")
    for raw in commands:
        cmd = raw.replace("{output}", shlex.quote(str(tmp_file))).replace("{cwd}", shlex.quote(cwd))
        result = _bounded_validation(cmd, cwd, timeout=600)
        if result.ok:
            continue
        tmp_file.unlink(missing_ok=True)
        detail = " (timeout)" if result.timed_out else f" (exit={result.exit_code})"
        print(f"\nStage {stage} REJECTED: validation failed{detail}: {cmd}; output NOT saved to {out_file}", file=sys.stderr, flush=True)
        for line in (result.stdout or "").strip().splitlines()[-5:]:
            print(f"  | {line}", file=sys.stderr)
        for line in (result.stderr or "").strip().splitlines()[-5:]:
            print(f"  | {line}", file=sys.stderr)
        return 1
    tmp_file.rename(out_file)
    print(f"\nWROTE: {out_file} (validated)", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AI-SDLC review runner — fill template variables and invoke pi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--stage", metavar="NN",
                   help="Stage number to run (00-09)")
    p.add_argument("--all", action="store_true",
                   help="Run all stages sequentially")
    p.add_argument("--resume", action="store_true",
                   help="Resume a previous --all session: skip stages whose output file already exists and chain from the saved outputs")
    p.add_argument("--context", metavar="FILE",
                   help="Context YAML file with subsystem details")
    p.add_argument("--project", help="Project name (overrides context YAML)")
    p.add_argument("--subsystem", help="Subsystem name (overrides context YAML)")
    p.add_argument("--files", help="Comma-separated list of primary files")
    p.add_argument("--rfcs", help="Comma-separated RFC/standard references")
    p.add_argument("--repo", help="Repository path (default: cwd)")
    p.add_argument("--model", default="", help="Model for pi invocation")
    p.add_argument("--agent-bin", default="", help="Agent CLI binary (default: pi-batch.yaml agent.bin, else 'pi')")
    p.add_argument("--timeout", type=int, default=0,
                   help="Per-stage agent timeout in seconds (default: 600; 0 = default)")
    p.add_argument("--session-mode", choices=["new", "shared"], default="new",
                   help="Session reuse across --all stages: new = fresh session per stage (default), shared = one session for the whole review run")
    p.add_argument("--session-name", default="",
                   help="Reproducible session base name (default: context name); shared sessions continue across runs")
    p.add_argument("--validate", default="",
                   help="Named validators from pi-batch.yaml validators registry, comma-separated (e.g. 'quick,gofmt'); AND semantics. Unknown names are treated as raw shell commands")
    p.add_argument("--validate-cmd", default="",
                   help="Engineering gate run against the agent result BEFORE stage-NN.out.md is written; {output} and {cwd} placeholders are substituted. Non-zero exit rejects the stage and leaves no file")
    p.add_argument("--output-dir", metavar="DIR", help="Output directory for review files")
    p.add_argument("--dry-run", action="store_true",
                   help="Print filled prompt without invoking pi")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if args.timeout < 0:
        parser.error("--timeout must be non-negative (0 uses the default)")

    ctx = _build_context(args)
    prompts_dir = Path(__file__).parent / "prompts"

    stages_to_run = _resolve_stages(args)

    out_dir = stage_out_dir(args)
    failures = []
    prior_outputs: dict = {}
    session_name = args.session_name or args.context_name
    if args.resume:
        prior_outputs, skipped = _resume_prior(args, stages_to_run, out_dir)
        print(f"Resume: {skipped} completed stage(s) found, {len(stages_to_run) - skipped} to run", flush=True)

    failures = _run_stage_loop(args, stages_to_run, prompts_dir, ctx, out_dir, session_name, prior_outputs)

    if failures:
        print(f"\nFailed stages: {', '.join(failures)}", file=sys.stderr)
        sys.exit(1)


def _chain_stage_output(stage: str, args, prior_outputs: dict, failures: list) -> None:
    """Load a just-written output through the same bounded chain gate."""
    out_file = stage_out_dir(args) / f"stage-{stage}.out.md"
    if not out_file.exists():
        return
    try:
        prior_outputs[stage] = read_text_bounded(
            out_file, batch_config.OUTPUT_MAX_BYTES, "review output")
    except ValueError as exc:
        print(f"ERROR: completed stage {stage} cannot be chained: {exc}",
              file=sys.stderr, flush=True)
        failures.append(stage)


def _run_stage_loop(args, stages_to_run: list, prompts_dir, ctx: dict, out_dir, session_name: str, prior_outputs: dict) -> list:
    """Run each stage in order: skip resumed outputs, fill the template with
    chained variables, start/continue the shared session, and keep completed
    outputs for chaining into later stages. Returns the list of failed stages."""
    failures = []
    session_active = False
    for stage in stages_to_run:
        if args.resume and stage in prior_outputs:
            print(f"  Stage {stage}: SKIP (output exists: {out_dir / f'stage-{stage}.out.md'})", flush=True)
            continue

        prompt, missing = _stage_prompt(args, stage, prompts_dir, ctx, prior_outputs)
        if missing:
            failures.append(stage)
            continue

        if args.dry_run:
            print(f"\n{'='*60}")
            print(f"  Stage {stage} — DRY RUN")
            print(f"{'='*60}")
            print(prompt)
            continue

        # Shared session: the first executed stage starts the session, later
        # stages continue it; --resume skips do not consume the session.
        flags = None
        if args.session_mode != "new":
            if not session_active:
                flags = session_flags("start", session_name, session_name)
                session_active = True
            else:
                flags = session_flags("continue", session_name, session_name)

        rc = run_stage(stage, prompt, args, flags)
        if rc != 0:
            failures.append(stage)
            if not args.all:
                sys.exit(rc)
            continue

        # Keep completed output for chaining into later stages of --all.
        _chain_stage_output(stage, args, prior_outputs, failures)
    return failures


def _stage_prompt(args, stage: str, prompts_dir, ctx: dict, prior_outputs: dict) -> tuple[str, bool]:
    """Fill the stage template with context and chained variables from
    prior stage outputs. Returns (prompt, missing_template)."""
    base = prompts_dir.resolve()
    candidate = base / STAGES[stage]
    template_file = candidate.resolve()
    try:
        template_file.relative_to(base)
    except ValueError:
        print(f"ERROR: template escapes prompts directory: {STAGES[stage]}",
              file=sys.stderr)
        return "", True
    if candidate.is_symlink() or not template_file.is_file():
        print(f"ERROR: template not found: {template_file}", file=sys.stderr)
        return "", True

    defaults = STAGE_VARS.get(stage, {})
    variables = {k: ctx.get(k.lower(), v) for k, v in defaults.items()}
    variables.update(context_to_vars(ctx, stage))
    paths = {source: stage_out_dir(args) / f"stage-{source}.out.md"
             for source in prior_outputs}
    variables.update(chain_variables(prior_outputs, stage, variables, defaults,
                                     paths=paths))
    try:
        return fill_template(template_file, variables), False
    except ValueError as exc:
        print(f"ERROR: template rejected: {exc}", file=sys.stderr)
        return "", True


def _build_context(args) -> dict:
    """Review context from the ctx file plus CLI overrides; the context
    name becomes the session base name for shared sessions."""
    prompts_dir = Path(__file__).parent / "prompts"
    if not prompts_dir.exists():
        print(f"ERROR: prompts directory not found: {prompts_dir}", file=sys.stderr)
        sys.exit(1)
    ctx = {}
    if args.context:
        ctx = load_context(args.context)
        args.context_name = Path(args.context).stem
    else:
        args.context_name = args.subsystem or "review"
    if args.project:
        ctx["project"] = args.project
    if args.subsystem:
        ctx["subsystem"] = args.subsystem
    if args.files:
        ctx["files"] = [f.strip() for f in args.files.split(",")]
    if args.rfcs:
        ctx["rfcs"] = [r.strip() for r in args.rfcs.split(",")]
    if args.repo:
        ctx["repo"] = args.repo
    return ctx


def _resolve_stages(args) -> list:
    """--all runs every stage; --stage NN runs one. --resume and shared
    sessions only make sense for a full run, so they are rejected otherwise."""
    if args.resume and not args.all:
        print("ERROR: --resume requires --all", file=sys.stderr)
        sys.exit(1)
    if args.session_mode != "new" and not args.all:
        print("ERROR: --session-mode shared requires --all", file=sys.stderr)
        sys.exit(1)
    if args.all:
        return sorted(STAGES.keys())
    if args.stage:
        stage = args.stage.zfill(2)
        if stage not in STAGES:
            print(f"ERROR: unknown stage '{args.stage}'. Valid: {', '.join(STAGES.keys())}", file=sys.stderr)
            sys.exit(1)
        return [stage]
    print("ERROR: specify --stage NN or --all", file=sys.stderr)
    sys.exit(1)


def _resume_prior(args, stages_to_run: list, out_dir) -> tuple[dict, int]:
    """Resume a previous session: load completed outputs from disk so
    downstream stages chain from them, and skip stages that already produced
    a non-empty file (they ran and passed validation last time; rejected
    stages never leave a file, so they rerun). Returns (prior_outputs, count)."""
    prior_outputs: dict = {}
    validate_spec = _validation_spec(args)
    workdir = args.repo or os.getcwd()
    for stage in stages_to_run:
        out_file = out_dir / f"stage-{stage}.out.md"
        if not out_file.exists():
            continue
        if not _revalidate_existing(out_file, validate_spec, workdir):
            print(f"  Stage {stage}: REGENERATE (saved output failed current validation)",
                  file=sys.stderr, flush=True)
            continue
        try:
            prior_outputs[stage] = read_text_bounded(
                out_file, batch_config.OUTPUT_MAX_BYTES, "review output")
        except ValueError as exc:
            print(f"  Stage {stage}: REGENERATE ({exc})", file=sys.stderr, flush=True)
    return prior_outputs, len(prior_outputs)


if __name__ == "__main__":
    main()
