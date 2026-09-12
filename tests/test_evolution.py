"""Regression tests for the Evolution Patterns domain (第四域):
pbatch/evolution.py + evolution-specs/rules.yaml + the five
pure-stdlib gates (L1/L2/L6/L7/L9) distilled from the snaplink-console
and iris-ui campaigns.

Covers: project-shape classification, rule-level routing with
exploration bypass (restraint — patterns are memory, not constitution),
constitution overrides, non-goal conformance, and every gate script.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from pbatch.evolution import (  # noqa: E402
    _constitution_boilerplate, applicable_rules, check_conformance,
    classify_project_shape, load_constitution,
)
from pbatch.rule_matcher import load_registry  # noqa: E402


@pytest.fixture
def registry():
    return load_registry(domain="evolution")


# ── 形态分类 ────────────────────────────────────────────────────────

def test_shape_contract_driven(registry):
    shape = classify_project_shape(
        "实现审计时间线，走后端契约目录，权限边界严格，租户隔离", registry)
    assert shape["shape"] == "contract_driven"


def test_shape_platform(registry):
    shape = classify_project_shape(
        "发布组件库新包，四框架对齐，插件体系，npm 分发", registry)
    assert shape["shape"] == "platform_product"


def test_shape_exploration(registry):
    shape = classify_project_shape(
        "探索这个创意：AI 生成登录页原型，先调研", registry)
    assert shape["shape"] == "exploration"


def test_shape_generic_no_signal(registry):
    shape = classify_project_shape("修复登录按钮的 hover 样式", registry)
    assert shape["shape"] == "generic"


# ── 规律路由与分级 ──────────────────────────────────────────────────

def test_contract_project_loads_authority(registry):
    manifest = applicable_rules(
        "实现审计时间线，走后端契约目录，权限边界严格", registry)
    levels = {item["id"]: item["level"] for item in manifest["rules"]}
    assert levels["authority-layering"] == "contract"
    assert levels["machine-gates"] == "invariant"
    assert levels["evidence-discipline"] == "invariant"
    assert levels["non-goal-restraint"] == "invariant"
    # verify-by-doing 是 platform scope，契约项目跳过
    assert "verify-by-doing" not in levels


def test_exploration_bypass_downgrades_non_invariant(registry):
    """探索豁免：只留 invariant 安全底线，其余全部降级（创新不被锁死）。"""
    manifest = applicable_rules(
        "探索这个创意：AI 生成登录页原型", registry)
    levels = {item["id"]: item["level"] for item in manifest["rules"]}
    assert "adversarial-gate" not in levels              # low risk → skip
    assert "change-budget" not in levels                 # demo → suppress
    assert levels["decision-memory"] == "suggestion"    # contract → suggestion
    assert levels["machine-gates"] == "invariant"       # 安全底线保留
    assert levels["evidence-discipline"] == "invariant"
    assert levels["non-goal-restraint"] == "invariant"
    assert "authority-layering" not in levels           # scope 不匹配 → 跳过
    assert "verify-by-doing" not in levels
    downgraded = {item["id"] for item in manifest["downgraded"]}
    assert "decision-memory" in downgraded


def test_adversarial_gate_requires_high_risk_signal(registry):
    low = applicable_rules("改一行普通文案", registry)
    assert "adversarial-gate" not in {item["id"] for item in low["rules"]}
    assert any(item["id"] == "adversarial-gate" and "not high-risk" in item["reason"]
               for item in low["skipped"])

    high = applicable_rules("修复审计权限删除流程", registry)
    levels = {item["id"]: item["level"] for item in high["rules"]}
    assert high["risk"] == "high"
    assert levels["adversarial-gate"] == "contract"


def test_demo_suppresses_change_budget(registry):
    manifest = applicable_rules("做一个 demo 小工具", registry)
    assert manifest["scale"] == "demo"
    assert "change-budget" not in {item["id"] for item in manifest["rules"]}
    assert any(item["id"] == "change-budget" and "suppressed on demo" in item["reason"]
               for item in manifest["skipped"])


def test_low_confidence_rule_is_advisory():
    custom = {
        "scale": {}, "signals": {}, "signal_weights": {}, "risk": {},
        "rules": {"uncertain": {
            "level": "contract", "scope": "any", "confidence": 0.5,
        }},
    }
    manifest = applicable_rules("普通维护", custom)
    rule = next(item for item in manifest["rules"] if item["id"] == "uncertain")
    assert rule["level"] == "suggestion"
    assert "low confidence" in rule["reason"]


def test_prototype_counter_signal_downgrades_authority(registry):
    """prototype 反例信号 → authority-layering 局部降级。"""
    manifest = applicable_rules(
        "做一个独立演示 demo：审计时间线页面，无后端契约", registry)
    levels = {item["id"]: item["level"] for item in manifest["rules"]}
    # contract 信号命中但 prototype 反例 → 降级为 suggestion
    assert levels.get("authority-layering") == "suggestion"


def test_constitution_override_downgrades(registry, tmp_path):
    constitution = {
        "overrides": {"change-budget": {"reason": "单文件小程序，无需预算"}}}
    manifest = applicable_rules("改一行文案", registry, constitution)
    levels = {item["id"]: item["level"] for item in manifest["rules"]}
    assert levels["change-budget"] == "suggestion"
    reasons = [item["reason"] for item in manifest["downgraded"]
               if item["id"] == "change-budget"]
    assert reasons and "单文件小程序" in reasons[0]


# ── 宪法与一致性 ────────────────────────────────────────────────────

def test_load_constitution_missing_is_empty(tmp_path):
    assert load_constitution(tmp_path) == {}


def test_load_constitution_roundtrip(tmp_path, monkeypatch):
    target = tmp_path / "docs" / "evolution" / "constitution.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(_constitution_boilerplate(), encoding="utf-8")
    data = load_constitution(tmp_path)
    assert "explicit_non_goals" in data
    assert "change_budget" in data
    assert data["change_budget"]["max_files"] == 5


def test_check_conformance_collision():
    goals = ["未经维护者授权不执行首次 npm 发布", "不在浏览器内重新实现 Token 签名"]
    assert check_conformance("计划执行首次 npm 发布", goals)
    assert not check_conformance("优化按钮 hover 样式", goals)


def test_check_conformance_requires_strong_match():
    """单个短词不构成碰撞（防误伤）。"""
    goals = ["不在浏览器内重新实现 Token 签名"]
    assert not check_conformance("实现 Token 展示", goals)


# ── 门禁脚本（L1/L2/L6/L7/L9）───────────────────────────────────────

def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *args],
        capture_output=True, text=True)


def _empty_git_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"],
                   check=True)
    subprocess.run(["git", "-C", str(path), "commit", "--allow-empty", "-qm",
                    "init"], check=True)


def test_gate_baseline_missing(tmp_path):
    proc = _run("check-baseline.py", str(tmp_path))
    assert proc.returncode == 1
    assert "baseline missing" in proc.stderr


def test_gate_baseline_present(tmp_path):
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n", encoding="utf-8")
    proc = _run("check-baseline.py", str(tmp_path))
    assert proc.returncode == 0


def test_gate_authority_invents_api(tmp_path):
    artifact = tmp_path / "bad.md"
    artifact.write_text(
        "前端新增私有通道：支持 POST /api/v1/bff/audit/proxy 经 BFF "
        "传递 tenant_id，可以调用服务端所有审计能力。", encoding="utf-8")
    proc = _run("check-authority.py", str(artifact), "--dir", str(tmp_path))
    assert proc.returncode == 1
    assert "invented_api" in proc.stderr or "invented" in proc.stderr


def test_gate_authority_anchored(tmp_path):
    # 有契约目录锚点 + file:line 引用 → 通过
    (tmp_path / "backend-contracts.json").write_text(
        '{"ops": [{"id": "audit.read", "path": "/api/v1/audit/events"}]}',
        encoding="utf-8")
    artifact = tmp_path / "good.md"
    artifact.write_text(
        "审计读取走 GET /api/v1/audit/events（governance_tab.dart:166 实测），"
        "引用 backend-contracts.json 的 audit.read。", encoding="utf-8")
    proc = _run("check-authority.py", str(artifact), "--dir", str(tmp_path))
    assert proc.returncode == 0


def test_gate_evidence_fabricated_pass(tmp_path):
    artifact = tmp_path / "report.md"
    artifact.write_text("验证完成：理论上应该通过。", encoding="utf-8")
    proc = _run("check-evidence.py", str(artifact))
    assert proc.returncode == 1
    assert "fabricated_pass" in proc.stderr or "fabricated" in proc.stderr


def test_gate_evidence_clean(tmp_path):
    artifact = tmp_path / "report.md"
    artifact.write_text("验证完成：pnpm test 全部通过（873/873），exit=0。",
                        encoding="utf-8")
    proc = _run("check-evidence.py", str(artifact))
    assert proc.returncode == 0


def test_gate_nongoal_collision(tmp_path):
    (tmp_path / "docs" / "evolution").mkdir(parents=True)
    (tmp_path / "docs" / "evolution" / "constitution.yaml").write_text(
        "explicit_non_goals:\n  - 未经维护者授权不执行首次 npm 发布\n",
        encoding="utf-8")
    artifact = tmp_path / "claim.md"
    artifact.write_text("计划执行首次 npm 发布。", encoding="utf-8")
    proc = _run("check-nongoal.py", str(artifact), "--dir", str(tmp_path))
    assert proc.returncode == 1
    assert "non_goal_collision" in proc.stderr or "collides" in proc.stderr


def test_gate_nongoal_no_constitution_warns_only(tmp_path):
    """无宪法不是通过信号（vacuous-green ban），但也不阻断。"""
    artifact = tmp_path / "claim.md"
    artifact.write_text("计划执行首次 npm 发布。", encoding="utf-8")
    proc = _run("check-nongoal.py", str(artifact), "--dir", str(tmp_path))
    assert proc.returncode == 0
    assert "WARN" in proc.stderr


def test_gate_changebudget_over_limit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                   check=True)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    for name in "bcdef":
        (repo / f"{name}.txt").write_text(name, encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "evolution").mkdir(parents=True)
    (repo / "docs" / "evolution" / "constitution.yaml").write_text(
        "change_budget:\n  max_files: 5\n", encoding="utf-8")
    proc = _run("check-changebudget.py", "--dir", str(repo))
    assert proc.returncode == 1
    assert "files changed > budget" in proc.stderr


def test_gate_changebudget_within_limit(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"],
                   check=True)
    (repo / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    (repo / "b.txt").write_text("b", encoding="utf-8")
    proc = _run("check-changebudget.py", "--dir", str(repo), "--budget",
                "files=5,lines=300")
    assert proc.returncode == 0


# ── CLI 子命令 ──────────────────────────────────────────────────────

def test_evolution_subcommand_registered():
    proc = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "apply",
         "实现审计时间线，走后端契约目录"],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0
    assert "shape: contract_driven" in proc.stdout
    assert "authority-layering [contract]" in proc.stdout


def test_evolution_init_and_report(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "init",
         "--dir", str(tmp_path)],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0
    constitution = tmp_path / "docs" / "evolution" / "constitution.yaml"
    assert constitution.is_file()
    proc = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "report",
         "--dir", str(tmp_path)],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0
    assert "baseline-first" in proc.stdout
    assert "machine-gates" in proc.stdout


def test_evolution_check_without_task_skips_artifact_gate(tmp_path):
    repo = tmp_path / "repo"
    _empty_git_repo(repo)
    subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "init",
         "--dir", str(repo)], check=True, capture_output=True, text=True,
        cwd=ROOT)

    proc = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "check",
         "--dir", str(repo)], capture_output=True, text=True, cwd=ROOT)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[SKIP] nongoal" in proc.stdout
    assert "[PASS] baseline" in proc.stdout
    assert "[PASS] changebudget" in proc.stdout
    assert "required: artifact" not in proc.stdout + proc.stderr


def test_evolution_check_keeps_task_artifact_until_gate_finishes(tmp_path):
    repo = tmp_path / "repo"
    _empty_git_repo(repo)
    subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "init",
         "--dir", str(repo)], check=True, capture_output=True, text=True,
        cwd=ROOT)

    proc = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "check",
         "--dir", str(repo), "--task-text", "计划执行部署动作"],
        capture_output=True, text=True, cwd=ROOT)

    output = proc.stdout + proc.stderr
    assert proc.returncode == 1
    assert "non_goal_collision" in output or "collides" in output
    assert "artifact not found" not in output


def test_evolution_override_records_reason(tmp_path):
    subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "init",
         "--dir", str(tmp_path)],
        capture_output=True, text=True, cwd=ROOT)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "override",
         "--rule", "change-budget", "--reason", "单文件小程序无需预算",
         "--dir", str(tmp_path)],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0
    data = load_constitution(tmp_path)
    assert data["overrides"]["change-budget"]["reason"] == "单文件小程序无需预算"
    # 生效：apply 显示降级
    proc = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "apply",
         "改一行文案", "--dir", str(tmp_path)],
        capture_output=True, text=True, cwd=ROOT)
    assert "change-budget [suggestion]" in proc.stdout
    assert "constitution override" in proc.stdout


def test_rules_check_includes_evolution_domain():
    """第四域注册表必须通过 schema 校验（fail closed 集成）。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "rules", "--check"],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert "[evolution] registry: OK" in proc.stderr


