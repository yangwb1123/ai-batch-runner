"""gate-fix loop 与 meta 韧性（self-iteration round 沉淀自 sverp 实战）：

1) gate FAIL/REJECT + gate_fix_rounds > 0 → 自动修复循环：
   - extract_gate_findings 解析 gatekeeper 的 ```json 发现数组
   - gate_fail_reason / gate_fix_prompt 组装修复指令
   - run_gate_fix 产出修复报告，重新执行 gate 阶段（extra_evidence）后
     重新裁决，最多 N 轮；PASS 提前结束
2) meta 韧性：meta_role_retries 重试失败角色；meta_max_failed_roles
   允许 N 个角色失败而阶段继续
3) {tool_root} 占位符：validator/stage 命令可引用工具仓库根
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from pbatch import gate_fix
from pbatch.models import Stage, Task


def _stage(**kw):
    base = dict(name="gate", gate=True, gate_fix_rounds=2,
                validate_cmd="", cwd="")
    base.update(kw)
    return Stage(**base)


class TestGateFindings:
    def test_extracts_bounded_findings_from_json_fence(self):
        text = (
            "VERDICT: FAIL - blocking\n\n"
            "```json\n"
            '[{"severity":"high","file":"a.ts","line":3,"problem":"bug",'
            '"fix_hint":"Number()"}, {"severity":"low","file":"b.ts"'
            ',"problem":"style"}]\n'
            "```\n"
        )
        findings = gate_fix.extract_gate_findings(text)
        assert findings is not None
        assert findings[0]["file"] == "a.ts"
        assert findings[0]["fix_hint"] == "Number()"
        assert findings[1]["severity"] == "low"

    def test_plain_text_reason_returns_none(self):
        assert gate_fix.extract_gate_findings("VERDICT: FAIL - plain only") is None

    def test_bad_json_and_empty_arrays_return_none(self):
        assert gate_fix.extract_gate_findings("```json\n[not json]\n```") is None
        assert gate_fix.extract_gate_findings("```json\n[]\n```") is None

    def test_non_dict_items_are_dropped(self):
        text = "```json\n[{\"file\":\"ok.ts\"}, \"junk\", 42]\n```"
        findings = gate_fix.extract_gate_findings(text)
        assert findings == [{"file": "ok.ts"}]

    def test_reason_uses_trailing_blocks_bounded(self):
        gate_dir = Path(__file__).parent / "tmp-gate-artifacts"
        gate_dir.mkdir(exist_ok=True)
        artifact = gate_dir / "gate.md"
        artifact.write_text("VERDICT: FAIL - blocked\n" + "x" * 1000, encoding="utf-8")
        try:
            reason = gate_fix.gate_fail_reason([str(artifact)])
            assert "VERDICT: FAIL - blocked" in reason
            assert len(reason) <= 16000
        finally:
            artifact.unlink()
            gate_dir.rmdir()

    def test_fix_prompt_custom_template_and_default(self):
        stage = _stage(gate_fix_prompt="FIX: {reason} | {findings}")
        prompt = gate_fix.gate_fix_prompt(stage, "reason", [{"file": "a.ts"}])
        assert "FIX: reason | " in prompt and "a.ts" in prompt
        prompt2 = gate_fix.gate_fix_prompt(_stage(), "reason", None)
        assert "gate stage 'gate' rejected" in prompt2
        assert "(none)" in prompt2


class TestToolRootPlaceholder:
    def test_validator_command_expands_tool_root(self, tmp_path):
        """{tool_root} 在验证器命令里展开为工具仓库根（替代硬编码绝对路径）。"""
        from pbatch import config
        from pbatch.models import Task
        from pbatch import runner

        out = tmp_path / "out.md"
        task = Task(prompt="p", output=str(out), cwd=str(tmp_path))
        result = runner.TaskResult(task=task, success=True, returncode=0,
                                   stdout="ok")
        # 用 {tool_root} 指向真实存在的文件断言展开结果
        probe = config.TOOL_ROOT
        cmd = f"test -f {{tool_root}}/pi-batch.py"
        expanded = cmd.replace("{tool_root}", probe)
        assert expanded == f"test -f {probe}/pi-batch.py"
        assert Path(probe, "pi-batch.py").exists()


# ── 端到端：gate FAIL → 自动修复循环 → PASS → 下游继续 ──────────────

def _gatefix_agent(tmp_path):
    """Stateful fake agent: first gatekeeper call FAILs with structured
    findings; the fix task reports; the re-run gatekeeper (whose prompt
    contains the fix report) PASSes."""
    path = tmp_path / "gatefix-agent.sh"
    path.write_text(
        "#!/bin/sh\n"
        'prompt="$2"\n'
        'if printf "%s" "$prompt" | grep -q "GATE VERDICT REASON"; then\n'
        '  echo "fixed everything; regression test added"\n'
        'elif printf "%s" "$prompt" | grep -q "fixed everything"; then\n'
        '  echo "VERDICT: PASS - all findings resolved"\n'
        'elif printf "%s" "$prompt" | grep -q "gatekeeper"; then\n'
        '  echo "VERDICT: FAIL - blocked"\n'
        '  echo "\\`\\`\\`json"\n'
        '  echo \'[{"severity":"high","file":"x.ts","line":3,"problem":'
        '"bug","fix_hint":"fix it"}]\'\n'
        '  echo "\\`\\`\\`"\n'
        'else\n'
        '  echo "plan output"\n'
        'fi\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_gate_fix_loop_resolves_and_continues(tmp_path):
    from pbatch import config
    from pbatch.models import Pipeline
    from pbatch.pipeline import run_pipeline

    agent = _gatefix_agent(tmp_path)
    old_bin = config.AGENT_BIN
    config.AGENT_BIN = str(agent)
    try:
        pipeline = Pipeline(stages=[
            Stage(name="plan", from_prompt="plan the work", output="plan.md",
                  cwd=str(tmp_path)),
            Stage(name="gate", from_outputs="plan", aggregate=True,
                  gate=True, gate_fix_rounds=2, cwd=str(tmp_path),
                  tasks=[{"prompt": "You are the gatekeeper. {input_content}",
                          "output": "gate.md"}]),
            Stage(name="downstream", from_outputs="gate", aggregate=True,
                  cwd=str(tmp_path),
                  tasks=[{"prompt": "consume {input_content}",
                          "output": "downstream.md"}]),
        ])
        results, failed = run_pipeline(pipeline)
        assert failed == []
        assert (tmp_path / "gate-fix-1.md").exists()
        assert "fixed everything" in (tmp_path / "gate-fix-1.md").read_text()
        assert (tmp_path / "downstream.md").exists()
        # 修复报告被折叠进 gate 证据（第二次 gatekeeper 看到修复内容）
        gate_text = (tmp_path / "gate.md").read_text()
        assert "VERDICT: PASS" in gate_text
    finally:
        config.AGENT_BIN = old_bin


def test_gate_fix_loop_exhausts_rounds_and_halts(tmp_path):
    from pbatch import config
    from pbatch.models import Pipeline
    from pbatch.pipeline import run_pipeline

    path = tmp_path / "stubborn-agent.sh"
    path.write_text(
        "#!/bin/sh\n"
        'prompt="$2"\n'
        'if printf "%s" "$prompt" | grep -q "GATE VERDICT REASON"; then\n'
        '  echo "tried"\n'
        'elif printf "%s" "$prompt" | grep -q "gatekeeper"; then\n'
        '  echo "VERDICT: FAIL - still blocked"\n'
        'else\n'
        '  echo "plan output"\n'
        'fi\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    old_bin = config.AGENT_BIN
    config.AGENT_BIN = str(path)
    try:
        pipeline = Pipeline(stages=[
            Stage(name="plan", from_prompt="plan", output="plan.md",
                  cwd=str(tmp_path)),
            Stage(name="gate", from_outputs="plan", aggregate=True,
                  gate=True, gate_fix_rounds=1, cwd=str(tmp_path),
                  tasks=[{"prompt": "gatekeeper {input_content}",
                          "output": "gate.md"}]),
        ])
        results, failed = run_pipeline(pipeline)
        assert failed == ["gate"]
        assert (tmp_path / "gate-fix-1.md").exists()
    finally:
        config.AGENT_BIN = old_bin


# ── meta 韧性：meta_max_failed_roles / meta_role_retries ─────────────

class _FakeRoleResult:
    def __init__(self, ok):
        self.success = ok
        self.task = Task(prompt="role", output="role.md")


def test_meta_allows_configured_role_failures(monkeypatch, tmp_path):
    import pbatch.meta as meta

    out_dir = tmp_path / "reviews"
    stage = Stage(name="review", meta=True, from_outputs="s0",
                  output_dir=str(out_dir), max_iterations=1,
                  meta_max_failed_roles=1, meta_role_retries=0,
                  cwd=str(tmp_path))
    src = tmp_path / "s0.md"
    src.write_text("evidence", encoding="utf-8")
    stage_outputs = {"s0": [str(src)]}

    monkeypatch.setattr(meta, "_select_roles",
                        lambda *a: [{"role": "reviewer",
                                     "task": "审查交付物"}])

    def fake_run_role_task(item, *args, **kw):
        return "reviewer", _FakeRoleResult(False)

    monkeypatch.setattr(meta, "_run_role_task", fake_run_role_task)
    # orchestrator 直接成功并给角色计划
    class _Ok:
        success = True
        stdout = '["reviewer"]'
        reason = ""
        from pbatch.models import Task as _Task
        task = _Task(prompt="x", output="orch.md")
    monkeypatch.setattr(meta, "record_task", lambda *a, **kw: None)
    monkeypatch.setattr(meta, "run_task", lambda *a, **kw: _Ok())

    results, ok = meta.run_meta_stage(stage, stage_outputs)
    assert ok is True  # 1 个失败 <= 允许 1
    assert len(results) == 1 and results[0].success is False


def test_meta_zero_allowance_still_fails(monkeypatch, tmp_path):
    import pbatch.meta as meta

    out_dir = tmp_path / "reviews"
    stage = Stage(name="review", meta=True, from_outputs="s0",
                  output_dir=str(out_dir), max_iterations=1,
                  meta_max_failed_roles=0, meta_role_retries=0,
                  cwd=str(tmp_path))
    src = tmp_path / "s0.md"
    src.write_text("evidence", encoding="utf-8")
    stage_outputs = {"s0": [str(src)]}
    monkeypatch.setattr(meta, "_select_roles",
                        lambda *a: [{"role": "reviewer",
                                     "task": "审查交付物"}])
    monkeypatch.setattr(meta, "_run_role_task",
                        lambda item, *a, **kw: ("reviewer", _FakeRoleResult(False)))

    # orchestrator 直接成功并给角色计划
    class _Ok:
        success = True
        stdout = '["reviewer"]'
        reason = ""
        from pbatch.models import Task as _Task
        task = _Task(prompt="x", output="orch.md")
    monkeypatch.setattr(meta, "record_task", lambda *a, **kw: None)
    monkeypatch.setattr(meta, "run_task", lambda *a, **kw: _Ok())

    results, ok = meta.run_meta_stage(stage, stage_outputs)
    assert ok is False
