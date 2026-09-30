"""Declarative pi-batch defaults, adapters and validator registry."""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import sys
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple, Optional

from .assets import (PACKAGE_ROOT, TOOL_ROOT, asset_candidates, expand_validator_registry,
                     is_tool_asset, placeholder_values, resolve_asset_path)
from .config_device import _bool_setting, load_device_fabric
from .config_values import _choice_setting, _float_setting, _int_setting, _string_setting
from .config_document import MAX_CONFIG_BYTES, load_config_yaml
from .text_io import read_text_bounded
from .validator_schema import schema_validator_command

try:
    import yaml
except ImportError:
    yaml = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pi-batch")


# Configuration documents are versioned independently from the package
# version.  Missing ``config_version`` remains accepted as version 0 so old
# projects keep working; a future version must fail closed instead of being
# silently interpreted with today's defaults.
CONFIG_SCHEMA_VERSION = 1

# Explicit project-config path. The validator registry (and agent defaults) is
# project data, not tool data: a tool checkout that drives campaigns for other
# repositories must not bake a per-project copy of pi-batch.yaml into itself,
# and a target repository must not have to vendor a copy of this tool to get
# its own validators. This variable points the loader at the project's file and
# takes precedence over the discovery order below. A path that does not exist
# fails closed instead of silently falling back to the tool's registry, which
# is exactly the drift this exists to prevent.
BATCH_CONFIG_ENV = "PBATCH_PROJECT_CONFIG"


def _batch_config_candidates() -> list[Path]:
    """Return explicit project candidates in the portable shim's order."""
    candidates = []
    script_dir = os.environ.get("PBATCH_SCRIPT_DIR", "")
    if script_dir:
        candidates.append(Path(script_dir) / "pi-batch.yaml")
    candidates.append(Path(__file__).resolve().parent / "pi-batch.yaml")
    candidates.append(Path("pi-batch.yaml"))
    return candidates


def _project_config_path() -> Optional[Path]:
    """Keep packaged defaults separate from operator-owned configuration.

    An explicit PBATCH_PROJECT_CONFIG wins over the discovery order. It is
    resolved even when the file is missing, so the caller reports the bad path
    rather than quietly running with the tool's own registry.
    """
    explicit = os.environ.get(BATCH_CONFIG_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return next((path for path in _batch_config_candidates() if path.exists()), None)


def _default_config_path() -> Optional[Path]:
    path = Path(TOOL_ROOT) / "pi-batch.yaml"
    project = _project_config_path()
    if project and project.resolve() == path.resolve():
        return None
    return path if path.exists() else None


def _batch_config_path() -> Optional[Path]:
    """Return the first existing project/tool config path, if any."""
    return _project_config_path() or _default_config_path()


def _validate_config_version(data: dict, path: Path) -> None:
    """Reject malformed or newer config documents before applying defaults."""
    version = data.get("config_version", 0)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"{path}: config_version must be an integer")
    if version < 0:
        raise ValueError(f"{path}: config_version must be >= 0")
    if version > CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: config_version {version} is newer than supported "
            f"version {CONFIG_SCHEMA_VERSION}")


def _yaml_error_detail(exc: Exception) -> str:
    """Describe parser failures without echoing potentially secret YAML text."""
    mark = getattr(exc, "problem_mark", None)
    location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
    return f"invalid YAML{location} ({type(exc).__name__})"


def _read_config_document(path: Path, max_bytes: int = MAX_CONFIG_BYTES) -> dict:
    """Read a bounded configuration mapping or raise an actionable error."""
    if not yaml:
        raise ValueError(f"{path}: PyYAML is unavailable")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError(f"{path}: configuration byte limit must be a positive integer")
    limit = min(max_bytes, MAX_CONFIG_BYTES)
    try:
        return load_config_yaml(read_text_bounded(path, limit, "configuration"), yaml)
    except (yaml.YAMLError, RecursionError) as exc:
        raise ValueError(f"{path}: {_yaml_error_detail(exc)}") from None
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{path}: cannot read YAML ({type(exc).__name__})") from None
    except ValueError as exc:
        if isinstance(exc.__cause__, (OSError, UnicodeError)):
            raise ValueError(f"{path}: cannot read YAML ({type(exc.__cause__).__name__})") from None
        raise ValueError(f"{path}: {exc}") from None


def _load_config_document(path: Optional[Path]) -> dict:
    if path is None:
        return {}
    data = _read_config_document(path)
    _validate_config_version(data, path)
    return data


def _find_batch_config() -> Optional[dict]:
    """Locate and parse pi-batch.yaml: the entry script directory
    (PBATCH_SCRIPT_DIR, set by the pi-batch.py shim) wins, then the package
    directory, then the process working directory. None when absent."""
    if not yaml:
        return None
    path = _batch_config_path()
    if path is None:
        return None
    return _load_config_document(path)

def _load_batch_config(path: str = "pi-batch.yaml") -> dict:
    """Optional defaults for pi-batch. Missing file -> {} (built-in defaults
    below apply), so the tool still runs standalone with zero config -- copy
    pi-batch.yaml alongside pi-batch.py to point it at a different agent
    CLI."""
    data = _find_batch_config()
    return data if data is not None else {}