def test_evolution_eval_suite_passes():
    """evals/evolution.yaml 全过（含探索豁免断言）。"""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "eval"],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0
    # 单独跑 evolution 套件确认无失败行
    proc = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "eval", "--filter",
         "generic-shape-no-signal"],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0
    assert "PASS" in proc.stderr


# ── P1: route_pipeline（L8）+ verify_experiment（L4）+ campaign L6 ─────

from pbatch.evolution import route_pipeline  # noqa: E402


def test_route_pipeline_release_is_maintainer_gate():
    route = route_pipeline("发布组件库新包到 npm")
    assert route["route"] == "release"
    assert route["pipeline"] == ""
    assert any("维护者授权" in r for r in route["reasons"])
    assert "evidence" in route["gates"]


def test_route_pipeline_backend_domain():
    route = route_pipeline("写一个订单创建接口，含支付审批，生产，go")
    assert route["route"] == "backend"
    assert "backend-implementation-pipeline.yaml" in route["pipeline"]


def test_route_pipeline_allowlist_adds_changebudget(tmp_path):
    constitution = {"change_budget": {"max_files": 5, "allowlist": ["lib/x/**"]}}
    route = route_pipeline("修复登录按钮样式", constitution)
    assert "changebudget" in route["gates"]
    assert any("白名单" in r for r in route["reasons"])


