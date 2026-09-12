"""Gate reuse, prompt propagation, and rejection lifecycle tests."""

import importlib.util
import sys
from pathlib import Path

import pytest

PI_BATCH = Path(__file__).resolve().parent.parent / "pi-batch.py"


def load_batch():
    spec = importlib.util.spec_from_file_location(
        "pi_batch_gate_lifecycle_under_test", PI_BATCH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def fake_agent(tmp_path):
    path = tmp_path / "fake-agent.sh"
    path.write_text("#!/bin/sh\necho \"$2\"\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# ── aero-id 维护周期 R5e 复盘的三个修复 ──────────────────────────────
# 1) gate 的 FAIL 裁决永不复用（裁决绑定仓库状态，fingerprint 看不见代码变化）
# 2) from_prompt / 任务模板支持 {prompt} 注入原始任务提示（-p --classify 路由主路径）
# 3) 验证器拒绝时保留有界摘录到用户级 runtime/rejected/（诊断可见性，输出路径零残留）


@pytest.fixture()
def counter_agent(tmp_path):
    """Fake agent that appends its prompt to a log and replies with a
    generic line, so tests can assert whether the agent was invoked."""
    calls = tmp_path / "agent-calls.txt"
    path = tmp_path / "counter-agent.sh"
    path.write_text('#!/bin/sh\necho "$2" >> %s\necho "reply"\n' % calls)
    path.chmod(0o755)
    return path, calls


def test_gate_fail_verdict_never_reused(tmp_path, counter_agent):
    """A gate deliverable carrying VERDICT: FAIL must never be reused by
    --reuse: the verdict binds repository state that evidence fingerprints
    cannot see (a code fix after the FAIL does not change the evidence
    docs — aero-id round R5e finding)."""
    mod = load_batch()
    agent, calls = counter_agent
    mod.config.AGENT_BIN = str(agent)
    gate_out = tmp_path / "gate.md"
    gate_out.write_text("VERDICT: FAIL - blocking issue remains\n", encoding="utf-8")
    upstream = tmp_path / "upstream.out.md"
    upstream.write_text("design\n", encoding="utf-8")
    stage = mod.Stage(
        name="gate",
        from_outputs="design",
        aggregate=True,
        gate=True,
        tasks=[{"prompt": "verify:\n{input_content}", "output": str(gate_out)}],
    )
    results, ok = mod.execute_stage(stage, {"design": [str(upstream)]}, reuse=True)
    assert ok is True
    assert len(results) == 1            # regenerated, not reused
    assert calls.read_text().strip()    # the agent really ran
    # the stale FAIL artifact was deleted before the rerun, and the fresh
    # deliverable replaced it
    assert gate_out.exists()
    assert "VERDICT: FAIL" not in gate_out.read_text()


@pytest.mark.parametrize("stale_verdict", [
    "VERDICT: PASS\n",
    "VERDICT: FAIL\n",
    "gatekeeper forgot the verdict\n",
])
def test_gate_reuse_blocker_deletes_bare_or_missing_verdict(
        tmp_path, stale_verdict):
    from pbatch.pipeline import _gate_reuse_blocked

    gate_out = tmp_path / "gate.md"
    gate_out.write_text(stale_verdict, encoding="utf-8")

    assert _gate_reuse_blocked(str(gate_out)) is True
    assert not gate_out.exists()


@pytest.mark.parametrize("stale_verdict", [
    "VERDICT: PASS\n",
    "VERDICT: FAIL\n",
    "gatekeeper forgot the verdict\n",
])
def test_gate_bare_or_missing_verdict_never_reused(
        tmp_path, counter_agent, stale_verdict):
    """Only a reasoned PASS is reusable; malformed gate artifacts are
    deleted and regenerated instead of being preserved until _handle_gate
    fails later."""
    mod = load_batch()
    agent, calls = counter_agent
    mod.config.AGENT_BIN = str(agent)
    gate_out = tmp_path / "gate.md"
    gate_out.write_text(stale_verdict, encoding="utf-8")
    upstream = tmp_path / "upstream.out.md"
    upstream.write_text("design\n", encoding="utf-8")
    stage = mod.Stage(
        name="gate", from_outputs="design", aggregate=True, gate=True,
        tasks=[{"prompt": "verify:\n{input_content}", "output": str(gate_out)}],
    )

    results, ok = mod.execute_stage(
        stage, {"design": [str(upstream)]}, reuse=True)

    assert ok is True
    assert len(results) == 1
    assert calls.read_text(encoding="utf-8").strip()
    assert gate_out.read_text(encoding="utf-8") == "reply\n"


def test_gate_pass_verdict_reused(tmp_path, fake_agent):
    """A PASS gate deliverable is still reusable when its evidence is
    unchanged (validated reuse semantics preserved)."""
    mod = load_batch()
    mod.config.AGENT_BIN = str(fake_agent)
    gate_out = tmp_path / "gate.md"
    gate_out.write_text("VERDICT: PASS - all checks satisfied\n", encoding="utf-8")
    upstream = tmp_path / "upstream.out.md"
    upstream.write_text("design\n", encoding="utf-8")
    stage = mod.Stage(
        name="gate",
        from_outputs="design",
        aggregate=True,
        gate=True,
        tasks=[{"prompt": "verify:\n{input_content}", "output": str(gate_out)}],
    )
    results, ok = mod.execute_stage(stage, {"design": [str(upstream)]}, reuse=True)
    assert ok is True
    assert len(results) == 0            # reused
    assert gate_out.read_text() == "VERDICT: PASS - all checks satisfied\n"


def test_from_prompt_injects_original_prompt(tmp_path, fake_agent):
    """{prompt} in a from_prompt stage is replaced with the original task
    prompt that launched the pipeline (the -p --classify routing path)."""
    mod = load_batch()
    mod.config.AGENT_BIN = str(fake_agent)
    out = tmp_path / "plan.md"
    stage = mod.Stage(
        name="plan",
        from_prompt="Original task: {prompt}\nProduce a plan.",
        output=str(out),
    )
    results, ok = mod.execute_stage(stage, {}, original_prompt="implement the widget")
    assert ok is True
    text = out.read_text()               # fake agent echoes the prompt
    assert "implement the widget" in text
    assert "Original task:" in text


def test_from_prompt_without_original_strips_placeholder(tmp_path, fake_agent):
    """Without an original prompt the {prompt} placeholder is replaced with
    an empty string so templates do not leak raw placeholders to the agent."""
    mod = load_batch()
    mod.config.AGENT_BIN = str(fake_agent)
    out = tmp_path / "plan.md"
    stage = mod.Stage(
        name="plan",
        from_prompt="Original task: {prompt}\nProduce a plan.",
        output=str(out),
    )
    results, ok = mod.execute_stage(stage, {})
    assert ok is True
    assert "{prompt}" not in out.read_text()


def test_task_template_injects_original_prompt(tmp_path, fake_agent):
    """{prompt} in an aggregate from_outputs task template is replaced with
    the original task prompt."""
    mod = load_batch()
    mod.config.AGENT_BIN = str(fake_agent)
    upstream = tmp_path / "upstream.out.md"
    upstream.write_text("evidence\n", encoding="utf-8")
    out = tmp_path / "combined.md"
    stage = mod.Stage(
        name="s1",
        from_outputs="s0",
        aggregate=True,
        tasks=[{"prompt": "Original: {prompt}\n{input_content}", "output": str(out)}],
    )
    results, ok = mod.execute_stage(stage, {"s0": [str(upstream)]},
                                    original_prompt="build auth")
    assert ok is True
    assert "build auth" in out.read_text()


def test_rejected_excerpt_saved_for_diagnosis(tmp_path, fake_agent):
    """A validator-rejected artifact keeps a bounded excerpt under
    the private runtime/rejected/ directory (diagnostics only) while the output path stays
    clean — the aero-id completion-gate diagnosis finding."""
    mod = load_batch()
    mod.config.AGENT_BIN = str(fake_agent)
    output = tmp_path / "report.md"
    results = mod.run_serial([mod.Task(prompt="x", output=str(output), cwd=str(tmp_path))],
                             validate_cmd="exit 7")
    assert results[0].success is False
    assert not output.exists()          # zero residue at the deliverable path
    from pbatch.user_dirs import project_runtime_dir
    rej = project_runtime_dir(str(tmp_path)) / "rejected"
    assert rej.is_dir()
    files = list(rej.glob("*.rejected.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "validator" in text and "reason" in text
    assert "x" in text                  # the rejected content excerpt


def test_meta_prompt_injects_original_prompt(tmp_path, counter_agent):
    """{prompt} in a meta stage's custom orchestrator prompt is replaced
    with the original task prompt (the orchestrator prompt actually sent
    to the agent carries it)."""
    mod = load_batch()
    agent, calls = counter_agent
    mod.config.AGENT_BIN = str(agent)
    upstream = tmp_path / "upstream.out.md"
    upstream.write_text("evidence\n", encoding="utf-8")
    out_dir = tmp_path / "reviews"
    out_dir.mkdir()
    stage = mod.Stage(
        name="review",
        from_outputs="s0",
        meta=True,
        meta_prompt="Task: {prompt}\nChoose roles from {roles}.\n{input_content}",
        output_dir=str(out_dir),
        max_iterations=1,
    )
    results, ok = mod.execute_stage(stage, {"s0": [str(upstream)]},
                                    original_prompt="harden auth")
    assert ok is True
    sent = calls.read_text(encoding="utf-8")
    assert "harden auth" in sent          # {prompt} substituted in the sent prompt
    assert "{prompt}" not in sent         # never leaked raw