def _find_user_config() -> dict:
    """用户级配置（分层低优先层；项目 pi-batch.yaml 覆盖之）。"""
    from . import user_dirs
    if not yaml:
        return {}
    path = user_dirs.user_settings_path()
    if not path.exists():
        return {}
    return _load_config_document(path)

def _merge_config(project: dict, user: dict) -> dict:
    """项目配置覆盖用户配置（节内键级合并）。"""
    merged: dict = {}
    for section_name in set(project) | set(user):
        proj = project.get(section_name, {})
        usr = user.get(section_name, {})
        if isinstance(proj, dict) and isinstance(usr, dict):
            merged[section_name] = {**usr, **proj}
        else:
            merged[section_name] = proj if section_name in project else usr
    return merged

def _startup_config() -> tuple[dict, tuple[str, ...]]:
    """Permit broken-file inspection, while refusing normal execution early."""
    # Copied stdlib-only tools (e.g. json_schema_cli.py under python -S)
    # historically run without YAML support, regardless of ambient settings.
    if not yaml:
        return {}, ()
    loaders = (lambda: _load_config_document(_default_config_path()),
               _find_user_config,
               lambda: _load_config_document(_project_config_path()))
    merged, errors = {}, []
    for load in loaders:
        try:
            merged = _merge_config(load(), merged)
        except ValueError as exc:
            errors.append(str(exc))
    if errors and sys.argv[1:2] not in (["config"], ["doctor"]):
        for error in errors:
            print(f"Configuration error: {error}", file=sys.stderr)
        print("Run 'pi-batch config check' or 'pi-batch doctor' for diagnostics.",
              file=sys.stderr)
        raise SystemExit(2)
    return merged, tuple(errors)


_BATCH_CFG, CONFIG_ERRORS = _startup_config()

def _section(name: str) -> dict:
    """Return a mapping config section, falling back on malformed input."""
    value = _BATCH_CFG.get(name, {})
    if isinstance(value, dict):
        return value
    log.warning("Config section '%s' must be a mapping; using defaults", name)
    return {}

_AGENT_CFG = _section("agent")
# Optional per-agent override sections: agent.agents.<bin>.* win over the
# global agent.* settings for that binary (G line: claude/codex/gemini have
# different failure signatures and session-flag syntax).
_AGENTS_CFG = _AGENT_CFG.get("agents")
if not isinstance(_AGENTS_CFG, dict):
    _AGENTS_CFG = {}

def _agent_section(bin_name: str) -> dict:
    """Per-agent config section for a binary name (agent.agents.<bin>),
    or {} when the section is absent/malformed. Exact path keys win;
    otherwise an executable path inherits its basename adapter."""
    if not bin_name:
        return {}
    section = _AGENTS_CFG.get(bin_name)
    if not isinstance(section, dict):
        section = _AGENTS_CFG.get(Path(str(bin_name)).name)
    return section if isinstance(section, dict) else {}

_DEFAULT_ARGUMENT_FLAGS = {
    "prompt": ("-p",),
    "model": ("--model",),
    "provider": ("--provider",),
    "thinking": ("--thinking",),
    "tools": ("--tools",),
    "exclude_tools": ("--exclude-tools",),
}


def _argument_tokens(value, default: tuple, label: str) -> tuple:
    """Normalize one adapter flag without shell-splitting untrusted config.

    An empty string/list disables an optional flag. For ``prompt`` it means
    the prompt is positional, which is required by CLIs such as Codex.
    """
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple)) and all(
            isinstance(token, str) and token for token in value):
        return tuple(value)
    log.warning("Config argument_flags.%s must be a string or string list; using defaults",
                label)
    return default


def agent_extra_args(bin_name: str = "") -> tuple[str, ...]:
    """Per-agent trailing args (agent.agents.<bin>.extra_args, falling back
    to the global agent.extra_args). Used by CLIs that need invocation-level
    flags outside the six argument_flags slots — e.g. Codex's sandbox mode
    (``codex exec --approve-for-me``). Empty tuple when absent/malformed.
    """
    section = _agent_section(bin_name or AGENT_BIN)
    value = section.get("extra_args", _AGENT_CFG.get("extra_args"))
    if isinstance(value, (list, tuple)) and all(
            isinstance(token, str) and token for token in value):
        return tuple(value)
    if value not in (None, []):
        log.warning(
            "agent extra_args must be a list of non-empty strings; ignored: %r",
            value)
    return ()


def agent_output_protocol(bin_name: str = "") -> str:
    """Return the stdout protocol declared by an agent adapter.

    ``codex-jsonl`` is an explicit trust-boundary contract: every stdout
    record is parsed as a Codex ``--json`` event and only the final assistant
    message may become a task artifact.  Unconfigured adapters remain plain
    text.  The basename/legacy-capture inference keeps existing Codex adapter
    overrides working while configurations migrate to ``output_protocol``.
    """
    name = bin_name or AGENT_BIN
    section = _agent_section(name)
    value = section.get("output_protocol", _AGENT_CFG.get("output_protocol"))
    if isinstance(value, str) and value.strip():
        normalized = value.strip().lower().replace("_", "-")
        if normalized in {"plain", "codex-jsonl"}:
            return normalized
        log.warning("Unknown agent output_protocol %r; treating stdout as plain text",
                    value)
        return "plain"
    extra_args = agent_extra_args(name)
    if "--json" not in extra_args:
        return "plain"
    if Path(str(name)).name == "codex" or agent_capture_thread_id(name):
        return "codex-jsonl"
    return "plain"