def test_verify_experiment_stage_schema_accepts_valid():
    from pbatch.pipeline_schema import validate_pipeline
    data = {"stages": [{"name": "impl", "from_prompt": "x", "output": "o.md",
                        "verify_experiment": ["python -c 'print(1)'"]}]}
    assert validate_pipeline(data) == []


def test_verify_experiment_stage_schema_rejects_non_list():
    from pbatch.pipeline_schema import validate_pipeline
    data = {"stages": [{"name": "impl", "from_prompt": "x", "output": "o.md",
                        "verify_experiment": "npm install"}]}
    errors = validate_pipeline(data)
    assert any("verify_experiment" in e for e in errors)


def test_verify_experiment_runs_and_gates(tmp_path, monkeypatch):
    """verify_experiment 命令失败 → 阶段失败（真实运行验证是门禁）。"""
    from pbatch.models import Stage
    from pbatch.pipeline import _run_stage_commands

    ok = Stage(name="t", cwd=str(tmp_path),
               verify_experiment=[sys.executable + " -c 'print(1)'"])
    assert _run_stage_commands(ok) is True
    bad = Stage(name="t", cwd=str(tmp_path),
                verify_experiment=[sys.executable + " -c 'import sys; sys.exit(3)'"])
    assert _run_stage_commands(bad) is False


