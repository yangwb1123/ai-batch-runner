"""Convert validated pipeline stage mappings into runtime data models."""

from __future__ import annotations

from .config import (AGENT_DEFAULT_WORKERS, COMMAND_OUTPUT_MAX_BYTES,
                     COMMAND_TIMEOUT)
from .models import Stage


def parse_stage_def(value: dict, global_git_commit: bool) -> Stage:
    """Build a Stage after pipeline_schema has validated its declaration."""
    fields = {**_identity_fields(value), **_source_fields(value),
              **_meta_fields(value), **_execution_fields(value, global_git_commit)}
    return Stage(**fields)


def _identity_fields(value: dict) -> dict:
    return {
        "name": value.get("name", ""),
        "agent": value.get("agent", ""),
        "model": value.get("model", ""),
        "provider": value.get("provider", ""),
        "orchestrator_model": value.get("orchestrator_model", ""),
        "orchestrator_provider": value.get("orchestrator_provider", ""),
        "worker_model": value.get("worker_model", ""),
        "worker_provider": value.get("worker_provider", ""),
        "role_models": dict(value.get("role_models", {})),
        "role_providers": dict(value.get("role_providers", {})),
        "delegation_only": value.get("delegation_only", False),
        "meta_resume": value.get("meta_resume", False),
        "meta_task_dag": value.get("meta_task_dag", False),
    }


def _source_fields(value: dict) -> dict:
    return {
        "from_dir": value.get("from_dir", ""),
        "from_outputs": value.get("from_outputs", ""),
        "suffix": value.get("suffix", ".md"),
        "output_suffix": value.get("output_suffix", ".out.md"),
        "mode": value.get("mode", "serial"),
        "workers": value.get("workers", AGENT_DEFAULT_WORKERS),
        "aggregate": value.get("aggregate", False),
        "validate_cmd": value.get("validate_cmd", value.get("validate")),
        "from_prompt": value.get("from_prompt", ""),
        "output": value.get("output", ""),
        "tasks": value.get("tasks", []),
        "cwd": value.get("cwd", ""),
    }


def _meta_fields(value: dict) -> dict:
    return {
        "meta": value.get("meta", False),
        "meta_prompt": value.get("meta_prompt", ""),
        "role_dir": value.get("role_dir", ""),
        "role_keywords": value.get("role_keywords", ""),
        "output_dir": value.get("output_dir", ""),
        "max_iterations": value.get("max_iterations", 3),
        "max_roles_per_iteration": value.get("max_roles_per_iteration", 3),
        "meta_timeout": value.get("meta_timeout", 0),
        "relevance_enabled": value.get("relevance_enabled", True),
        "relevance_min_score": value.get("relevance_min_score", 0),
        "meta_max_failed_roles": value.get("meta_max_failed_roles", 0),
        "meta_role_retries": value.get("meta_role_retries", 1),
    }


def _execution_fields(value: dict, global_git_commit: bool) -> dict:
    return {
        "gate": value.get("gate", False),
        "gate_fix_rounds": value.get("gate_fix_rounds", 0),
        "gate_fix_prompt": value.get("gate_fix_prompt", ""),
        "gate_fix_validate": value.get("gate_fix_validate"),
        "approval": value.get("approval", False),
        "commands": value.get("commands", []),
        "commands_parallel": value.get("commands_parallel", False),
        "command_timeout": value.get("command_timeout", COMMAND_TIMEOUT),
        "command_output_max_bytes": value.get(
            "command_output_max_bytes", COMMAND_OUTPUT_MAX_BYTES),
        "verify_experiment": value.get("verify_experiment", []),
        "git_commit": value.get("git_commit", global_git_commit),
        "commit_message": value.get("commit_message", ""),
        "read_only": value.get("read_only", False),
        "read_only_paths": list(value.get("read_only_paths", [])),
        "or_tasks": value.get("or_tasks", False),
    }