def agent_argument_flags(bin_name: str = "") -> dict[str, tuple]:
    """Resolve the argv adapter for an agent binary.

    Precedence is agent.agents.<bin>.argument_flags > agent.argument_flags >
    the historical pi-compatible flag contract. Per-key merging keeps a
    small override backwards compatible with the existing defaults.
    """
    global_flags = _AGENT_CFG.get("argument_flags")
    global_flags = global_flags if isinstance(global_flags, dict) else {}
    local_flags = _agent_section(bin_name or AGENT_BIN).get("argument_flags")
    local_flags = local_flags if isinstance(local_flags, dict) else {}
    resolved = {}
    for key, default in _DEFAULT_ARGUMENT_FLAGS.items():
        value = local_flags.get(key, global_flags.get(key, default))
        resolved[key] = _argument_tokens(value, default, key)
    return resolved


def agent_no_tools_args(bin_name: str = "") -> tuple[str, ...]:
    """Return the adapter's explicit tool-disable argv, or empty if unsupported."""
    section = _agent_section(bin_name or AGENT_BIN)
    value = section.get("no_tools_args", _AGENT_CFG.get("no_tools_args"))
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, (list, tuple)) and all(
            isinstance(token, str) and token for token in value):
        return tuple(value)
    if value is not None:
        log.warning("agent no_tools_args must be a string or non-empty string list")
    return ()


def build_agent_argv(bin_name: str, prompt: str, *, model: str = "",
                     provider: str = "", thinking: str = "", tools: str = "",
                     exclude_tools: str = "", no_tools: bool = False,
                     session_flags: Optional[list] = None) -> list[str]:
    """Build an agent argv from the configured adapter, without a shell."""
    if no_tools and tools:
        raise ValueError("no_tools cannot be combined with an explicit tools allowlist")
    no_tools_args = agent_no_tools_args(bin_name) if no_tools else ()
    if no_tools and not no_tools_args:
        raise ValueError(
            f"agent adapter for {bin_name!r} cannot disable tools; "
            "configure agent.agents.<name>.no_tools_args")
    flags = agent_argument_flags(bin_name)
    session_led = bool(session_flags) and any(
        "{prompt}" in token for token in session_flags)
    if session_led:
        cmd = [bin_name, *(token.replace("{prompt}", prompt)
                           for token in session_flags)]
    else:
        cmd = [bin_name, *flags["prompt"], prompt]
    for key, value in (("model", model), ("provider", provider),
                       ("thinking", thinking), ("tools", tools),
                       ("exclude_tools", exclude_tools)):
        if not value:
            continue
        if not flags[key]:
            raise ValueError(
                f"agent adapter for {bin_name!r} does not support {key}")
        cmd.extend([*flags[key], value])
    if session_flags and not session_led:
        cmd.extend(session_flags)
    # Extra args are appended with dedup against tokens already present:
    # shared options (e.g. --approve-for-me inside a session-led template)
    # must not be handed to the CLI twice; leading placement wins.
    seen = set(cmd)
    cmd.extend(token for token in agent_extra_args(bin_name)
               if token not in seen)
    cmd.extend(no_tools_args)
    return cmd


def agent_prompt_index(bin_name: str = "") -> int:
    """Index of the prompt value in argv built by :func:`build_agent_argv`."""
    if agent_session_argv_layout(bin_name or AGENT_BIN) == "led":
        for flags in _configured_session_flag_sets(bin_name or AGENT_BIN):
            for index, token in enumerate(flags, 1):
                if "{prompt}" in token:
                    return index
    return 1 + len(agent_argument_flags(bin_name or AGENT_BIN)["prompt"])


def agent_session_argv_layout(bin_name: str) -> str:
    """Return whether configured session argv leads or trails the prompt."""
    return ("led" if any("{prompt}" in token
                         for flags in _configured_session_flag_sets(bin_name)
                         for token in flags) else "tail")


def _configured_session_flag_sets(bin_name: str) -> list[list[str]]:
    """Return valid configured session flag templates for layout inspection."""
    configured = _agent_section(bin_name or AGENT_BIN).get("session_flags")
    if configured is None:
        configured = _AGENT_CFG.get("session_flags")
    if not isinstance(configured, dict):
        return []
    return [list(flags) for flags in configured.values()
            if isinstance(flags, (list, tuple)) and flags
            and all(isinstance(flag, str) for flag in flags)]


def agent_preflight_errors(bin_name: str, *, cwd: str = "",
                           path: Optional[str] = None) -> list[str]:
    """Return adapter/executable errors before logs, locks or agents start."""
    name = str(bin_name or "").strip()
    if not name:
        return ["agent binary is empty"]
    errors = []
    if cwd or path is not None:
        root = os.path.abspath(cwd or os.getcwd())
        candidate = os.path.join(root, name) if os.path.dirname(name) else name
        search = os.environ.get("PATH", os.defpath) if path is None else path
        search = os.pathsep.join(os.path.join(root, item) for item in search.split(os.pathsep))
        executable = shutil.which(candidate, path=search)
    else:
        executable = shutil.which(name)
    if executable is None:
        errors.append(f"agent executable not found or not executable: {name}")
    try:
        agent_argument_flags(name)
    except (TypeError, ValueError) as exc:
        errors.append(f"invalid adapter for {name}: {exc}")
    return errors