def test_campaign_non_goal_collision_blocks_direction(tmp_path):
    """L6：方向撞明确不做 → campaign 选择器拒绝（fail closed）。"""
    from pbatch.campaign_analysis import Direction, non_goal_collision, \
        select_directions

    (tmp_path / "docs" / "evolution").mkdir(parents=True)
    (tmp_path / "docs" / "evolution" / "constitution.yaml").write_text(
        "explicit_non_goals:\n  - 未经维护者授权不执行首次 npm 发布\n",
        encoding="utf-8")
    direction = Direction(
        module="lib", title="执行首次 npm 发布",
        problem="把组件库发布到 npm，需要授权",
        value=9, risk_reduction=9, effort=2, confidence=9,
        evidence=["packages/core/package.json"], acceptance=["npm publish 成功"])
    assert non_goal_collision(tmp_path, direction)
    (tmp_path / "packages" / "core").mkdir(parents=True)
    (tmp_path / "packages" / "core" / "package.json").write_text("{}")
    selected, rejected = select_directions(tmp_path, [direction], maximum=1,
                                           minimum_score=0)
    assert not selected
    assert any("non-goal collision" in reason for _, reason in rejected)


def test_campaign_non_goal_no_constitution_passes(tmp_path):
    """无宪法 → 不拦截（缺省边界不是通过信号，但也不臆造边界）。"""
    from pbatch.campaign_analysis import Direction, non_goal_collision
    direction = Direction(module="lib", title="执行首次发布", problem="发布动作",
                          value=9, risk_reduction=9, effort=2, confidence=9,
                          evidence=[], acceptance=[])
    assert non_goal_collision(tmp_path, direction) == ""


