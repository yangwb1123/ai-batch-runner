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

import json
import subprocess
import sys
from pathlib import Path

import pytest

from pbatch import gate_fix
from pbatch.models import Stage, Task

REPO_ROOT = Path(__file__).resolve().parent.parent


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

    def test_fix_context_uses_only_rejecting_artifacts(self, tmp_path):
        passed = tmp_path / "pass.md"
        failed = tmp_path / "fail.md"
        pass_contract = {
            "schema_version": 1, "verdict": "PASS", "reason": "advisory",
            "evidence": ["ok"],
            "validators": [{"name": "tests", "status": "PASS"}],
            "findings": [{"severity": "low", "file": "advisory.py",
                          "problem": "non-blocking"}],
        }
        fail_contract = {
            "schema_version": 1, "verdict": "FAIL", "reason": "blocked",
            "findings": [{"severity": "high", "file": "blocking.py",
                          "problem": "must fix"}],
        }
        passed.write_text(
            f"```json\n{json.dumps(pass_contract)}\n```\n" + "p" * 20000,
            encoding="utf-8")
        failed.write_text(
            f"```json\n{json.dumps(fail_contract)}\n```\n",
            encoding="utf-8")

        findings = gate_fix._first_findings([str(passed), str(failed)])
        reason = gate_fix.gate_fail_reason([str(failed), str(passed)])

        assert findings and [item["file"] for item in findings] == [
            "blocking.py"]
        assert "blocked" in reason
        assert "advisory" not in reason

    def test_reason_reports_missing_invalid_and_unreadable_artifacts(
            self, tmp_path, monkeypatch):
        passed = tmp_path / "pass.md"
        invalid = tmp_path / "invalid.md"
        missing = tmp_path / "missing.md"
        oversized = tmp_path / "oversized.md"
        passed.write_text("VERDICT: PASS - checked", encoding="utf-8")
        invalid.write_text("review notes only", encoding="utf-8")
        oversized.write_text("x" * 64, encoding="utf-8")

        reason = gate_fix.gate_fail_reason(
            [str(passed), str(invalid), str(missing)])
        assert "review notes only" not in reason
        assert f"{invalid}: invalid Gate artifact" in reason
        assert "no usable Gate verdict" in reason
        assert f"{missing}: Gate artifact is missing" in reason
        assert str(passed) not in reason

        monkeypatch.setattr(gate_fix.config, "OUTPUT_MAX_BYTES", 8)
        unreadable = gate_fix.gate_fail_reason([str(oversized)])
        assert f"{oversized}: invalid Gate artifact" in unreadable
        assert "exceeds" in unreadable

    def test_findings_do_not_follow_symlink_artifacts(self, tmp_path):
        target = tmp_path / "outside.md"
        artifact = tmp_path / "gate.md"
        target.write_text(
            "VERDICT: FAIL - linked\n"
            "```json\n"
            '[{"severity":"high","file":"outside.py",'
            '"problem":"must not be injected"}]\n'
            "```\n",
            encoding="utf-8",
        )
        artifact.symlink_to(target)

        assert gate_fix._first_findings([str(artifact)]) is None


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


def test_stage_agent_jsonl_gate_fix_resolves_and_continues(tmp_path):
    """A Codex-shaped transcript must drive both FAIL remediation and the
    later PASS; the gate-fix task also keeps the gate stage's executor."""
    from pbatch.models import Pipeline
    from pbatch.pipeline import run_pipeline

    agent = tmp_path / "jsonl-agent.py"
    agent.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import sys\n"
        "prompt = sys.argv[2]\n"
        "if 'GATE VERDICT REASON' in prompt:\n"
        "    for _ in range(80):\n"
        "        print(json.dumps({'type': 'item.completed', 'item': {\n"
        "            'type': 'command_execution',\n"
        "            'aggregated_output': 'x' * 1024}}))\n"
        "    message = 'fixed everything; regression test added'\n"
        "elif 'fixed everything' in prompt:\n"
        "    message = 'VERDICT: PASS - verified after remediation'\n"
        "elif 'gatekeeper' in prompt:\n"
        "    message = 'VERDICT: FAIL - implementation evidence is missing'\n"
        "else:\n"
        "    message = 'ordinary stage output'\n"
        "print(json.dumps({'type': 'item.completed', 'item': {\n"
        "    'type': 'agent_message', 'text': message}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {}}))\n",
        encoding="utf-8",
    )
    agent.chmod(0o755)
    pipeline = Pipeline(stages=[
        Stage(name="plan", agent=str(agent), from_prompt="plan",
              output=str(tmp_path / "plan.md"), cwd=str(tmp_path)),
        Stage(name="gate", agent=str(agent), from_outputs="plan",
              aggregate=True, gate=True, gate_fix_rounds=1,
              cwd=str(tmp_path), tasks=[{
                  "prompt": "gatekeeper {input_content}",
                  "output": str(tmp_path / "gate.md"),
              }]),
        Stage(name="downstream", agent=str(agent), from_outputs="gate",
              aggregate=True, cwd=str(tmp_path), tasks=[{
                  "prompt": "consume {input_content}",
                  "output": str(tmp_path / "downstream.md"),
              }]),
    ])

    _results, failed = run_pipeline(pipeline)

    assert failed == []
    fix_report = (tmp_path / "gate-fix-1.md").read_text()
    assert "fixed everything" in fix_report
    assert "command_execution" not in fix_report
    assert len(fix_report.encode("utf-8")) < 1024
    assert "VERDICT: PASS" in (tmp_path / "gate.md").read_text()
    assert (tmp_path / "downstream.md").exists()


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