# Default provider/CLI failure signatures (G line). Per-agent overrides:
#   agent.error_patterns: [...]      (all agents)
#   agent.agents.<bin>.error_patterns: [...]   (one binary)
# Empty list disables signature rejection for that agent.
_DEFAULT_PROVIDER_ERROR_PATTERNS = (
    # provider error codes (quota, rate limit, billing, auth)
    r"rate_?limit_?error",
    r"insufficient_?quota",
    r"quota_?exceeded",
    # opencode-go / Go-gateway usage limits. The emitted shape is
    # `429: {"type":"GoUsageLimitError","message":"Go usage limit exceeded"}`
    # — camelCase class name and a SPACE-separated phrase — so none of the
    # underscore/`_?exceeded` patterns above match it. Without these two the
    # shared provider breaker never tripped during the 2026-09-19 iris-ui 24h
    # campaign and ~1412 provider errors burned 8h of wall clock at one
    # empty round every ~2 minutes.
    r"go_?usage_?limit_?error",
    r"usage_?limit_?exceeded",
    r"credit_?balance_?too_?low",
    r"insufficient\s+(?:balance|funds)",
    r"billing_?error",
    r"payment_?required",
    r"invalid_?api_?key",
    r"authentication_?error",
    r"overloaded_?error",
    r"429 too many requests",
    # network connectivity failures (offline, DNS, TLS, proxy)
    r"network is unreachable",
    r"no route to host",
    r"temporary failure in name resolution",
    r"name or service not known",
    r"dns resolution failed",
    r"getaddrinfo",
    r"max retries exceeded",
    r"certificate verify failed",
    r"connectionerror",
    r"sslerror",
    r"proxyerror",
    r"econnrefused",
    r"econnreset",
    r"etimedout",
    r"curl: \(\d+\)",
    r"connection timed out",
    r"connect timed out",
    r"operation timed out",
    r"connection refused",
    r"connection reset",
    r"connection closed",
    r"broken pipe",
    r"failed to connect",
    r"unable to connect",
    r"could not connect",
)

_DEFAULT_ERROR_PATTERNS = (
    *_DEFAULT_PROVIDER_ERROR_PATTERNS,
    # Task-specific request rejection is not provider health.
    r"context_?length_?exceeded",
    # CLI-level failure banners (inline (?im) adds MULTILINE to IGNORECASE).
    r"(?im)^\[?error\]?:",
    r"(?im)^fatal:",
)
_ARTIFACT_ONLY_ERROR_PATTERNS = frozenset({
    r"context_?length_?exceeded",
    r"(?im)^\[?error\]?:",
    r"(?im)^fatal:",
})

@lru_cache(maxsize=16)
def _compile_patterns(patterns: tuple) -> tuple:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)

def agent_error_patterns(bin_name: str = "") -> tuple:
    """Failure-signature patterns for the current agent binary: per-agent
    override (agent.agents.<bin>.error_patterns) > global override
    (agent.error_patterns) > built-in defaults. Reads config.AGENT_BIN at
    call time so a --agent-bin CLI override is honored."""
    name = bin_name or AGENT_BIN
    raw = _agent_section(name).get("error_patterns")
    if raw is None:
        raw = _AGENT_CFG.get("error_patterns")
    if raw is None:
        raw = _DEFAULT_ERROR_PATTERNS
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple)):
        log.warning("Config 'error_patterns' must be a string list; using defaults")
        raw = _DEFAULT_ERROR_PATTERNS
    patterns = tuple(str(p) for p in raw if str(p).strip())
    if not patterns:
        # explicit empty list disables signature rejection for this agent
        return ()
    try:
        return _compile_patterns(patterns)
    except re.error as exc:
        # A malformed configured matcher must not crash a long-running
        # review/batch process.  Revert to the known-safe built-ins instead
        # of silently disabling provider-failure rejection.
        log.warning("Invalid agent error pattern for %s (%s); using defaults",
                    name, exc)
        return _compile_patterns(_DEFAULT_ERROR_PATTERNS)


def agent_provider_error_patterns(bin_name: str = "") -> tuple:
    """Operational failures that count against provider health.

    This is deliberately narrower than ``agent_error_patterns``: malformed
    task requests and generic CLI banners still reject an artifact, but do
    not trip a shared provider circuit unless explicitly configured here.
    """
    name = bin_name or AGENT_BIN
    section = _agent_section(name)
    raw = section.get("provider_error_patterns")
    if raw is None:
        raw = _AGENT_CFG.get("provider_error_patterns")
    legacy = False
    if raw is None:
        raw = section.get("error_patterns")
        if raw is None:
            raw = _AGENT_CFG.get("error_patterns")
        legacy = raw is not None
    if raw is None:
        raw = _DEFAULT_PROVIDER_ERROR_PATTERNS
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple)):
        log.warning("provider_error_patterns must be a string list; using defaults")
        raw = _DEFAULT_PROVIDER_ERROR_PATTERNS
    patterns = tuple(str(value) for value in raw if str(value).strip())
    if legacy:
        patterns = tuple(value for value in patterns
                         if value not in _ARTIFACT_ONLY_ERROR_PATTERNS)
    try:
        return _compile_patterns(patterns) if patterns else ()
    except re.error as exc:
        log.warning("Invalid provider error pattern for %s (%s); using defaults",
                    name, exc)
        return _compile_patterns(_DEFAULT_PROVIDER_ERROR_PATTERNS)
