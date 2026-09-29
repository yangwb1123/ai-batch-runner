from pbatch import config, meta, pipeline
from pbatch.meta_resume import MetaResumeStore, evidence_fingerprint
from pbatch.models import Stage, TaskResult
from pbatch.evidence import combine_outputs
from pbatch.reuse import write_sidecar
from pbatch.pipeline_schema import validate_pipeline


def test_meta_resume_state_keeps_only_named_roles_and_is_signature_bound(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_DATA_DIR_OVERRIDE", str(tmp_path / "runtime"))
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "reviewer.md").write_text("review template", encoding="utf-8")
    stage = _stage(tmp_path, roles)
    store = _store(stage, ["reviewer"])
    store.load(resume=False)
    store.planned(1, "a" * 64, [{"role": "reviewer", "task": ""}])
    store.started(1)

    restored = _store(stage, ["reviewer"])
    restored.load(resume=True)
    assert restored.iteration(1, "a" * 64)["roles"] == ["reviewer"]
    assert restored.iteration(1, "b" * 64) is None

    restored.planned(2, "c" * 64,
                     [{"role": "reviewer", "task": "sensitive task text"}])
    assert restored.iteration(2, "c" * 64) is None
    assert b"sensitive task text" not in restored.path.read_bytes()

    changed = _store(stage, ["reviewer"], model_override="different-model")
    changed.load(resume=True)
    assert changed.iteration(1, "a" * 64) is None


def test_meta_resume_uses_saved_named_plan_then_returns_to_planner(
        tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_DATA_DIR_OVERRIDE", str(tmp_path / "runtime"))
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "reviewer.md").write_text("Review {input_content}", encoding="utf-8")
    source = tmp_path / "source.md"
    source.write_text("initial evidence", encoding="utf-8")
    stage = _stage(tmp_path, roles)
    store = _store(stage, ["reviewer"])
    store.load(resume=False)
    evidence = combine_outputs([str(source)])
    store.planned(1, evidence_fingerprint(evidence),
                  [{"role": "reviewer", "task": ""}])
    planned, _ = meta._build_role_tasks(
        stage, [{"role": "reviewer", "task": ""}], evidence, "", 0,
        [str(source)])
    worker_task = planned[0][1]
    worker_task.output_path().parent.mkdir(parents=True, exist_ok=True)
    worker_task.output_path().write_text("review done", encoding="utf-8")
    write_sidecar(
        str(worker_task.output_path_unresolved()), worker_task.prompt, "",
        worker_task.model, worker_task.provider, worker_task.workdir(),
        worker_task.effective_agent(), worker_task.thinking, worker_task.tools,
        worker_task.exclude_tools, worker_task.no_tools)
    probe = _store(stage, ["reviewer"])
    probe.load(resume=True)
    assert probe.iteration(1, evidence_fingerprint(evidence)) is not None
    planner_calls, worker_calls = [], []

    def fake_run_task(task, **_kwargs):
        role = task.memory.get("role")
        if role == "orchestrator":
            planner_calls.append(task.prompt)
            return TaskResult(task, True, stdout="[]")
        worker_calls.append(task.memory.get("role"))
        return TaskResult(task, True, stdout="unexpected worker rerun")

    def save_output(task, _result, _validator, _cancel_event):
        task.output_path().write_text("review done", encoding="utf-8")
        return True

    monkeypatch.setattr(meta, "run_task", fake_run_task)
    monkeypatch.setattr(meta, "record_task", lambda _result: None)
    monkeypatch.setattr(meta, "_save_validated", save_output)
    monkeypatch.setattr(meta, "prepare_task_context", lambda *_args, **_kwargs: None)

    results, success = meta.run_meta_stage(
        stage, {"source": [str(source)]}, reuse=True)

    assert success is True
    assert len(planner_calls) == 1  # only iteration 2; iteration 1 resumed
    assert worker_calls == []  # completed worker is re-used by its fingerprint
    assert (tmp_path / "reviews" / "reviewer.md").exists()
    assert results and all(item.success for item in results)


def test_meta_resume_scopes_role_outputs_by_iteration(tmp_path):
    roles = tmp_path / "roles"
    roles.mkdir()
    (roles / "reviewer.md").write_text("review", encoding="utf-8")
    stage = _stage(tmp_path, roles)
    tasks, ok = meta._build_role_tasks(
        stage, [{"role": "reviewer", "task": ""}], "evidence", "", 0,
        iteration=2)
    assert ok is True
    assert tasks[0][1].output.endswith("reviewer-iter2.md")


def test_pipeline_schema_limits_durable_meta_resume_to_meta_stages():
    spec = {"stages": [{
        "name": "source", "from_prompt": "input", "output": "source.md",
    }, {
        "name": "review", "from_outputs": "source", "meta": True,
        "output_dir": "reviews", "meta_resume": True,
        "max_iterations": 129,
    }]}
    errors = validate_pipeline(spec)
    assert any("max_iterations exceeds meta_resume limit" in item
               for item in errors)
    valid_stage = {**spec["stages"][1], "max_iterations": 3}
    assert pipeline._parse_stage_def(valid_stage, False).meta_resume is True

    invalid = {"stages": [{
        "name": "source", "from_prompt": "input", "output": "source.md",
        "meta_resume": True,
    }]}
    assert any("meta_resume requires meta" in item
               for item in validate_pipeline(invalid))


def _stage(root, roles):
    return Stage(
        name="review", meta=True, meta_resume=True,
        from_outputs="source", role_dir=str(roles),
        output_dir=str(root / "reviews"), cwd=str(root),
        max_iterations=2, max_roles_per_iteration=2,
        meta_role_retries=0, workers=1)


def _store(stage, roles, model_override=""):
    return MetaResumeStore(
        stage, model_override=model_override, validate_cmd="",
        original_prompt="", session_name="", role_names=roles,
        fingerprint_mode=stage.meta_resume)
