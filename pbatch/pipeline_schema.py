"""Fail-closed structural validation for declarative pipeline YAML."""

from __future__ import annotations


_TOP_LEVEL_FIELDS = frozenset(("stages", "decision_log", "archive_dir", "git_commit"))
_STAGE_FIELDS = frozenset((
    "name", "agent", "model", "provider", "orchestrator_model",
    "orchestrator_provider", "worker_model", "worker_provider", "role_models",
    "role_providers", "delegation_only", "meta_resume", "meta_task_dag", "from_dir", "from_outputs", "suffix",
    "output_suffix", "mode", "workers", "aggregate", "validate",
    "validate_cmd", "meta", "meta_prompt", "role_dir", "role_keywords",
    "output_dir", "max_iterations", "max_roles_per_iteration", "meta_timeout",
    "relevance_enabled", "relevance_min_score", "meta_max_failed_roles",
    "meta_role_retries", "gate", "gate_fix_rounds", "gate_fix_prompt",
    "gate_fix_validate", "approval", "from_prompt", "output", "tasks",
    "commands", "commands_parallel", "command_timeout",
    "command_output_max_bytes", "cwd", "git_commit", "commit_message",
    "read_only", "read_only_paths", "or_tasks", "verify_experiment",
))
_TASK_FIELDS = frozenset((
    "prompt", "prompt_template", "output", "agent", "model", "provider", "thinking",
    "tools", "exclude_tools", "no_tools", "cwd", "timeout", "env", "validate",
    # `role` is descriptive metadata used by the shipped pipeline templates.
    # `validate_cmd` is the legacy spelling of the task-level `validate` alias.
    "role", "validate_cmd",
))
_STRING_FIELDS = ("agent", "model", "provider", "orchestrator_model",
                  "orchestrator_provider", "worker_model", "worker_provider",
                  "from_dir", "suffix", "output_suffix",
                  "meta_prompt", "role_dir", "role_keywords", "output_dir",
                  "from_prompt", "output", "cwd", "commit_message",
                  "gate_fix_prompt")
_BOOL_FIELDS = ("aggregate", "meta", "delegation_only", "meta_resume", "relevance_enabled",
                "gate", "approval", "commands_parallel", "git_commit",
                "read_only", "or_tasks", "meta_task_dag")
_TASK_STRING_FIELDS = ("prompt", "prompt_template", "output", "agent", "model", "provider",
                       "thinking", "tools", "exclude_tools", "cwd", "role")
_NULLABLE_TASK_STRINGS = ("model", "provider", "thinking", "tools", "exclude_tools")


def validate_pipeline(data) -> list[str]:
    """Return every structural error before a pipeline can start work."""
    if not isinstance(data, dict):
        return ["top level must be a mapping"]
    stages = data.get("stages")
    if not isinstance(stages, list) or not stages:
        return ["stages must be a non-empty list"]
    errors = _unknown_field_errors(data, _TOP_LEVEL_FIELDS, "top level")
    for key in ("decision_log", "archive_dir"):
        if key in data and not isinstance(data[key], str):
            errors.append(f"{key} must be a string")
    if "git_commit" in data and not isinstance(data["git_commit"], bool):
        errors.append("git_commit must be boolean")
    seen = set()
    for index, stage in enumerate(stages, 1):
        stage_errors, name = _validate_stage(stage, index, seen)
        errors.extend(stage_errors)
        if name:
            seen.add(name)
    return errors


def _validate_stage(stage, index: int, seen: set[str]) -> tuple[list[str], str]:
    label = f"stage[{index}]"
    if not isinstance(stage, dict):
        return [f"{label} must be a mapping"], ""
    errors = []
    name = stage.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append(f"{label}.name must be a non-empty string")
        name = ""
    else:
        name = name.strip()
    if name and name in seen:
        errors.append(f"{label}.name duplicates earlier stage '{name}'")
    prefix = f"stage '{name}'" if name else label
    errors.extend(_unknown_field_errors(stage, _STAGE_FIELDS, prefix))
    errors.extend(_field_errors(stage, prefix))
    errors.extend(_source_errors(stage, prefix, seen))
    errors.extend(_task_errors(stage.get("tasks", []), prefix))
    return errors, name