_COMMIT_CFG = _section("commit")
_PROJECT_CFG = _section("project")
_LOGGING_CFG = _section("logging")
_SESSION_CFG = _section("session")
_OUTPUT_CFG = _section("output")
_INPUT_CFG = _section("input")
_EVIDENCE_CFG = _section("evidence")
_PROMPT_CFG = _section("prompt")
_VALIDATION_CFG = _section("validation")
_COMMAND_CFG = _section("commands")
_MEMORY_CFG = _section("memory")
_TUI_CFG = _section("tui")
_CLASSIFIER_CFG = _section("classifier")
_LIMITS_CFG = _section("limits")

AGENT_BIN = _string_setting(_AGENT_CFG, "bin", "pi", allow_empty=False)
AGENT_DEFAULT_MODEL = _string_setting(_AGENT_CFG, "default_model", "")
# Invocation-level CLI override. It is intentionally empty by default so
# task/stage YAML thinking settings retain their historical precedence.
AGENT_THINKING_OVERRIDE = ""
AGENT_DEFAULT_TIMEOUT = _int_setting(_AGENT_CFG, "default_timeout", 900, 1)
AGENT_DEFAULT_WORKERS = _int_setting(_AGENT_CFG, "default_workers", 4, 1)


PROJECT_FRONTEND_ROOT = _string_setting(
    _PROJECT_CFG, "frontend_root", "src", allow_empty=False)
PROJECT_BACKEND_ROOT = _string_setting(
    _PROJECT_CFG, "backend_root", "src", allow_empty=False)
PROJECT_REPORT_ROOT = _string_setting(
    _PROJECT_CFG, "report_root", "docs", allow_empty=False)


def command_placeholder_values(cwd: str) -> dict[str, str]:
    """Resolved, unquoted values for validator/stage command placeholders."""
    return placeholder_values(cwd, PROJECT_FRONTEND_ROOT, PROJECT_BACKEND_ROOT,
                              PROJECT_REPORT_ROOT)
COMMIT_PREFIX_DEFAULT = _string_setting(_COMMIT_CFG, "prefix", "[pi-batch]")

# T3 (7x24 governance): log rotation thresholds; --log-max-bytes /
# --log-backups override per run. 5MB x 3 backups bounds a 24x7 log.
LOG_MAX_BYTES = _int_setting(_LOGGING_CFG, "max_bytes", 5 * 1024 * 1024, 0)
LOG_BACKUP_COUNT = _int_setting(_LOGGING_CFG, "backups", 3, 0)
# Model response bodies are artifacts/messages, not operational log records.
# auto streams them only to an interactive terminal; full/none are explicit
# overrides. A task without an output path still prints its final answer once.
STREAM_OUTPUT = _choice_setting(
    _LOGGING_CFG, "stream_output", "auto", ("auto", "full", "none"))

# T10 (7x24 governance): rotate (fork) the shared session past this size.
SESSION_MAX_BYTES = _int_setting(_SESSION_CFG, "max_bytes", 2 * 1024 * 1024, 1)
# Pi stores one JSON object per line. Normal assistant messages may approach
# OUTPUT_MAX_BYTES, so allow headroom for JSON escaping while refusing a
# pathological line before metering/compaction scans allocate it wholesale.
SESSION_LINE_MAX_BYTES = _int_setting(
    _SESSION_CFG, "line_max_bytes", 4 * 1024 * 1024, 1)

# T12e (governance): reject agent outputs above this size (default 2MB).
OUTPUT_MAX_BYTES = _int_setting(_OUTPUT_CFG, "max_bytes", 2 * 1024 * 1024, 1)

# External prompt/task/template files are trusted repository inputs, but a
# malformed or accidentally generated multi-gigabyte file must not be read
# wholesale before the prompt-size gate can reject it.  Keep this separate
# from OUTPUT_MAX_BYTES so operators can tune input and model-response
# budgets independently.
INPUT_MAX_BYTES = _int_setting(_INPUT_CFG, "max_bytes", 2 * 1024 * 1024, 1)

# Bound the total artifact content injected into any aggregate/meta prompt.
# Full artifacts stay on disk and truncation markers point back to them.
EVIDENCE_MAX_BYTES = _int_setting(_EVIDENCE_CFG, "max_bytes", 64 * 1024, 1)
# Bound path traversal and fence/manifest overhead independently of byte size.
EVIDENCE_MAX_SOURCES = _int_setting(_EVIDENCE_CFG, "max_sources", 64, 1)

# Agent prompts are passed as one `-p` argv value. Stay below Linux's usual
# 128KiB per-argument ceiling and reject predictably before subprocess spawn.
PROMPT_MAX_BYTES = _int_setting(_PROMPT_CFG, "max_bytes", 120 * 1024, 0)

# Validator diagnostics are useful only in small tails; cap both pipes so a
# noisy external gate cannot OOM the coordinator before its timeout.
VALIDATION_OUTPUT_MAX_BYTES = _int_setting(
    _VALIDATION_CFG, "output_max_bytes", 256 * 1024, 1)