# ── ratchet 门禁（基线 + 仅查新增）──

def test_ratchet_baseline_holds_and_detects_new(tmp_path):
    """--update-baseline 建立基线 → 存量放行 → 新增违规拒绝（退出码 1）。"""
    import subprocess, sys
    (tmp_path / "a.css").write_text(".a { padding: 7px; }", encoding="utf-8")
    base = tmp_path / "base.json"
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/check-ui-spec.py"),
         "--dir", str(tmp_path), "--all", "--ext", "css",
         "--update-baseline", str(base)],
        capture_output=True, text=True)
    assert r.returncode == 0
    # 存量放行
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/check-ui-spec.py"),
         "--dir", str(tmp_path), "--all", "--ext", "css",
         "--baseline", str(base)],
        capture_output=True, text=True)
    assert r.returncode == 0
    assert "ratchet held" in r.stdout
    # 新增违规 → 拒绝
    (tmp_path / "b.css").write_text(".b { padding: 9px; }", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts/check-ui-spec.py"),
         "--dir", str(tmp_path), "--all", "--ext", "css",
         "--baseline", str(base)],
        capture_output=True, text=True)
    assert r.returncode == 1
    assert "1 new violation" in r.stderr
    assert "b.css" in r.stderr


def test_gate_fix_uses_stage_agent_for_remediation_task(tmp_path, monkeypatch):
    """T13/T14: the remediation task inherits stage.agent (not only the
    global binary) so a gate on a codex stage is fixed by codex."""
    from pbatch import runner
    from pbatch.gate_fix import run_gate_fix
    from pbatch.models import Stage, TaskResult

    stage = Stage(name="gate", agent="/tmp/remediator", from_outputs="x",
                  gate=True, cwd=str(tmp_path))
    gate_out = tmp_path / "gate.md"
    gate_out.write_text(
        '{"type":"item.completed","item":{"id":"i0","type":"agent_message",'
        '"text":"VERDICT: FAIL - checklist only"}}\n'
        '{"type":"turn.completed","usage":{}}\n', encoding="utf-8")
    invoked = []

    def fake_run(task, **kwargs):
        invoked.append(task)
        return TaskResult(task, True, stdout="ok", elapsed=0.1)

    monkeypatch.setattr(runner, "run_task", fake_run)
    monkeypatch.setattr(runner, "_save_validated",
                        lambda task, result, cmd: True)
    # gate_fix calls memory.record_task from within its module
    import pbatch.memory as memory_mod
    monkeypatch.setattr(memory_mod, "record_task", lambda result: None)

    result, _report = run_gate_fix(stage, [str(gate_out)], 1, "", "s")
    assert result.success
    assert invoked and invoked[0].agent == "/tmp/remediator"
    # JSONL verdict text flows into the fix prompt
    assert "VERDICT: FAIL - checklist only" in invoked[0].prompt
    assert "turn.completed" not in invoked[0].prompt


def test_gate_fix_inherits_campaign_model_and_timeout(tmp_path, monkeypatch):
    """The remediation task must run the campaign model with the campaign
    timeout, not the bare default (which made acceptance fixes time out)."""
    from pbatch import runner
    from pbatch.gate_fix import run_gate_fix
    from pbatch.models import Stage, TaskResult

    stage = Stage(name="acceptance", agent="pi", from_outputs="x",
                  gate=True, cwd=str(tmp_path))
    gate_out = tmp_path / "gate.md"
    gate_out.write_text("VERDICT: FAIL - missing evidence\n", encoding="utf-8")
    invoked = []

    def fake_run(task, **kwargs):
        invoked.append(task)
        return TaskResult(task, True, stdout="ok", elapsed=0.1)

    monkeypatch.setattr(runner, "run_task", fake_run)
    monkeypatch.setattr(runner, "_save_validated",
                        lambda task, result, cmd: True)
    import pbatch.memory as memory_mod
    monkeypatch.setattr(memory_mod, "record_task", lambda result: None)

    run_gate_fix(stage, [str(gate_out)], 1, "", "s",
                 model_override="openai-codex/gpt-6.1-sol", timeout_override=5400)
    assert invoked and invoked[0].model == "openai-codex/gpt-6.1-sol"
    assert invoked[0].timeout == 5400

    invoked.clear()
    run_gate_fix(stage, [str(gate_out)], 1, "", "s")
    assert invoked and invoked[0].timeout == 1800
