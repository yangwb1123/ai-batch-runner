"""pbatch: the pi-batch runner, organized as a package.

Thin entry script pi-batch.py re-exports everything; importing pbatch
directly also works.
"""

from .config import (AGENT_BIN, AGENT_DEFAULT_MODEL, AGENT_DEFAULT_TIMEOUT,
                     AGENT_DEFAULT_WORKERS, COMMIT_PREFIX_DEFAULT, VALIDATORS,
                     log, yaml)
from .models import Pipeline, Stage, Task, TaskResult
from .agent_session import (
    AgentSession, AgentSessionOptions, CompactionTimeout, PromptOutcome,
    QueueReceipt, TurnResult, create_agent_session,
)
from .tui_actor_tree import ActorNode, ActorTreeSnapshot
from .tui_native import (
    NativePipelineApprovalChannel, NativePipelineAttachment,
    NativePipelineControlChannel, NativePipelineObserver)
from .tui_runtime import (
    ActorControlRuntime, ActorInterruptRequest, ActorSteerRequest,
    CompactionHandle, RuntimeCompactionResult, RuntimeContextUsage,
    RuntimeModelCatalog, RuntimeModelRef, RuntimeSessionState, RuntimeSessionStats,
    RuntimeThinkingLevels,
    RuntimeTokenTotals,
)
from .runner import (ValidationResult, agent_failure_reason, print_summary,
                     revalidate_existing, run_parallel, run_serial, run_task,
                     run_validation, run_argv, save_result)
from .reuse import fingerprint as _fp_export
from .reuse import reuse_decision
from .reuse import sidecar_path as _scp_export
from .reuse import write_sidecar as _wsc_export
fingerprint = _fp_export
sidecar_path = _scp_export
write_sidecar = _wsc_export
from .config import _session_flags
from .pipeline import _archive_outputs
from .runner import _resolve_validators
from .pipeline import (execute_stage, load_pipeline, load_tasks,
                       load_tasks_from_dir, run_pipeline)
from .cli import build_parser, main

__all__ = [
    "AGENT_BIN", "AGENT_DEFAULT_MODEL", "AGENT_DEFAULT_TIMEOUT",
    "AGENT_DEFAULT_WORKERS", "COMMIT_PREFIX_DEFAULT", "VALIDATORS", "log", "yaml",
    "Pipeline", "Stage", "Task", "TaskResult",
    "AgentSession", "AgentSessionOptions", "CompactionHandle",
    "CompactionTimeout", "PromptOutcome", "QueueReceipt",
    "ActorControlRuntime", "ActorInterruptRequest", "ActorSteerRequest",
    "RuntimeCompactionResult", "RuntimeContextUsage", "RuntimeModelCatalog",
    "RuntimeModelRef", "RuntimeThinkingLevels",
    "RuntimeSessionState", "RuntimeSessionStats", "RuntimeTokenTotals",
    "TurnResult", "create_agent_session", "NativePipelineApprovalChannel",
    "NativePipelineAttachment", "NativePipelineControlChannel",
    "NativePipelineObserver",
    "ActorNode", "ActorTreeSnapshot",
    "agent_failure_reason", "print_summary", "run_parallel", "run_serial",
    "run_task", "save_result", "run_validation", "revalidate_existing",
    "run_argv",
    "ValidationResult", "reuse_decision", "fingerprint", "sidecar_path", "write_sidecar",
    "execute_stage", "load_pipeline", "load_tasks",
    "load_tasks_from_dir", "run_pipeline", "build_parser", "main",
    "_session_flags", "_archive_outputs", "_resolve_validators",
]