# Post-stage hooks are repository-wide gates and may legitimately run longer
# than per-artifact validators. They still need the same bounded process-tree
# semantics so one noisy or orphan-producing hook cannot wedge a 7x24 run.
COMMAND_TIMEOUT = _int_setting(_COMMAND_CFG, "timeout", 600, 1)
COMMAND_OUTPUT_MAX_BYTES = _int_setting(
    _COMMAND_CFG, "output_max_bytes", 256 * 1024, 1)

# Progressive memory: preserve pi's raw JSONL sessions, while exposing only
# a small metadata manifest to the model unless it elects to read more.
MEMORY_MODE = _choice_setting(_MEMORY_CFG, "mode", "auto", ("auto", "on", "off"))
MEMORY_TENANT = _string_setting(_MEMORY_CFG, "tenant", "default", allow_empty=False)
MEMORY_SENSITIVITY = _choice_setting(
    _MEMORY_CFG, "sensitivity", "internal",
    ("public", "internal", "confidential", "secret", "regulated"))
TUI_JOURNAL_ENABLED = _bool_setting(_TUI_CFG, "journal_enabled", True)
TUI_JOURNAL_MAX_BYTES = _int_setting(
    _TUI_CFG, "journal_max_bytes", 16 * 1024 * 1024, 1)
TUI_COMMAND_ARTIFACT_MAX_BYTES = _int_setting(
    _TUI_CFG, "command_artifact_max_bytes", 2 * 1024 * 1024, 64 * 1024)
TUI_COMMAND_ARTIFACT_STORE_MAX_BYTES = _int_setting(
    _TUI_CFG, "command_artifact_store_max_bytes",
    max(64 * 1024 * 1024, TUI_COMMAND_ARTIFACT_MAX_BYTES),
    TUI_COMMAND_ARTIFACT_MAX_BYTES)
TUI_COMMAND_ARTIFACT_MAX_FILES = _int_setting(
    _TUI_CFG, "command_artifact_max_files", 256, 1)
_MEMORY_AGENT_VALUE = _MEMORY_CFG.get("agent_names", ("pi",))
if isinstance(_MEMORY_AGENT_VALUE, str):
    _MEMORY_AGENT_VALUE = (_MEMORY_AGENT_VALUE,)
elif not isinstance(_MEMORY_AGENT_VALUE, (list, tuple)):
    log.warning("Config value 'agent_names' must be a string or list; using pi")
    _MEMORY_AGENT_VALUE = ("pi",)
MEMORY_AGENT_NAMES = tuple(str(value) for value in _MEMORY_AGENT_VALUE)
# ── 运行时数据目录（data_dir）───────────────────────────────────────
# 运行状态（记忆索引/被拒产物/worktrees/设备状态/预算/流程工件/反思）
# 默认位于 ~/.codex/pbatch/projects/<workspace-id>/，不污染项目源码；
# --data-dir / PBATCH_DATA_DIR 可显式指定兼容目录或共享运行根。
# 锁仍按 cwd 保护工作区，与数据位置解耦（单实例语义不变）。
_DATA_DIR_OVERRIDE = ""  # CLI --data-dir（进程级，优先于 env）