def _command_list_errors(stage: dict, prefix: str) -> list:
    """commands / verify_experiment 列表字段校验。"""
    errors = []
    commands = stage.get("commands", [])
    if not isinstance(commands, list) or not all(
            isinstance(item, str) for item in commands):
        errors.append(f"{prefix}.commands must be a string list")
    experiments = stage.get("verify_experiment", [])
    if not isinstance(experiments, list) or not all(
            isinstance(item, str) and bool(item.strip()) for item in experiments):
        errors.append(f"{prefix}.verify_experiment must be a non-empty string list")
    return errors


def _field_errors(stage: dict, prefix: str) -> list[str]:
    errors = [f"{prefix}.{key} must be a string" for key in _STRING_FIELDS
              if key in stage and not isinstance(stage[key], str)]
    errors.extend(f"{prefix}.{key} must be boolean" for key in _BOOL_FIELDS
                  if key in stage and not isinstance(stage[key], bool))
    if stage.get("mode", "serial") not in ("serial", "parallel"):
        errors.append(f"{prefix}.mode must be serial or parallel")
    for key, minimum in (("workers", 1), ("max_iterations", 1),
                         ("max_roles_per_iteration", 1), ("relevance_min_score", 0),
                         ("command_timeout", 1), ("command_output_max_bytes", 1),
                         ("meta_timeout", 0), ("meta_max_failed_roles", 0),
                         ("meta_role_retries", 0), ("gate_fix_rounds", 0)):
        value = stage.get(key, minimum)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            errors.append(f"{prefix}.{key} must be an integer >= {minimum}")
    errors.extend(_read_only_path_errors(stage, prefix))
    errors.extend(_meta_resume_bounds(stage, prefix))
    errors.extend(_meta_task_dag_bounds(stage, prefix))
    errors.extend(_role_model_errors(stage, prefix))
    errors.extend(_command_list_errors(stage, prefix))
    errors.extend(_meta_route_errors(stage, prefix))
    for key in ("validate", "validate_cmd", "gate_fix_validate"):
        value = stage.get(key)
        if value is not None and not isinstance(value, str):
            errors.append(f"{prefix}.{key} must be a string or null")
    return errors


def _meta_route_errors(stage: dict, prefix: str) -> list[str]:
    if stage.get("meta"):
        return []
    fields = ("orchestrator_model", "orchestrator_provider", "worker_model",
              "worker_provider", "role_models", "role_providers",
              "delegation_only", "meta_resume", "meta_task_dag")
    if stage.get("delegation_only"):
        return [f"{prefix}.delegation_only requires meta: true"]
    if stage.get("meta_resume"):
        return [f"{prefix}.meta_resume requires meta: true"]
    if stage.get("meta_task_dag"):
        return [f"{prefix}.meta_task_dag requires meta: true"]
    return ([f"{prefix} meta routing fields require meta: true"]
            if any(key in stage for key in fields) else [])


def _meta_resume_bounds(stage: dict, prefix: str) -> list[str]:
    if not stage.get("meta_resume"):
        return []
    errors = []
    if stage.get("max_iterations", 3) > 128:
        errors.append(f"{prefix}.max_iterations exceeds meta_resume limit 128")
    if stage.get("max_roles_per_iteration", 3) > 64:
        errors.append(f"{prefix}.max_roles_per_iteration exceeds meta_resume limit 64")
    return errors


def _meta_task_dag_bounds(stage: dict, prefix: str) -> list[str]:
    if stage.get("meta_task_dag") and stage.get("max_roles_per_iteration", 3) > 64:
        return [f"{prefix}.meta_task_dag supports at most 64 tasks per iteration"]
    return []


def _role_model_errors(stage: dict, prefix: str) -> list[str]:
    errors = []
    for field in ("role_models", "role_providers"):
        value = stage.get(field, {})
        if not isinstance(value, dict):
            errors.append(f"{prefix}.{field} must be a mapping of role names to strings")
            continue
        if any(not isinstance(role, str) or not role.strip()
               or not isinstance(target, str) or not target.strip()
               for role, target in value.items()):
            errors.append(
                f"{prefix}.{field} must map non-empty role names to non-empty strings")
    return errors


def _read_only_path_errors(stage: dict, prefix: str) -> list[str]:
    if "read_only_paths" not in stage:
        return []
    paths = stage["read_only_paths"]
    if not isinstance(paths, list) or not all(isinstance(item, str) and item.strip() for item in paths):
        return [f"{prefix}.read_only_paths must be a string list"]
    return []