# ── P2: learn 生命周期 / report self-review / context 宪法注入 ─────────

def test_learn_promote_evolution_requires_samples(tmp_path, monkeypatch):
    """evolution 域 promote 强制归纳出处（规律是记忆，必须有 samples）。"""
    from pbatch.learn import DRAFT_DIR, PROMOTED_DIR, promote_draft
    monkeypatch.setattr("pbatch.learn.PROMOTED_DIR", tmp_path / "promoted")

    drafts = tmp_path / "drafts"
    drafts.mkdir()
    (drafts / "EVO-001-20260808.yaml").write_text(
        "id: EVO-001\ntitle: 契约纪律\nrequirements: [必须引用目录]\n"
        "samples: []\nconfidence: 0.5\n", encoding="utf-8")
    with pytest.raises(ValueError, match="samples"):
        promote_draft("EVO-001", registry="evolution",
                      files=["evolution-specs/anti-patterns.md"],
                      draft_dir=drafts,
                      registry_path=str(tmp_path / "rules.yaml"))
    # 带 samples + confidence → 成功并写入注册表
    (drafts / "EVO-001-20260808.yaml").write_text(
        "id: EVO-001\ntitle: 契约纪律\nrequirements: [必须引用目录]\n"
        "samples: [snaplink-console]\nconfidence: 0.8\n", encoding="utf-8")
    (tmp_path / "rules.yaml").write_text("rules: {}\n", encoding="utf-8")
    registry_path, _ = promote_draft(
        "EVO-001", registry="evolution",
        files=["evolution-specs/anti-patterns.md"],
        draft_dir=drafts, registry_path=str(tmp_path / "rules.yaml"))
    text = Path(registry_path).read_text(encoding="utf-8")
    assert "samples: ['snaplink-console']" in text
    assert "confidence: 0.8" in text


def test_learn_category_evolution_accepted():
    from pbatch.learn import failure_to_draft
    draft = failure_to_draft("前端发明了后端不存在的接口", "evolution",
                             "error", "EVO-AUTHORITY-001")
    assert draft["samples"] == []
    assert draft["confidence"] == 0.5


def test_evolution_report_self_review(tmp_path):
    subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "init",
         "--dir", str(tmp_path)],
        capture_output=True, text=True, cwd=ROOT)
    subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "override",
         "--rule", "change-budget", "--reason", "小程序无需预算",
         "--dir", str(tmp_path)],
        capture_output=True, text=True, cwd=ROOT)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "report",
         "--self-review", "--dir", str(tmp_path)],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0
    assert "overrides" in proc.stdout
    assert "change-budget" in proc.stdout
    assert "self-review" in proc.stdout
    assert "override 事件" in proc.stdout


def test_context_injects_constitution_on_keywords(tmp_path):
    """任务命中宪法关键词 + 项目有 constitution.yaml → 自动注入。"""
    from pbatch.context import _with_constitution_route

    (tmp_path / "docs" / "evolution").mkdir(parents=True)
    (tmp_path / "docs" / "evolution" / "constitution.yaml").write_text(
        "explicit_non_goals: []", encoding="utf-8")
    routes = []
    expanded = _with_constitution_route(routes, "这个方向违反明确不做吗？", str(tmp_path))
    assert any("constitution.yaml" in str(r.get("load"))
               for r in expanded)
    # 无关任务不注入
    assert _with_constitution_route([], "优化按钮样式", str(tmp_path)) == []
    # 无宪法不注入
    bare = tmp_path / "bare"
    bare.mkdir()
    assert _with_constitution_route([], "宪法相关任务", str(bare)) == []


# ── P3: campaign 分析注入宪法 + 已拒方向（L6/L10 防重复建议）───────