def data_dir(cwd: str = "") -> Path:
    """Return the private per-workspace runtime root.

    Explicit CLI/environment overrides retain full control.  The default is a
    stable directory under ``~/.codex/pbatch`` so sessions, rejected
    diagnostics, journals, campaign state, logs and generated reports never
    appear in the repository merely because the command was run there.
    """
    override = _DATA_DIR_OVERRIDE or os.environ.get("PBATCH_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    from .user_dirs import project_runtime_dir
    base = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    return project_runtime_dir(str(base))


def set_data_dir_override(path: str) -> None:
    """CLI --data-dir 设置（早于 env，进程级）。"""
    global _DATA_DIR_OVERRIDE
    _DATA_DIR_OVERRIDE = path.strip()


MEMORY_INDEX_FILE = _string_setting(
    _MEMORY_CFG, "index_file", ".pi-batch/memory/sessions.index.jsonl", allow_empty=False)
MEMORY_MANIFEST_RECENT = _int_setting(_MEMORY_CFG, "manifest_recent", 3, 0)
MEMORY_MANIFEST_SCAN = _int_setting(_MEMORY_CFG, "manifest_scan", 30, 1)
MEMORY_MANIFEST_MAX_BYTES = _int_setting(_MEMORY_CFG, "manifest_max_bytes", 4096, 1)
MEMORY_READ_MAX_BYTES = _int_setting(_MEMORY_CFG, "read_max_bytes", 65536, 1)
MEMORY_INDEX_LINE_MAX_BYTES = _int_setting(
    _MEMORY_CFG, "index_line_max_bytes", 256 * 1024, 1)
MEMORY_INDEX_MAX_BYTES = _int_setting(
    _MEMORY_CFG, "index_max_bytes", 16 * 1024 * 1024, 1)

# F line (round-5 F): provider rate limiting. A process-wide token bucket
# serializes agent invocations across parallel workers / meta role fan-out;
# --min-interval only throttles successful SERIAL tasks. 0 = unlimited.
RATE_LIMIT_PER_SECOND = _float_setting(_LIMITS_CFG, "per_second", 0.0, 0.0)
RATE_LIMIT_BURST = _float_setting(_LIMITS_CFG, "burst", 1.0, 0.0)
if not math.isfinite(RATE_LIMIT_PER_SECOND):
    RATE_LIMIT_PER_SECOND = 0.0
if not math.isfinite(RATE_LIMIT_BURST) or RATE_LIMIT_BURST <= 0:
    RATE_LIMIT_BURST = 1.0
_LIMIT_PROVIDERS = _LIMITS_CFG.get("providers")


def _rate_provider_overrides(values) -> dict[str, float]:
    """Normalize provider rates; an explicit zero disables that provider."""
    if not isinstance(values, dict):
        return {}
    overrides: dict[str, float] = {}
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        try:
            number = float(value)
        except OverflowError:
            continue
        if math.isfinite(number) and number >= 0:
            overrides[str(key)] = number
    return overrides


RATE_LIMIT_PROVIDERS = _rate_provider_overrides(_LIMIT_PROVIDERS)


# Each fabric capability remains independently off by default. Loading the
# module never implies scanning, execution, migration, or installation.
_DEVICE_FABRIC = load_device_fabric(_section("device_fabric"))
DEVICE_FABRIC_ENABLED = _DEVICE_FABRIC.enabled
DEVICE_FABRIC_MODE = _DEVICE_FABRIC.mode
DEVICE_FABRIC_LAN_SCAN = _DEVICE_FABRIC.lan_scan
DEVICE_FABRIC_BENCHMARK = _DEVICE_FABRIC.benchmark
DEVICE_FABRIC_AUTO_INSTALL = _DEVICE_FABRIC.auto_install


# Shared agent-run ledger (multi-agent visibility): off by default, like
# device_fabric — an unconfigured project must not start writing runtime
# state into repositories that ignore nothing (ledger writes are runtime
# data under data_dir/, which unrelated test/consumer repos do not
# necessarily gitignore).
AGENT_LEDGER_ENABLED = bool(
    _section("agent_ledger").get("enabled", False))
DEVICE_FABRIC_APPROVAL_REQUIRED = _DEVICE_FABRIC.approval_required
DEVICE_FABRIC_MTLS_REQUIRED = _DEVICE_FABRIC.mtls_required
DEVICE_FABRIC_SHORT_LIVED_CREDENTIALS = _DEVICE_FABRIC.short_lived_credentials
DEVICE_FABRIC_STATIC = _DEVICE_FABRIC.static
DEVICE_FABRIC_CLUSTERS = _DEVICE_FABRIC.clusters

# Task type classifier (pbatch/classifier.py): deterministic keyword gate
# that runs BEFORE execution and routes frontend UI tasks to the UI
# generation pipeline (--classify / `pi-batch.py classify`).
CLASSIFIER_KEYWORDS = _string_setting(_CLASSIFIER_CFG, "keywords", "")
CLASSIFIER_FRONTEND_PIPELINE = _string_setting(
    _CLASSIFIER_CFG, "frontend_pipeline",
    "examples/frontend-implementation-pipeline.yaml", allow_empty=False)
CLASSIFIER_BACKEND_PIPELINE = _string_setting(
    _CLASSIFIER_CFG, "backend_pipeline",
    "examples/backend-implementation-pipeline.yaml", allow_empty=False)
# Minimum top score for a confident classification (2 = one strong hit).
CLASSIFIER_MIN_SCORE = _int_setting(_CLASSIFIER_CFG, "min_score", 2, 1)
# Route when this share of a batch's tasks classify as frontend UI.
CLASSIFIER_FRONTEND_RATIO = _float_setting(
    _CLASSIFIER_CFG, "frontend_ratio", 0.5, 0.0, 1.0)
CLASSIFIER_BACKEND_RATIO = _float_setting(
    _CLASSIFIER_CFG, "backend_ratio", 0.5, 0.0, 1.0)
CLASSIFIER_TIE_POLICY = _choice_setting(
    _CLASSIFIER_CFG, "tie_policy", "frontend", ("plain", "frontend", "backend"))

_DEFAULT_SESSION_FLAGS = {
    "start": ["--session-id", "{session}", "--name", "{name}"],
    "continue": ["--session-id", "{session}"],
    "fork": ["--fork", "{session}"],
}


class _SessionFlags(list):
    """Rendered argv tokens carrying the logical session context."""

    def __init__(self, values: list[str], kind: str, session_id: str,
                 session_name: str):
        super().__init__(values)
        self.kind = kind
        self.session_id = session_id
        self.session_name = session_name


def agent_capture_thread_id(bin_name: str = "") -> bool:
    """Whether the selected provider exposes a capturable thread id."""
    return _bool_setting(
        _agent_section(bin_name or AGENT_BIN), "capture_thread_id", False)


def agent_session_flags(key: str, session_id: str, session_name: str,
                        bin_name: str = "",
                        provider_session: Optional[str] = None) -> list:
    """Resolve session flags for a call (start/continue/fork), replacing
    {session} and {name} placeholders. A {prompt} placeholder is retained
    for :func:`build_agent_argv` to render at the session-led position. Per-agent override
    (agent.agents.<bin>.session_flags) wins over agent.session_flags,
    then pi-style defaults — so a non-pi agent CLI can point these at its
    own continue-session flags without touching shared config (G line)."""
    configured = _agent_section(bin_name or AGENT_BIN).get("session_flags")
    if configured is None:
        configured = _AGENT_CFG.get("session_flags")
    cfg = configured if isinstance(configured, dict) else {}
    flags = cfg.get(key)
    if (not isinstance(flags, (list, tuple))
            or not all(isinstance(flag, str) for flag in flags)):
        if flags is not None:
            log.warning("Config session_flags.%s must be a string list; using defaults", key)
        flags = _DEFAULT_SESSION_FLAGS[key]
    rendered_session = session_id if provider_session is None else provider_session
    values = [f.replace("{session}", rendered_session).replace("{name}", session_name)
              for f in flags]
    return _SessionFlags(values, key, session_id, session_name)

def _session_flags(key: str, session_id: str, session_name: str) -> list:
    """Backward-compatible alias resolving flags for the configured bin."""
    return agent_session_flags(key, session_id, session_name)

def _load_validators() -> dict:
    """Read validators from the already merged project/user configuration.

    The rest of this module resolves settings from ``_BATCH_CFG``.  Loading
    the registry from the project file alone would make user-level validator
    additions and overrides silently ineffective, violating the documented
    precedence contract.
    """
    registry = _BATCH_CFG.get("validators")
    if not isinstance(registry, dict):
        return {}
    return expand_validator_registry(
        registry, PROJECT_FRONTEND_ROOT, PROJECT_BACKEND_ROOT, PROJECT_REPORT_ROOT)

VALIDATORS = _load_validators()

class ValidatorSpec(NamedTuple):
    """One resolved validator entry.

    cmd:   shell command ({output}/{cwd} placeholders)
    judge: when True, the gate parses a VERDICT: PASS|FAIL - <reason> line
           from stdout (fail closed on missing/bare verdict) instead of
           trusting the exit code alone — LLM-as-judge protocol (H line)
    scope: "file" = run per artifact before it is committed (default);
           "repo" = full-tree gate deferred to stage end / batch end so it
           never races concurrent role agents writing the tree (N2)
    requires_any: relative file paths; the validator is applicable only when
                  at least one path exists under the target working directory
    """
    cmd: str
    judge: bool = False
    scope: str = "file"
    requires_any: tuple = ()


def _required_paths(entry) -> tuple:
    """Normalize an optional validator applicability file list."""
    raw = entry.get("requires_any", ()) if isinstance(entry, dict) else ()
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, (list, tuple)):
        log.warning("Validator requires_any must be a string or string list; ignoring")
        return ()
    paths = tuple(item.strip() for item in raw
                  if isinstance(item, str) and item.strip())
    if len(paths) != len(raw):
        log.warning("Validator requires_any contains invalid paths; ignoring invalid entries")
    return paths

