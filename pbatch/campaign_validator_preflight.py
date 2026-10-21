"""Non-executing startup-reference checks for campaign validators.

This is not a shell interpreter or a baseline gate: file/judge checkers must
not run before their artifacts exist. Complex/dynamic commands are explicitly
unverified; their normal execution-time validators remain mandatory.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from . import config
from .cmd_expand import expand_cmd
from .config import log, validator_applies
from .runner import run_validation

_FUTURE_OUTPUT = "__pbatch_future_artifact__"
_SHELL_PUNCTUATION = frozenset("|&;()<>")
_PYTHON_OPTIONS = frozenset(("-B", "-E", "-I", "-O", "-OO", "-s", "-S", "-u"))


def _declarations(pipeline, root: Path):
    """Match stage-end repo collection and effective file-task inheritance."""
    for stage in pipeline.stages:
        label = f"stage '{stage.name}'"
        yield label, stage.validate_cmd, root, "repo", str(root)
        if stage.gate and stage.gate_fix_rounds:
            value = (stage.gate_fix_validate if stage.gate_fix_validate is not None
                     else stage.validate_cmd)
            yield label + " gate-fix", value, root, "", str(root)
        for index, task in enumerate(stage.tasks, 1):
            yield (f"{label} task {index}",
                   task.get("validate", task.get("validate_cmd")), root, "repo", str(root))
        yield from _file_declarations(stage, root, label)


def _file_declarations(stage, root: Path, label: str):
    if stage.from_outputs and not stage.meta:
        for index, task in enumerate(stage.tasks, 1):
            value = task.get("validate", task.get("validate_cmd"))
            value = stage.validate_cmd if value is None else value
            render_cwd = task.get("cwd") or str(root)
            cwd = Path(render_cwd)
            cwd = cwd if cwd.is_absolute() else root / cwd
            yield f"{label} task {index}", value, cwd, "file", render_cwd
    elif stage.from_dir:
        from .campaign_pipeline import _repository_resource
        cwd = _repository_resource(root, stage.from_dir)
        if not stage.aggregate and any(char in stage.suffix for char in ("/", "\\")):
            log.warning("PREFLIGHT: %s file validator cwd not statically checked (nested input glob)",
                        label)
            return  # actual per-file parents may differ; do not invent a root cwd or new walker
        yield label, stage.validate_cmd, cwd, "file", str(cwd)
    else:  # from_prompt and meta role tasks use the materialized stage cwd
        yield label, stage.validate_cmd, root, "file", str(root)


def validator_readiness_errors(pipeline, root: Path) -> list[str]:
    """Inspect configured references, without running checker bodies."""
    errors = []
    seen = set()
    try:
        declarations = list(_declarations(pipeline, root))
    except (ValueError, OSError) as exc:
        return [f"validator declarations unavailable: {exc}"]
    for label, value, cwd, scope, render_cwd in declarations:
        for name in (item.strip() for item in (value or "").split(",")):
            if not name:
                continue
            for spec in config._resolve_validator_specs(name):
                if scope and spec.scope != scope:
                    continue
                effective_cwd = root if spec.scope == "repo" else cwd
                expansion_cwd = str(root) if spec.scope == "repo" else render_cwd
                key = (name, str(effective_cwd), expansion_cwd)
                if key in seen:
                    continue
                seen.add(key)
                if not validator_applies(spec, str(effective_cwd)):
                    log.info("PREFLIGHT: %s validator not applicable", label)
                    continue
                error = _reference_error(spec.cmd, effective_cwd, label, expansion_cwd)
                if error:
                    errors.append(f"{label}: {error}")
    return errors


def _direct_argv(command: str) -> list[str] | None:
    # Unsupported shell bodies must be detected before parsing direct-command
    # quotes: heredoc/comment/substitution contents have different quoting rules.
    if any(char in command for char in ("#", "$", "`", "\n", "~", "*", "?", "[")):
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    argv = list(lexer)  # malformed quoting must fail, never become unchecked success
    if (any(token and set(token) <= _SHELL_PUNCTUATION for token in argv) or
            (argv and "=" in argv[0])):
        return None
    return argv


def _reference_error(command: str, cwd: Path, label: str, expansion_cwd: str) -> str:
    try:
        rendered = expand_cmd(command, _FUTURE_OUTPUT, expansion_cwd)
        argv = _direct_argv(rendered)
    except (ValueError, OSError) as exc:
        return f"invalid validator command: {exc}"
    if argv is None:
        log.warning("PREFLIGHT: %s validator not statically checked (shell/dynamic command)", label)
        return ""
    if not argv:
        return "validator command is empty"
    if _FUTURE_OUTPUT in argv[0]:
        log.warning("PREFLIGHT: %s validator not statically checked (output-derived executable)",
                    label)
        return ""
    if not cwd.is_dir():
        log.warning("PREFLIGHT: %s validator not statically checked (cwd unavailable)", label)
        return ""
    # Only shell builtin lookup runs. Do not execute file validators or judges.
    lookup = run_validation("command -v " + shlex.quote(argv[0]), str(cwd),
                            timeout=min(10, config.COMMAND_TIMEOUT), cap=4096)
    if not lookup.ok:
        return f"validator executable unavailable: {argv[0]}"
    script = _script_argument(argv)
    if script is None or _FUTURE_OUTPUT in script:
        log.warning("PREFLIGHT: %s validator input not statically checked (interpreter resolution)",
                    label)
        return ""
    if script:
        path = Path(script)
        path = path if path.is_absolute() else cwd / path
        if not path.is_file():
            if path.exists():
                log.warning("PREFLIGHT: %s validator input not statically checked (non-file startup)",
                            label)
                return ""
            return f"validator script missing or unavailable: {path}"
    log.info("PREFLIGHT: %s validator startup references checked (not checker execution)", label)
    return ""


def _script_argument(argv: list[str]) -> str | None:
    """Only Python literal files are checked; other startup resolution is unknown."""
    executable = Path(argv[0]).name
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        args = list(argv[1:])
        while args and args[0] in _PYTHON_OPTIONS:
            args.pop(0)
    elif executable in {"bash", "sh", "node", "ruby", "perl"}:
        return None
    else:
        return ""
    return args[0] if args and not args[0].startswith("-") else None