def test_evolution_context_injects_constitution_and_rejected(tmp_path):
    """分析 prompt 自动携带明确不做 + 历史已拒方向（有界）。"""
    from pbatch.campaign_analysis import _evolution_context

    (tmp_path / "docs" / "evolution").mkdir(parents=True)
    (tmp_path / "docs" / "evolution" / "constitution.yaml").write_text(
        "explicit_non_goals:\n  - 未经维护者授权不执行首次 npm 发布\n"
        "change_budget:\n  max_files: 5\n", encoding="utf-8")
    (tmp_path / "docs" / "auto").mkdir(parents=True)
    import json as _json
    with (tmp_path / "docs" / "auto" / "state.jsonl").open("w", encoding="utf-8") as h:
        h.write(_json.dumps({"status": "PASSED", "direction": "ok"}) + "\n")
        h.write(_json.dumps({"status": "DIRECTION_REJECTED",
                             "direction": "发明 BFF 接口",
                             "reason": "no invented BFF"}) + "\n")
    text = _evolution_context(tmp_path)
    assert "首次 npm 发布" in text
    assert "max_files=5" in text
    assert "发明 BFF 接口" in text
    assert "no invented BFF" in text


def test_build_analysis_task_carries_context(tmp_path):
    """build_analysis_task 的 prompt 含宪法/已拒块；无宪法时不追加。"""
    from pbatch.campaign_analysis import build_analysis_task
    from pbatch.campaign_models import CampaignSettings

    settings = CampaignSettings(
        name="t", output_dir="docs/auto", analysis_prompt="Analyze {module}")
    # 无宪法 → prompt 与 settings 模板一致（不臆造边界）
    task = build_analysis_task(tmp_path, settings, "lib/api")
    assert task.prompt == "Analyze lib/api"
    # 有宪法 → 追加 context 块
    (tmp_path / "docs" / "evolution").mkdir(parents=True)
    (tmp_path / "docs" / "evolution" / "constitution.yaml").write_text(
        "explicit_non_goals:\n  - 不发明接口\n", encoding="utf-8")
    task = build_analysis_task(tmp_path, settings, "lib/api")
    assert "Project context" in task.prompt
    assert "不发明接口" in task.prompt


# ── P4: assess 宪法块 / advance denylist / campaign 轨迹事件 ─────────