def _validator_spec(entry) -> Optional[ValidatorSpec]:
    """Normalize one registry entry: a string command, or a mapping with
    cmd/judge/scope keys. Malformed entries fall back to a raw command so a
    typo fails deterministically at execution, not at import."""
    if isinstance(entry, str):
        return ValidatorSpec(cmd=entry)
    if isinstance(entry, dict):
        cmd = entry.get("cmd")
        if isinstance(cmd, str) and cmd.strip():
            judge = bool(entry.get("judge", False))
            scope = str(entry.get("scope", "file")).lower()
            if scope not in ("file", "repo"):
                log.warning("Validator scope '%s' must be file or repo; using file", scope)
                scope = "file"
            return ValidatorSpec(cmd=cmd.strip(), judge=judge, scope=scope,
                                 requires_any=_required_paths(entry))
    log.warning("Validator entry must be a command string or a {cmd, judge, scope} "
                "mapping; skipping %r", entry)
    return None

VALIDATOR_SPECS: dict = {name: spec for name, entry in VALIDATORS.items()
                         if (spec := _validator_spec(entry)) is not None}


def validator_applies(spec: ValidatorSpec, cwd: str = "") -> bool:
    """Return whether a validator's declared applicability files are present.

    ``requires_any`` is an explicit opt-in for cross-language registries: an
    absent prerequisite means the gate is not applicable, not that a failed
    command should be hidden. Callers log the skip so the decision remains
    observable and easy to audit.
    """
    required = tuple(getattr(spec, "requires_any", ()) or ())
    if not required:
        return True
    root = Path(cwd or os.getcwd()).resolve()
    for raw in required:
        candidate = Path(raw).expanduser()
        if candidate.is_absolute():
            matches = (candidate,)
        elif any(marker in raw for marker in "*?["):
            matches = root.glob(raw)
        else:
            matches = (root / candidate,)
        if any(path.is_file() for path in matches):
            return True
    return False

def _resolve_validator_specs(value: str) -> list:
    """Resolve registry names, jsonschema=PATH, and raw commands."""
    out = []
    for item in [x.strip() for x in (value or "").split(",") if x.strip()]:
        spec = VALIDATOR_SPECS.get(item)
        command = schema_validator_command(item)
        out.append(spec if spec is not None else ValidatorSpec(cmd=command or item))
    return out

def _resolve_validators(value: str) -> list:
    """Backward-compatible alias: command strings only (judge/scope ignored)."""
    return [spec.cmd for spec in _resolve_validator_specs(value)]
