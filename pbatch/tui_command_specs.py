"""Single-source command metadata for the product TUI."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable, Optional


SETTING_BUSY = "runtime-setting"
COMPACTION_BUSY = "compaction"
REVIEW_BUSY = "review"
TURN_BUSY = "turn"


@dataclass(frozen=True)
class TUICommandSpec:
    """One slash command and its non-authoritative presentation metadata."""

    name: str
    usage: str
    description: str
    handler_name: str
    aliases: tuple[str, ...] = ()
    extra_help: tuple[tuple[str, str], ...] = ()
    palette_title: str = ""
    palette_description: str = ""
    palette_handler: str = ""
    palette_slash: bool = False
    allowed_while: frozenset[str] = field(default_factory=frozenset)
    risk: str = "read"

    @property
    def spellings(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


def _allowed(*states: str) -> frozenset[str]:
    return frozenset(states)


COMMAND_SPECS = (
    TUICommandSpec(
        "help", "help", "show this help", "_slash_help",
        palette_title="Slash command help",
        palette_description="List TUI commands", palette_handler="_palette_help",
        allowed_while=_allowed(SETTING_BUSY, COMPACTION_BUSY)),
    TUICommandSpec(
        "status", "status", "show the current backend/session snapshot",
        "_slash_status", palette_title="Show status",
        palette_description="Display backend snapshot",
        palette_handler="_palette_status",
        allowed_while=_allowed(SETTING_BUSY, COMPACTION_BUSY)),
    TUICommandSpec(
        "session", "session",
        "show typed state, usage, context, and cost for the active runtime session",
        "_slash_session", palette_title="Runtime session",
        palette_description="Show typed active-session state and usage",
        palette_handler="_slash_session", palette_slash=True,
        allowed_while=_allowed(COMPACTION_BUSY, REVIEW_BUSY, TURN_BUSY)),
    TUICommandSpec(
        "compact", "compact [instructions]",
        "compact older context in the background, preserving optional guidance",
        "_slash_compact", allowed_while=_allowed(COMPACTION_BUSY), risk="lossy"),
    TUICommandSpec(
        "new", "new [name]", "start a new backend session", "_slash_new",
        palette_title="New session", palette_description="Reset backend session",
        palette_handler="action_new_session", risk="mutation"),
    TUICommandSpec(
        "sessions", "sessions", "open the scoped keyboard session picker",
        "_slash_sessions", palette_title="Sessions",
        palette_description="Find and restore TUI sessions",
        palette_handler="_palette_sessions"),
    TUICommandSpec(
        "native-recover", "native-recover <session-id>",
        "open an exact native Pipeline journal as a read-only viewer",
        "_slash_native_recover", risk="read"),
    TUICommandSpec(
        "resume", "resume <session-id>",
        "restore trusted history and provider context when supported",
        "_slash_resume", risk="mutation"),
    TUICommandSpec(
        "fork", "fork [session-id]",
        "fork trusted provider context into a new local session", "_slash_fork",
        risk="mutation"),
    TUICommandSpec(
        "archive", "archive <session-id>",
        "locally archive an inactive session without deleting history",
        "_slash_archive", risk="mutation"),
    TUICommandSpec(
        "unarchive", "unarchive <session-id>",
        "restore a locally archived session", "_slash_unarchive", risk="mutation"),
    TUICommandSpec(
        "provider-archive", "provider-archive <session-id>",
        "archive a provider thread without changing local journal state",
        "_slash_provider_archive", risk="mutation"),
    TUICommandSpec(
        "provider-unarchive", "provider-unarchive <session-id>",
        "restore a provider thread without changing local journal state",
        "_slash_provider_unarchive", risk="mutation"),
    TUICommandSpec(
        "queue", "queue [TEXT|list|run|clear|drop N|edit N TEXT]",
        "manage process-local follow-ups", "_slash_queue",
        palette_title="Follow-up queue",
        palette_description="Inspect queued prompts", palette_handler="_palette_queue",
        allowed_while=_allowed(TURN_BUSY), risk="mutation"),
    TUICommandSpec(
        "steer", "steer <text>",
        "redirect the active turn only when the runtime declares support",
        "_slash_steer", allowed_while=_allowed(TURN_BUSY), risk="mutation"),
    TUICommandSpec(
        "actor-steer", "actor-steer <actor-id> <text>",
        "steer one active typed actor when exact runtime control is supported",
        "_slash_actor_steer", allowed_while=_allowed(TURN_BUSY), risk="mutation"),
    TUICommandSpec(
        "actor-stop", "actor-stop <actor-id>",
        "stop one active typed actor when exact runtime control is supported",
        "_slash_actor_stop", allowed_while=_allowed(TURN_BUSY), risk="mutation"),
    TUICommandSpec(
        "approvals", "approvals",
        "reopen the current typed approval card without parsing transcript text",
        "_slash_approvals", palette_title="Typed approvals",
        palette_description="Inspect pending runtime approvals",
        palette_handler="_palette_approvals",
        allowed_while=_allowed(REVIEW_BUSY, TURN_BUSY)),
    TUICommandSpec(
        "clear", "clear", "clear the visible conversation only", "_slash_clear",
        palette_title="Clear conversation",
        palette_description="Clear visible messages", palette_handler="action_clear",
        risk="mutation"),
    TUICommandSpec(
        "runtime", "runtime [auto|app-server|pi-rpc|structured|legacy]",
        "show or select the TUI runtime", "_slash_runtime", risk="mutation"),
    TUICommandSpec(
        "model", "model [query]",
        "open the fuzzy picker or select an exact Pi session model",
        "_slash_model", palette_title="Runtime model",
        palette_description="Browse or select the active session model",
        palette_handler="_slash_model", palette_slash=True, risk="mutation"),
    TUICommandSpec(
        "thinking", "thinking [level]",
        "open the picker or select an exact current Pi thinking level",
        "_slash_thinking", palette_title="Thinking level",
        palette_description="Browse or select the active session thinking level",
        palette_handler="_slash_thinking", palette_slash=True, risk="mutation"),
    TUICommandSpec(
        "provider", "provider [name]", "show or change the provider",
        "_slash_provider", risk="mutation"),
    TUICommandSpec(
        "agent", "agent [binary]", "show or change the agent CLI",
        "_slash_agent", risk="mutation"),
    TUICommandSpec(
        "memory", "memory [auto|on|off]", "show or change memory mode",
        "_slash_memory", risk="mutation"),
    TUICommandSpec(
        "agents", "agents [agent]",
        "inspect the current and latest shared agent runs", "_slash_agents",
        aliases=("runs",), palette_title="Agent runs",
        palette_description="Inspect shared agent activity",
        palette_handler="_palette_agents",
        allowed_while=_allowed(REVIEW_BUSY, TURN_BUSY)),
    TUICommandSpec(
        "breaker", "breaker", "show shared provider breaker state",
        "_slash_breaker", palette_title="Provider breaker",
        palette_description="Show provider cooldown state",
        palette_handler="_slash_breaker", palette_slash=True),
    TUICommandSpec(
        "files", "files", "show changed workspace files", "_slash_files",
        palette_title="Changed files",
        palette_description="List dirty workspace files",
        palette_handler="_palette_files"),
    TUICommandSpec(
        "diff", "diff [path]", "review the current unified diff", "_slash_diff"),
    TUICommandSpec(
        "plan", "plan", "open the current trusted plan projection", "_slash_plan",
        palette_title="Plan inspector",
        palette_description="Inspect the current projected plan",
        palette_handler="_palette_plan"),
    TUICommandSpec(
        "completion", "completion",
        "inspect current-turn outcome, changes, checks, risks, and usage",
        "_slash_completion", palette_title="Completion",
        palette_description="Inspect current-turn completion evidence",
        palette_handler="_palette_completion",
        allowed_while=_allowed(REVIEW_BUSY, TURN_BUSY)),
    TUICommandSpec(
        "artifacts", "artifacts [N]",
        "inspect explicit current-turn artifact refs and safe local previews",
        "_slash_artifacts", aliases=("artifact",), palette_title="Artifacts",
        palette_description="Preview trusted current-turn artifact refs",
        palette_handler="_palette_artifacts",
        allowed_while=_allowed(REVIEW_BUSY, TURN_BUSY)),
    TUICommandSpec(
        "changes", "changes [all|unstaged|staged|untracked|last-turn] [path]",
        "inspect files and hunks by Git scope", "_slash_changes",
        extra_help=(("changes base|commit <ref> [path]",
                     "inspect a validated Git ref comparison"),),
        palette_title="Changes inspector",
        palette_description="Inspect files and the current Git diff",
        palette_handler="_palette_changes"),
    TUICommandSpec(
        "product", "product [requirement]",
        "draft a local product-direction brief", "_slash_product",
        palette_title="Product direction",
        palette_description="Draft a local product brief",
        palette_handler="_palette_product"),
    TUICommandSpec(
        "review", "review [scope]", "run a read-only AI review",
        "_slash_review", palette_title="Run AI review",
        palette_description="Run a read-only structured review",
        palette_handler="_palette_review"),
    TUICommandSpec(
        "pipeline", "pipeline <file> [--reuse]",
        "run an explicit native Pipeline with a read-only TUI projection",
        "_slash_pipeline", palette_title="Run native Pipeline",
        palette_description="Start a Pipeline and observe its typed lifecycle",
        palette_handler="_palette_pipeline", risk="mutation"),
    TUICommandSpec(
        "findings", "findings", "inspect structured review findings and jump to Changes",
        "_slash_findings", palette_title="Review findings",
        palette_description="Inspect findings and locate their changed file",
        palette_handler="_palette_findings",
        allowed_while=_allowed(REVIEW_BUSY, TURN_BUSY)),
    TUICommandSpec(
        "refresh", "refresh", "refresh workspace and backend status",
        "_slash_refresh", palette_title="Refresh workspace",
        palette_description="Refresh Git and backend status",
        palette_handler="_palette_refresh"),
    TUICommandSpec(
        "doctor", "doctor", "run local environment diagnostics", "_slash_doctor",
        palette_title="Run doctor", palette_description="Check local environment",
        palette_handler="_palette_doctor"),
    TUICommandSpec(
        "timeline", "timeline", "cycle the trusted execution timeline view",
        "_slash_timeline"),
    TUICommandSpec(
        "quit", "quit", "cancel active work and leave the TUI", "_slash_quit",
        allowed_while=_allowed(SETTING_BUSY, COMPACTION_BUSY), risk="mutation"),
)


def _build_name_map() -> dict[str, TUICommandSpec]:
    result: dict[str, TUICommandSpec] = {}
    for spec in COMMAND_SPECS:
        for name in spec.spellings:
            if not name or name != name.lower() or name in result:
                raise RuntimeError(f"invalid or duplicate TUI command: {name!r}")
            result[name] = spec
    return result


_COMMAND_BY_NAME = _build_name_map()


def command_spec(name: str) -> Optional[TUICommandSpec]:
    """Resolve a canonical command or alias without fuzzy execution."""
    return _COMMAND_BY_NAME.get(str(name).casefold())


def command_spellings() -> tuple[str, ...]:
    """Return the bounded local builtin names used by composer completion."""
    return tuple(sorted(_COMMAND_BY_NAME))


def command_allowed_while(name: str, state: str) -> bool:
    """Check the registry policy; handlers still own final authorization."""
    spec = command_spec(name)
    return bool(spec is not None and state in spec.allowed_while)


def palette_specs() -> tuple[TUICommandSpec, ...]:
    return tuple(spec for spec in COMMAND_SPECS if spec.palette_title)


def palette_callback(owner: Any, spec: TUICommandSpec) -> Callable[[], Any]:
    callback = getattr(owner, spec.palette_handler)
    return partial(callback, ()) if spec.palette_slash else callback


def render_slash_help() -> str:
    lines = ["Available commands:", ""]
    for spec in COMMAND_SPECS:
        lines.append(f"- `/{spec.usage}` — {spec.description}")
        suffix = spec.usage[len(spec.name):]
        for alias in spec.aliases:
            lines.append(f"- `/{alias}{suffix}` — alias for `/{spec.name}`")
        for usage, description in spec.extra_help:
            lines.append(f"- `/{usage}` — {description}")
    return "\n".join(lines)