def test_assess_attaches_constitution(monkeypatch, tmp_path):
    """assess 输出携带项目宪法摘要（L6/L9 在评估时可见）。"""
    from pbatch.evolution import attach_constitution, format_constitution_block

    (tmp_path / "docs" / "evolution").mkdir(parents=True)
    (tmp_path / "docs" / "evolution" / "constitution.yaml").write_text(
        "explicit_non_goals:\n  - 未经维护者授权不执行首次 npm 发布\n"
        "change_budget:\n  max_files: 5\n"
        "overrides:\n  change-budget:\n    reason: 小程序\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assessment = {}
    attach_constitution(assessment)
    assert assessment["constitution"]["non_goals"]
    assert assessment["constitution"]["budget"]["max_files"] == 5
    assert assessment["constitution"]["overrides"] == ["change-budget"]
    text = format_constitution_block(assessment)
    assert "明确不做" in text
    assert "首次 npm 发布" in text
    assert "change-budget" in text


def test_assess_no_constitution_skips(monkeypatch, tmp_path):
    from pbatch.evolution import attach_constitution
    monkeypatch.chdir(tmp_path)
    assessment = {}
    attach_constitution(assessment)
    assert "constitution" not in assessment


def test_advance_build_batches_respects_denylist():
    """宪法 denylist 命中的文件从 advance 批次剔除（修复不触碰被禁路径）。"""
    from pbatch.advance_support import build_batches

    scan = {"dimensions": {"fe_complexity": {
        "priority": 2, "count": 2, "label": "复杂度", "fix": "拆分",
        "files": ["lib/x/god.dart", "lib/y/ok.dart"],
        "samples": ["lib/x/god.dart:12"]}}}
    batches = build_batches(scan, denylist=["lib/x/**"])
    assert batches[0]["files"] == ["lib/y/ok.dart"]
    # 全部文件被剔除 → 批次整体跳过
    batches = build_batches(scan, denylist=["lib/**"])
    assert batches == []
    batches = build_batches(scan, denylist=["lib/y/**"])
    assert batches[0]["files"] == ["lib/x/god.dart"]
    batches = build_batches(scan)
    assert len(batches[0]["files"]) == 2


def test_advance_denylist_glob_semantics():
    from pbatch.advance_support import _denylist_matches
    assert _denylist_matches("lib/x/god.dart", ["lib/x/**"])
    assert _denylist_matches("lib/x/god.dart", ["**/god.dart"])
    assert not _denylist_matches("lib/x/god.dart", ["lib/y/**"])
    assert _denylist_matches("lib/x/god.dart", ["lib/*/god.dart"])


def test_campaign_records_evolution_trajectory(tmp_path, monkeypatch):
    """campaign 完成事件沉淀到用户级 evolution/state.jsonl（轨迹数据源）。"""
    from types import SimpleNamespace
    from pbatch import config
    from pbatch.campaign_exec import _record_evolution_trajectory

    outcome = SimpleNamespace(module="lib/api", direction="审计读路径",
                              status="PASSED")
    context = SimpleNamespace(root=tmp_path)
    _record_evolution_trajectory(context, [outcome])
    state = config.data_dir(str(tmp_path)) / "evolution" / "state.jsonl"
    assert state.is_file()
    line = json.loads(state.read_text(encoding="utf-8"))
    assert line["event"] == "campaign_done"
    assert line["module"] == "lib/api"
    assert line["status"] == "PASSED"
    assert "ts" in line


# ── P5: evolution track（演化路径分析）+ verify 失败诊断闭环 ─────────

def test_track_evolution_summarizes_events(tmp_path):
    """轨迹分析：触发源分类 + 状态分布 + 被拒收敛信号。"""
    from pbatch.evolution import _append_state, track_evolution

    _append_state(tmp_path, {"event": "campaign_done",
                             "module": "lib/api",
                             "direction": "实现审计时间线，走后端契约目录",
                             "status": "PASSED"})
    _append_state(tmp_path, {"event": "campaign_done",
                             "module": "lib/screens",
                             "direction": "发明 BFF 接口被拒",
                             "status": "DIRECTION_REJECTED",
                             "reason": "no invented BFF"})
    _append_state(tmp_path, {"event": "override", "rule": "change-budget",
                             "reason": "小程序"})
    report = track_evolution(tmp_path)
    assert report["summary"]["total"] == 3
    assert "contract_gap" in report["sources"]
    assert report["statuses"]["PASSED"] == 1
    assert report["statuses"]["DIRECTION_REJECTED"] == 1
    assert report["modules"]["lib/api"] == 1
    assert any("发明 BFF" in item["direction"] for item in report["rejected"])


def test_track_evolution_empty(tmp_path):
    from pbatch.evolution import track_evolution
    report = track_evolution(tmp_path)
    assert report["summary"]["total"] == 0


def test_classify_trigger_sources():
    from pbatch.evolution import classify_trigger
    assert classify_trigger("走后端契约目录") == "contract_gap"
    assert classify_trigger("UI 打磨：登录页体验") == "product_feedback"
    assert classify_trigger("复杂度重构") == "tech_debt"
    assert classify_trigger("四框架对齐") == "parity"
    assert classify_trigger("发布 npm") == "release"
    assert classify_trigger("随便什么") == "other"


def test_evolution_track_cli(tmp_path):
    """evolution track 子命令 + --out 写 TRACK.md。"""
    from pbatch.evolution import _append_state
    _append_state(tmp_path, {"event": "campaign_done", "module": "lib",
                             "direction": "审计读路径", "status": "PASSED"})
    proc = subprocess.run(
        [sys.executable, str(ROOT / "pi-batch.py"), "evolution", "track",
         "--dir", str(tmp_path), "--out", "docs/evolution/TRACK.md"],
        capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0
    track = tmp_path / "docs" / "evolution" / "TRACK.md"
    assert track.is_file()
    text = track.read_text(encoding="utf-8")
    assert "触发源分布" in text
    assert "审计读路径" in text


def test_verify_failure_writes_diagnostics(tmp_path):
    """verify_experiment 失败 → 用户级 runtime 中的有界诊断文件。"""
    from pbatch import config
    from pbatch.models import Stage
    from pbatch.pipeline import _run_stage_commands

    stage = Stage(name="v", cwd=str(tmp_path),
                  verify_experiment=[sys.executable + " -c 'import sys; sys.exit(7)'"])
    assert _run_stage_commands(stage) is False
    rejected = config.data_dir(str(tmp_path)) / "rejected"
    files = list(rejected.glob("verify-cmd-*.txt")) if rejected.is_dir() else []
    assert files
    text = files[0].read_text(encoding="utf-8")
    assert "exit=7" in text
    assert "sys.exit(7)" in text