def _source_errors(stage: dict, prefix: str, seen: set[str]) -> list[str]:
    from_outputs = stage.get("from_outputs", "")
    valid_outputs = (isinstance(from_outputs, str) and (not from_outputs or bool(from_outputs.strip()))) or (
        isinstance(from_outputs, list) and all(
            isinstance(item, str) and bool(item.strip()) for item in from_outputs))
    errors = [] if valid_outputs else [f"{prefix}.from_outputs must be a string or string list"]
    errors.extend(_source_contract(stage, prefix, from_outputs))
    if valid_outputs:
        errors.extend(_upstream_errors(from_outputs, prefix, seen))
    return errors


def _source_contract(stage: dict, prefix: str, from_outputs) -> list[str]:
    errors = []
    if stage.get("meta"):
        if not from_outputs:
            errors.append(f"{prefix} meta stage requires from_outputs")
        if not stage.get("output_dir"):
            errors.append(f"{prefix} meta stage requires output_dir")
        if stage.get("from_prompt") or stage.get("from_dir"):
            errors.append(f"{prefix} meta stage accepts only from_outputs")
    else:
        sources = sum(bool(stage.get(key)) for key in ("from_prompt", "from_dir", "from_outputs"))
        if sources != 1:
            errors.append(f"{prefix} requires exactly one input source")
        if stage.get("or_tasks") and not stage.get("tasks"):
            errors.append(f"{prefix} or_tasks stage requires a 'tasks' "
                          "candidate list")
        if stage.get("from_prompt") and not stage.get("output"):
            errors.append(f"{prefix} from_prompt requires output")
        if from_outputs and not stage.get("tasks"):
            errors.append(f"{prefix} from_outputs requires tasks")
    return errors


def _upstream_errors(from_outputs, prefix: str, seen: set[str]) -> list[str]:
    raw_names = from_outputs if isinstance(from_outputs, list) else [from_outputs]
    names = [name.strip() for name in raw_names if name.strip()]
    return [f"{prefix}.from_outputs references unavailable earlier stage '{name}'"
            for name in names if name not in seen]


def _task_errors(tasks, prefix: str) -> list[str]:
    if not isinstance(tasks, list):
        return [f"{prefix}.tasks must be a list"]
    errors = []
    for index, task in enumerate(tasks, 1):
        label = f"{prefix}.tasks[{index}]"
        errors.extend(_one_task_errors(task, label))
    return errors


def _one_task_errors(task, label: str) -> list[str]:
    if not isinstance(task, dict):
        return [f"{label} must be a mapping"]
    errors = _unknown_field_errors(task, _TASK_FIELDS, label)
    errors.extend(f"{label}.{key} must be a string" for key in _TASK_STRING_FIELDS
                  if key in task and not isinstance(task[key], str)
                  and not (key in _NULLABLE_TASK_STRINGS and task[key] is None))
    errors.extend(_no_tools_errors(task, label))
    if not any(isinstance(task.get(key), str) and task[key].strip()
               for key in ("prompt", "prompt_template")):
        errors.append(f"{label} requires a non-empty prompt or prompt_template")
    timeout = task.get("timeout", 1)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        errors.append(f"{label}.timeout must be an integer >= 1")
    if "env" in task and not _valid_env(task["env"]):
        errors.append(f"{label}.env must map strings to strings")
    for key in ("validate", "validate_cmd"):
        if key in task and task[key] is not None and not isinstance(task[key], str):
            errors.append(f"{label}.{key} must be a string or null")
    return errors


def _no_tools_errors(task: dict, label: str) -> list[str]:
    if "no_tools" in task and not isinstance(task["no_tools"], bool):
        return [f"{label}.no_tools must be boolean"]
    if task.get("no_tools") and task.get("tools"):
        return [f"{label}.no_tools cannot be combined with tools allowlist"]
    return []


def _valid_env(value) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items())


def _unknown_field_errors(value: dict, allowed: frozenset, prefix: str) -> list[str]:
    """Reject misspelled/unsupported declarative fields before execution."""
    return [f"{prefix} has unknown field '{key}'"
            for key in value if key not in allowed]
