"""Separate high-capability meta planning from routed role workers."""

from __future__ import annotations

from pbatch import config, meta, pipeline
from pbatch.models import Stage
from pbatch.pipeline_schema import validate_pipeline


def test_meta_planner_and_role_workers_route_independently(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_AGENT_CFG", {})
    monkeypatch.setattr(config, "_AGENTS_CFG", {
        "pi": {"no_tools_args": ["--no-tools"]},
    })
    stage = Stage(
        name="review", meta=True, delegation_only=True,
        model="shared-fallback", provider="shared-provider",
        orchestrator_model="planner-flagship",
        orchestrator_provider="planner-provider",
        worker_model="worker-cheap", worker_provider="worker-provider",
        role_models={"security_engineer": "security-model"},
        role_providers={"security_engineer": "security-provider"},
        output_dir=str(tmp_path), cwd=str(tmp_path))

    planner = meta._build_meta_prompt_task(
        stage, ["security_engineer"], "evidence", "", 0)
    workers, ok = meta._build_role_tasks(
        stage,
        [{"role": "security_engineer", "task": "inspect security"},
         {"role": "qa_lead", "task": "check tests"}],
        "evidence", "", 0)

    assert planner.model == "planner-flagship"
    assert planner.memory["model_route_source"] == "orchestrator_model"
    assert planner.provider == "planner-provider"
    assert planner.no_tools is True
    assert "DELEGATION-ONLY CONTROLLER" in planner.prompt
    assert "--no-tools" in planner.to_cmd()
    assert ok is True
    assert [(task.model, task.provider) for _, task in workers] == [
        ("security-model", "security-provider"),
        ("worker-cheap", "worker-provider"),
    ]
    assert [task.memory["model_route_source"] for _, task in workers] == [
        "role_model", "worker_model"]


def test_cli_model_override_remains_authoritative_for_planner_and_workers(
        tmp_path):
    stage = Stage(
        name="review", meta=True, delegation_only=True,
        orchestrator_model="planner", worker_model="cheap",
        output_dir=str(tmp_path), cwd=str(tmp_path))
    planner = meta._build_meta_prompt_task(
        stage, [], "evidence", "cli-model", 0)
    workers, _ = meta._build_role_tasks(
        stage, [{"role": "reviewer", "task": "review"}],
        "evidence", "cli-model", 0)

    assert planner.model == "cli-model"
    assert planner.memory["model_route_source"] == "cli_override"
    assert workers[0][1].model == "cli-model"
    assert workers[0][1].memory["model_route_source"] == "cli_override"


def test_pipeline_schema_accepts_meta_model_routing_and_rejects_bad_policy():
    valid = {"stages": [
        {"name": "source", "from_prompt": "task", "output": "source.md"},
        {"name": "review", "from_outputs": "source", "meta": True,
         "output_dir": "reviews", "delegation_only": True,
         "orchestrator_model": "flagship", "worker_model": "cheap",
         "role_models": {"security_engineer": "security-model"},
         "role_providers": {"security_engineer": "provider-a"}},
    ]}
    assert validate_pipeline(valid) == []
    parsed = pipeline._parse_stage_def(valid["stages"][1], False)
    assert parsed.orchestrator_model == "flagship"
    assert parsed.worker_model == "cheap"
    assert parsed.role_models == {"security_engineer": "security-model"}
    assert parsed.delegation_only is True

    invalid = {"stages": [
        {"name": "review", "from_prompt": "task", "output": "out.md",
         "delegation_only": True, "role_models": ["cheap"]},
    ]}
    errors = validate_pipeline(invalid)
    assert any("delegation_only requires meta" in error for error in errors)
    assert any("role_models must be a mapping" in error for error in errors)


def test_pipeline_task_schema_rejects_conflicting_tool_policy():
    spec = {"stages": [
        {"name": "source", "from_prompt": "task", "output": "source.md"},
        {"name": "worker", "from_outputs": "source", "tasks": [
            {"prompt": "inspect", "output": "out.md", "no_tools": True,
             "tools": "read"}],
        },
    ]}
    assert any("no_tools cannot be combined" in error
               for error in validate_pipeline(spec))
