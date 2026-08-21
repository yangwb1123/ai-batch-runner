"""Tests for Agent 工程组织规范（agent-engineering/ + pi-batch org）.

17 个流程节点岗位说明书（9 要素）、org check 完整性校验（fail closed）、
org flow 顺序、auto_checks 命令存在性校验。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pbatch import org


@pytest.mark.contract
def test_all_nodes_have_nine_elements():
    nodes = org.load_nodes()
    assert len(nodes) >= 16
    for node in nodes:
        for key in org.REQUIRED_KEYS:
            assert key in node, f"{node.get('id')} 缺 {key}"


def test_stages_consecutive_0_to_16():
    nodes = org.load_nodes()
    stages = sorted(node["stage"] for node in nodes)
    assert stages == list(range(len(nodes)))
    assert stages[0] == 0 and stages[-1] == 16


def test_node_ids_unique():
    nodes = org.load_nodes()
    ids = [n["id"] for n in nodes]
    assert len(ids) == len(set(ids))


def test_auto_checks_reference_real_commands():
    """自动检测列全部为仓库真实命令（规范可执行）。"""
    report = org.org_check()
    assert report["valid"] is True, report["problems"]


def test_org_check_detects_missing_element(tmp_path, monkeypatch):
    (tmp_path / "node.yaml").write_text("id: x\nname: 测试\n",
                                        encoding="utf-8")
    monkeypatch.setattr(org, "ORG_DIR", tmp_path)
    report = org.org_check()
    assert report["valid"] is False
    assert any("缺少要素" in p for p in report["problems"])


def test_org_check_detects_bad_command(tmp_path, monkeypatch):
    (tmp_path / "a.yaml").write_text(
        "id: a\nname: A\nstage: 0\nparent: \"\"\n"
        "responsibilities: []\ninputs: []\noutputs: []\n"
        "check_rules: []\nforbidden: []\nbest_practices: []\n"
        "failure_cases: []\nauto_checks: [no-such-command-xyz]\n"
        "review_checklist: []\n", encoding="utf-8")
    monkeypatch.setattr(org, "ORG_DIR", tmp_path)
    report = org.org_check()
    assert report["valid"] is False
    assert any("命令不存在" in p for p in report["problems"])


def test_org_check_detects_unsupported_placeholder(tmp_path, monkeypatch):
    """未知占位符在静态层拒绝，避免运行时把字面量交给 shell。"""
    (tmp_path / "evolution.yaml").write_text(
        "id: evolution\nname: Evolution\nrole: 演化\nstage: 0\nparent: ''\n"
        "responsibilities: []\ninputs: []\noutputs: []\ncheck_rules: []\n"
        "forbidden: []\nbest_practices: []\nfailure_cases: []\n"
        "auto_checks: ['pi-batch retro --actions {unknown_token}']\n"
        "review_checklist: []\nrole_template: ''\n", encoding="utf-8")
    monkeypatch.setattr(org, "ORG_DIR", tmp_path)
    report = org.org_check()
    assert report["valid"] is False
    assert any("evolution" in problem and "{unknown_token}" in problem
               for problem in report["problems"])


def test_org_gate_skips_unsupported_placeholder(tmp_path, monkeypatch):
    """运行时遇到未知 token 不执行，也不能标记 passed。"""
    (tmp_path / "evolution.yaml").write_text(
        "id: evolution\nname: Evolution\nrole: 演化\nstage: 0\nparent: ''\n"
        "responsibilities: []\ninputs: []\noutputs: []\ncheck_rules: []\n"
        "forbidden: []\nbest_practices: []\nfailure_cases: []\n"
        "auto_checks: ['echo {unknown_token}']\nreview_checklist: []\n"
        "role_template: ''\n", encoding="utf-8")
    monkeypatch.setattr(org, "ORG_DIR", tmp_path)
    report = org.run_gate("evolution", task="task", code_dir="code")
    entry = report["results"][0]
    assert entry["status"] == "skipped"
    assert "不支持的占位符" in entry["reason"]
    assert "{unknown_token}" in entry["reason"]


def test_org_gate_does_not_shell_interpret_placeholder_values(tmp_path):
    """Untrusted task text is passed as argv data, never shell syntax."""
    marker = tmp_path / "injected"
    check = "python -c \"import sys; print(sys.argv[1])\" {task}"
    result = org._execute_check(
        check, {"{task}": f"; touch {marker}; echo INJECTED"})
    assert result["status"] == "passed"
    assert not marker.exists()
    assert "touch" in result["evidence"]


def test_check_exists_pi_batch_subcommand():
    commands = org._known_commands()
    assert org._check_exists("pi-batch impact --task", commands) is True
    assert org._check_exists("pi-batch no-such-cmd", commands) is False
    assert org._check_exists("scripts/check-ui-spec.py --dir", commands) is True
    assert org._check_exists("pytest tests/", commands) is True


def test_check_exists_file_reference():
    commands = org._known_commands()
    assert org._check_exists("backend-specs/", commands) is True
    assert org._check_exists("prompts/critic.md", commands) is True
    assert org._check_exists("no/such/file.md", commands) is False


def test_org_cli_check_json(capsys):
    with pytest.raises(SystemExit) as exc:
        org.org_main(["check", "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["valid"] is True
    assert report["node_count"] >= 16


def test_org_cli_flow(capsys):
    with pytest.raises(SystemExit) as exc:
        org.org_main(["flow"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "orchestrator" in out.lower() and "evolution" in out.lower()


def test_org_cli_show_unknown(capsys):
    with pytest.raises(SystemExit) as exc:
        org.org_main(["show", "no-such-node"])
    assert exc.value.code == 2


def test_org_cli_show_node(capsys):
    with pytest.raises(SystemExit) as exc:
        org.org_main(["show", "ux_designer"])
    assert exc.value.code == 0
    node = json.loads(capsys.readouterr().out)
    assert node["stage"] == 3
    assert node["auto_checks"]  # 设计师有自动检测（ui-spec 门禁）


# ---------------------------------------------------------------------------
# org prompt / org gate（岗位说明书 → 可注入、可执行）
# ---------------------------------------------------------------------------

def test_org_prompt_renders_nine_elements():
    node = next(n for n in org.load_nodes() if n["id"] == "ux_designer")
    prompt = org.render_role_prompt(node)
    for section in ("职责", "输入", "输出契约", "检查规则", "禁止事项",
                    "最佳实践", "Review Checklist", "自动检测"):
        assert section in prompt
    assert "- [ ]" in prompt  # checklist 自检项


def test_org_prompt_cli(capsys):
    with pytest.raises(SystemExit) as exc:
        org.org_main(["prompt", "security"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Security" in out and "禁止事项" in out


def test_org_gate_skipped_when_param_missing():
    """缺参占位符 → skipped（不伪装通过）。"""
    report = org.run_gate("developer", task="", code_dir="")
    skipped = [r for r in report["results"] if r["status"] == "skipped"]
    assert skipped  # {code}/{file} 未注入 → 跳过并说明


@pytest.mark.scenario
def test_org_gate_requirement_ok():
    """需求节点门禁：详尽需求 → 全部通过（assess/atomize/impact exit 0）。"""
    report = org.run_gate(
        "requirement",
        task="企业订单管理：新建订单表单，含支付审批流程，员工可提交，"
             "主管审批，异常超时重试，验收标准为审批流测试通过")
    assert report["gate_passed"] is True, report["results"]


def test_org_gate_attention_reported():
    """不完整需求 → reflect 低分 = attention（诊断发现，不阻塞门禁）。"""
    report = org.run_gate("requirement", task="加个按钮")
    attention = [r for r in report["results"] if r["status"] == "attention"]
    assert attention and any("reflect" in a["check"] for a in attention)
    assert report["gate_passed"] is True


def test_org_gate_cli_json(capsys):
    with pytest.raises(SystemExit) as exc:
        org.org_main(["gate", "requirement",
                      "--task", "完整需求：登录页，含角色权限、异常重试、"
                                 "验收标准测试", "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["gate_passed"] is True
    assert report["passed"] >= 3


def test_org_gate_unknown_node(capsys):
    with pytest.raises(SystemExit) as exc:
        org.org_main(["gate", "no-such-node"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# org run（流程串联）/ role_template / parent
# ---------------------------------------------------------------------------

def test_all_nodes_have_role_template():
    nodes = org.load_nodes()
    for node in nodes:
        template = node.get("role_template", "")
        assert template, f"{node['id']} 缺 role_template"
        assert Path(template).exists(), f"{node['id']} 模板不存在"


def test_new_role_templates_exist():
    """补的 6 个关键角色模板存在且含岗位要素。"""
    for name in ("orchestrator", "domain_expert", "api_designer",
                 "planning", "operation", "evolution"):
        path = Path(f"prompts/{name}.md")
        assert path.exists(), name
        text = path.read_text(encoding="utf-8")
        assert "## Role and Input" in text
        assert "## Focus" in text


def test_org_check_parent_valid():
    report = org.org_check()
    assert report["valid"] is True  # parent 全部存在（两遍校验后）


def test_org_check_detects_bad_parent(tmp_path, monkeypatch):
    (tmp_path / "a.yaml").write_text(
        "id: a\nname: A\nstage: 0\nparent: ghost\nrole_template: prompts/critic.md\n"
        "responsibilities: []\ninputs: []\noutputs: []\ncheck_rules: []\n"
        "forbidden: []\nbest_practices: []\nfailure_cases: []\n"
        "auto_checks: []\nreview_checklist: []\n", encoding="utf-8")
    monkeypatch.setattr(org, "ORG_DIR", tmp_path)
    report = org.org_check()
    assert report["valid"] is False
    assert any("parent 'ghost'" in p for p in report["problems"])


def test_org_run_flow_l1_simple_change():
    """简单按钮修改 → L1 流程（impact 判定），不触发架构评审。"""
    report = org.run_flow(task="把订单页按钮对齐")
    assert report["impact_level"] in ("L0", "L1")
    assert report["run_level"] in ("L0", "L1")
    node_ids = [n["node"] for n in report["nodes"]]
    assert "architecture" not in node_ids
    assert "developer" in node_ids


def test_org_frontend_flow_includes_ux_gate():
    report = org.run_flow(task="调整 React 订单页面按钮")
    assert "ux_designer" in [node["node"] for node in report["nodes"]]


def test_org_l3_flow_routes_all_organization_nodes(monkeypatch):
    monkeypatch.setattr(
        org, "run_gate",
        lambda node_id, *args: {"passed": 1, "failed": 0, "skipped": 0,
                                "attention": 0, "gate_passed": True,
                                "results": []},
    )
    report = org.run_flow(task="平台级生产变更", level="L3")
    ids = [node["node"] for node in report["nodes"]]
    assert set(ids) == {node["id"] for node in org.load_nodes()}
    assert report["flow_passed"] is True


@pytest.mark.scenario
def test_org_run_flow_l2_high_risk():
    """生产导出 → L2 流程：含架构/安全评审节点。"""
    report = org.run_flow(task="增加员工绩效导出Excel，生产环境")
    assert report["impact_level"] == "L2"
    node_ids = [n["node"] for n in report["nodes"]]
    assert "architecture" in node_ids and "security" in node_ids


def test_org_run_flow_passed_semantics():
    """flow_passed：节点 gate 全过才 True（skipped 不阻塞，failed 阻塞）。"""
    report = org.run_flow(task="把订单页按钮对齐")
    assert report["flow_passed"] is True


def test_org_run_flow_node_error_fails_closed(monkeypatch):
    """节点查找/配置错误不能被 continue 吞掉并把空流程判为通过。"""
    def fake_gate(node_id, *args):
        if node_id == "developer":
            return {"error": "developer gate configuration missing"}
        return {"passed": 0, "failed": 0, "skipped": 0, "attention": 0,
                "gate_passed": True, "results": []}

    monkeypatch.setattr(org, "run_gate", fake_gate)
    report = org.run_flow(task="把订单页按钮对齐", level="L1")
    failed = next(node for node in report["nodes"] if node["node"] == "developer")
    assert report["flow_passed"] is False
    assert failed["gate_passed"] is False
    assert failed["error"] == "developer gate configuration missing"


def test_org_run_cli_json(capsys):
    with pytest.raises(SystemExit) as exc:
        org.org_main(["run", "--task", "把订单页按钮对齐", "--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["run_level"] in ("L0", "L1")
    assert report["nodes"]


# ---------------------------------------------------------------------------
# org run --save / org runs / org lesson（流程工件与失败案例沉淀）
# ---------------------------------------------------------------------------

def test_save_and_list_flows(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path))
    report = {"task": "把订单页按钮对齐", "run_level": "L1",
              "flow_passed": True, "nodes": []}
    path = org.save_flow(report)
    assert path.startswith(str(tmp_path))
    records = org.list_flows()
    assert len(records) == 1
    assert records[0]["flow_passed"] is True
    assert records[0]["file"].endswith(".json")


def test_list_flows_bounded_and_ordered(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path))
    for index in range(3):
        org.save_flow({"task": f"t{index}", "run_level": "L0",
                       "flow_passed": True, "nodes": []})
    records = org.list_flows()
    assert len(records) == 3
    assert records[0]["task"] == "t2"  # 新→旧


def test_lesson_for_renders_learn_command():
    node = next(n for n in org.load_nodes() if n["id"] == "architecture")
    text = org.lesson_for(node)
    assert "失败案例与规则沉淀" in text
    assert "learn draft" in text
    assert "ORG-ARCHITECTURE-01" in text


def test_lesson_for_no_cases():
    node = {"id": "x", "failure_cases": []}
    assert "无失败案例" in org.lesson_for(node)


def test_org_runs_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path))
    org.save_flow({"task": "测试流程", "run_level": "L0",
                   "flow_passed": True, "nodes": []})
    with pytest.raises(SystemExit) as exc:
        org.org_main(["runs"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "测试流程" in out and "✓" in out


def test_org_lesson_cli(capsys):
    with pytest.raises(SystemExit) as exc:
        org.org_main(["lesson", "evolution"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "learn draft" in out


# ---------------------------------------------------------------------------
# org run --artifact / test-report / replay / counterfactuals
# ---------------------------------------------------------------------------

def test_org_run_artifact_writes_node_outputs(tmp_path):
    """工件链：每节点输出落盘（下游输入的最小机制）。"""
    artifact_dir = tmp_path / "art"
    report = org.run_flow(task="把订单页按钮对齐", artifact_dir=str(artifact_dir))
    assert report["artifact_dir"] == str(artifact_dir)
    for node in ("requirement", "developer", "testing"):
        path = artifact_dir / f"{node}.txt"
        assert path.exists(), node
        text = path.read_text(encoding="utf-8")
        assert "passed:" in text
    # requirement 有 task 注入 → 有实际执行结果；developer 缺 code 参数
    # → 全 skipped（缺参不伪装通过，工件如实标注）
    req = (artifact_dir / "requirement.txt").read_text(encoding="utf-8")
    assert "[passed]" in req or "[attention]" in req
    dev = (artifact_dir / "developer.txt").read_text(encoding="utf-8")
    assert "[skipped]" in dev


def test_test_report_classification(tmp_path):
    from pbatch import test_report
    (tmp_path / "test_alpha.py").write_text(
        "import pytest\n\n@pytest.mark.scenario\n"
        "def test_full_flow():\n    pass\n\n"
        "@pytest.mark.unit\ndef test_simple():\n    pass\n",
        encoding="utf-8")
    report = test_report.classify_tests(tmp_path)
    assert report["test_functions"] == 2
    assert report["categories"]["scenario"] == 1
    assert report["categories"]["unit"] == 1


def test_test_report_cli_json(capsys):
    from pbatch import test_report
    with pytest.raises(SystemExit) as exc:
        test_report.test_report_main(["--json"])
    assert exc.value.code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["test_functions"] >= 900
    assert "scenario" in report["categories"]


def test_pytest_markers_filter():
    """pytest.ini markers 生效：-m 可过滤分类运行。"""
    import subprocess
    result = subprocess.run(
        ["python", "-m", "pytest", "tests/test_org.py",
         "-m", "scenario", "-q", "--no-header"],
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-300:]
    assert "2 passed" in result.stdout or "1 passed" in result.stdout


def test_profile_counterfactuals():
    """反事实推理：多租户/国际化信号 → 预留建议。"""
    from pbatch import profile
    report = profile.task_profile(
        "做一个多租户 SaaS 员工绩效系统，未来支持国际化")
    suggestions = report["counterfactuals"]["suggestions"]
    assert any("tenant_id" in s for s in suggestions)
    assert any("locale" in s for s in suggestions)
    plain = profile.task_profile("把订单页按钮对齐")
    assert plain["counterfactuals"]["suggestions"] == []


def test_replay_compares_capsule_versions(tmp_path):
    """replay：胶囊版本对比 + eval 回归（--json 直接读 stdout）。"""
    from pbatch import capsule
    cap_path = tmp_path / "cap.json"
    from pbatch.capsule import capture_context_capsule
    import json as _json
    cap_path.write_text(_json.dumps(capture_context_capsule("任务")),
                        encoding="utf-8")
    import io, contextlib
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            capsule.replay_main(["--capsule", str(cap_path), "--json"])
        except SystemExit:
            pass  # eval 通过 → exit 0（正常终止）
    report = json.loads(buffer.getvalue())
    assert report["eval_passed"] is True
    assert "captured_rule_set" in report
    assert "current_rule_set" in report


# ---------------------------------------------------------------------------
# check 全面自检聚合
# ---------------------------------------------------------------------------

def test_check_steps_include_org_and_capabilities():
    """pi-batch check 聚合 org/capabilities/graph/tools 门禁。"""
    import argparse
    from pbatch import selfcheck
    args = argparse.Namespace(skip_eval=True, quick_eval=False, steps="")
    steps = selfcheck._default_steps(args)
    names = [name for name, _ in steps]
    for expected in ("quality", "registry", "org", "capabilities", "graph",
                     "tools", "knowledge"):
        assert expected in names


def test_check_step_filter():
    """--steps 过滤在 check_main 内实现（默认返回全量）。"""
    import argparse
    from pbatch import selfcheck
    args = argparse.Namespace(skip_eval=True, quick_eval=False, steps="")
    steps = selfcheck._default_steps(args)
    wanted = {name for name, _ in steps}
    assert "org" in wanted and "capabilities" in wanted


def test_check_run_step_ok():
    """org check 作为 check 步骤可运行且通过。"""
    from pbatch import selfcheck
    ok = selfcheck._run_step(
        "org", ["python", "-m", "pbatch", "org", "check"])
    assert ok is True


def test_check_cli_full(capsys):
    """完整 check（--steps 限定快速门禁）：全部通过（子进程级验证）。"""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "pi-batch.py", "check",
         "--steps", "org,capabilities,tools", "--skip-eval"],
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-300:]
    assert "all 3 gates passed" in result.stdout


# ---------------------------------------------------------------------------
# campaign 实战内化回归（2026-08-08 两轮 campaign 暴露）
# ---------------------------------------------------------------------------

def test_gate_leftover_placeholder_fails_closed():
    """未知占位符 → failed（不静默假通过）。"""
    from pbatch import org
    result = org._execute_check("echo {unknown_x}",
                                {"{task}": "t", "{code}": "", "{file}": "",
                                 "{log}": ""})
    assert result["status"] == "failed"
    assert "占位符" in result["reason"]
    assert "{unknown_x}" in result["reason"]


def test_gate_log_placeholder_known():
    """{log} 已知占位符：替换为日志路径执行成功。"""
    from pbatch import org
    params = {"{task}": "t", "{code}": "", "{file}": "",
              "{log}": "logs/x.log"}
    result = org._execute_check("echo {log}", params)
    assert result["status"] == "passed"
    assert "logs/x.log" in result["evidence"]


def test_evolution_gate_log_placeholder():
    """evolution 节点 {log} 不再静默假通过（campaign 发现的真 bug）。"""
    from pbatch import org
    gate = org.run_gate("evolution", task="复盘", log_path="")
    for result in gate["results"]:
        if "retro" in result["check"]:
            assert result["status"] == "skipped", result
            assert "{log}" in result["reason"]
