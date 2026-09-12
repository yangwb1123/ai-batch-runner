"""Evolution Patterns Engine — 实战规律路由与项目宪法（第四域）。

归纳自两个实战项目（snaplink-console / iris-ui）用本工具跑出的规律，
注册表在 evolution-specs/rules.yaml。引擎做三件事：

1. classify_project_shape:  项目形态分类（契约驱动/平台/面向产品/探索）
2. applicable_rules:        按形态 × 反例信号 × 任务场景路由规律，
                            invariant 硬执行、contract/policy 默认强制
                            （宪法可豁免）、suggestion 只注入
3. check_conformance:       方向/产物 vs 项目明确不做（L6，fail closed）

探索豁免（spike）：exploration 信号命中 → contextual/advisory 全降级，
只留 invariant 安全底线——防止规律限制创新（规律是记忆，不是宪法）。

用法：
    pi-batch evolution "写一个审计时间线"          # 规律适用清单
    pi-batch evolution init [--dir]               # 生成项目宪法骨架
    pi-batch evolution check [--dir]              # 项目宪法门禁（只读）
    pi-batch evolution override --rule X --reason  # 宪法豁免（留痕）
    pi-batch evolution report [--dir]             # 命中/豁免/stale 报告
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config
from .config import log, yaml
from .relevance import _keyword_hit
from .rule_matcher import LEVELS, detect_scale, load_registry
from .memory_io import reverse_lines
from .text_io import read_text_bounded

REGISTRY_PATH = "evolution-specs/rules.yaml"
CONSTITUTION_REL = "docs/evolution/constitution.yaml"
STATE_REL = "docs/evolution/state.jsonl"  # legacy read fallback


def _runtime_state_path(root: Path) -> Path:
    """Return the private evolution event ledger for one workspace."""
    return config.data_dir(str(root)) / "evolution" / "state.jsonl"


def _state_paths(root: Path) -> tuple[Path, ...]:
    """Return runtime and legacy ledgers, without silently dropping history."""
    runtime = _runtime_state_path(root)
    legacy = root / STATE_REL
    paths = []
    if runtime.is_file():
        paths.append(runtime)
    if legacy.is_file() and legacy != runtime:
        paths.append(legacy)
    return tuple(paths)


def _state_read_path(root: Path) -> Path:
    """Return the preferred ledger path for diagnostics and compatibility."""
    paths = _state_paths(root)
    return paths[0] if paths else _runtime_state_path(root)


# 反例信号 → 命中的信号名（注册表 signals 节内声明，此处是引擎内置语义）。
_COUNTER_SIGNALS = {"exploration", "prototype", "quick"}


def classify_project_shape(text: str, registry: Optional[dict] = None) -> dict:
    """形态分类：注册表 signals 节关键词命中评分，返回全部信号分值与主导形态。

    无命中 → 形态 "generic"（不加载任何 contextual 规律）。
    """
    if registry is None:
        registry = load_registry(domain="evolution")
    lowered = (text or "").lower()
    signals = registry.get("signals", {})
    weights = registry.get("signal_weights", {})
    scored: dict = {}
    for name, terms in signals.items():
        matched = tuple(t for t in terms if _keyword_hit(lowered, str(t)))
        if matched:
            scored[name] = {
                "score": len(matched) * float(weights.get(name, 1.0)),
                "matched": list(matched),
            }
    if not scored:
        return {"shape": "generic", "signals": {}}
    dominant = max(scored, key=lambda k: scored[k]["score"])
    return {"shape": dominant, "signals": scored}


def _counter_hit(signals: dict, counter: list) -> Optional[str]:
    for name in counter:
        if name in signals:
            return name
    return None


def _high_risk(task_text: str, registry: dict) -> bool:
    """Whether any declared high-risk vocabulary is present."""
    lowered = (task_text or "").lower()
    return any(_keyword_hit(lowered, str(term))
               for terms in (registry.get("risk") or {}).values()
               for term in terms)


def _apply_one_rule(rule_id: str, rule: dict, shape: str, signals: dict,
                    overrides: dict, exploring: bool, tier: str,
                    high_risk: bool) -> dict:
    """单条规律的生效决策：{action: load|skip, level, reason}。

    - scope 不匹配 → skip
    - 宪法 override → 降级为 suggestion（带豁免理由）
    - 探索/反例信号 → 非 invariant 降级为 suggestion（安全底线保留）
    """
    level = str(rule.get("level", "suggestion"))
    if level not in LEVELS:
        level = "suggestion"
    scope = rule.get("scope", "any")
    if scope != "any" and scope != shape:
        return {"action": "skip",
                "reason": f"scope '{scope}' != shape '{shape}'"}
    if level != "invariant" and rule.get("required_when_risk") and not high_risk:
        return {"action": "skip", "reason": "not high-risk"}
    suppressed = rule.get("suppress_on") or []
    if level != "invariant" and tier in suppressed:
        return {"action": "skip", "reason": f"suppressed on {tier}"}
    override = overrides.get(rule_id)
    if override:
        return {"action": "load", "level": "suggestion",
                "reason": f"constitution override: {override.get('reason', '')}"}
    cause = "exploration" if exploring else _counter_hit(
        signals, rule.get("counter_signals") or [])
    if cause:
        if level == "invariant":
            return {"action": "load", "level": level}
        return {"action": "load", "level": "suggestion", "reason": cause}
    confidence = rule.get("confidence", 1.0)
    if (level != "invariant"
            and isinstance(confidence, (int, float))
            and not isinstance(confidence, bool)
            and confidence < 0.6):
        return {"action": "load", "level": "suggestion",
                "reason": f"low confidence: {confidence}"}
    return {"action": "load", "level": level}


def applicable_rules(task_text: str, registry: Optional[dict] = None,
                     constitution: Optional[dict] = None) -> dict:
    """返回 {rules, downgraded, skipped} 规律适用清单。

    - level=invariant: 恒适用（探索期也保留——安全底线）
    - scope 不匹配: skipped
    - counter_signals 命中 / 探索形态: 非 invariant 降级为 suggestion
    - constitution overrides: 降级为 suggestion（带豁免理由）
    """
    if registry is None:
        registry = load_registry(domain="evolution")
    shape_info = classify_project_shape(task_text, registry)
    signals = shape_info["signals"]
    shape = shape_info["shape"]
    overrides = ((constitution or {}).get("overrides") or {})
    exploring = "exploration" in signals
    tier, tier_evidence = detect_scale(task_text, registry)
    high_risk = _high_risk(task_text, registry)

    rules, downgraded, skipped = [], [], []
    for rule_id, rule in (registry.get("rules") or {}).items():
        decision = _apply_one_rule(str(rule_id), rule, shape, signals,
                                   overrides, exploring, tier, high_risk)
        if decision["action"] == "skip":
            skipped.append({"id": str(rule_id), "reason": decision["reason"]})
            continue
        reason = decision.get("reason")
        if reason:
            downgraded.append({
                "id": str(rule_id), "level": rule.get("level", "suggestion"),
                "to": decision["level"], "reason": reason})
        rules.append({"id": str(rule_id), "level": decision["level"],
                      "scope": rule.get("scope", "any"),
                      **({"reason": reason} if reason else {})})
    return {"shape": shape, "signals": signals, "scale": tier,
            "scale_evidence": list(tier_evidence),
            "risk": "high" if high_risk else "low",
            "rules": rules, "downgraded": downgraded, "skipped": skipped}


def load_constitution(root: Path) -> dict:
    """读取项目宪法（docs/evolution/constitution.yaml 或 evolution.yaml）。

    缺省返回空 dict（不报错——没有宪法时规律仍按注册表级别生效，
    只是没有项目级 non-goals / budget / overrides 上下文）。
    """
    for name in (CONSTITUTION_REL, "evolution.yaml"):
        candidate = root / name
        if not candidate.is_file():
            continue
        try:
            data = yaml.safe_load(read_text_bounded(
                candidate, config.INPUT_MAX_BYTES, "constitution")) or {}
        except Exception:
            return {}
        if isinstance(data, dict):
            return data
    return {}


def check_conformance(text: str, goals: list) -> list:
    """方向/产物 vs 明确不做清单 → 碰撞清单（至少 2 个关键词命中或
    1 个 ≥4 字长中文词命中；英文词需 ≥6 字符或双命中，避免误伤）。"""
    lowered = (text or "").lower()
    collisions = []
    for goal in goals:
        if not isinstance(goal, str):
            continue
        terms = [t.strip(" '\"`.,;:()[]{}") for t in goal.replace("/", " ").split()
                 if len(t.strip(" '\"`.,;:()[]{}")) >= 2]
        hits = [t for t in terms if t.lower() in lowered]
        strong = [t for t in hits
                  if (any("\u4e00" <= c <= "\u9fff" for c in t) and len(t) >= 4)
                  or (not any("\u4e00" <= c <= "\u9fff" for c in t)
                      and len(t) >= 6)]
        if len(hits) >= 2 or strong:
            collisions.append({"goal": goal, "matched": hits[:5]})
    return collisions


# L8: 管线形状路由表（风险等级 × 产品化级别 × 变更预算 → 模板）。
_ROUTES = {
    "frontend": {
        "path": "examples/frontend-implementation-pipeline.yaml",
        "label": "前端实现管线（plan→implement+门禁→对抗审查→裁决门）",
    },
    "backend": {
        "path": "examples/backend-implementation-pipeline.yaml",
        "label": "后端实现管线（plan含持久化设计→implement+双门禁→对抗审查→裁决门）",
    },
    "adversarial": {
        "path": "examples/repository-campaign-pipeline.yaml",
        "label": "对抗管线（需求→设计→多角色对抗→裁决门，gate-fix 收敛）",
    },
    "release": {
        "path": "",
        "label": "授权门管线（发布/不可逆动作：维护者显式授权后才执行）",
    },
}


def _route_signals(task_text: str) -> dict:
    """路由信号：领域分类 / 风险等级 / 产品化级别 / 发布信号。"""
    from .assessor import workflow_level
    from .classifier import classify_text
    from .product import productization_level

    cls = classify_text(task_text)
    lowered = task_text.lower()
    release_signal = any(k in lowered for k in
                         ("publish", "发布", "release", "npm publish",
                          "上线", "部署", "deploy"))
    return {"cls": cls,
            "workflow": workflow_level(task_text)["level"],
            "product": productization_level(task_text)[0],
            "release": release_signal}


def _route_choice(cls, wf: str, lowered: str, task_text: str,
                  release_signal: bool) -> tuple:
    """主路由选择：发布 > 高风险 > 领域。返回 (route, reason)。"""
    if release_signal:
        return ("release", "发布/不可逆动作 → 维护者授权门（L6 克制：不自动开工）")
    if wf in ("L2_high_risk", "L3_platform"):
        return ("adversarial", f"风险等级 {wf} → 对抗管线（L5：多角色对抗+裁决门）")
    if cls.task_type == "frontend_ui":
        return ("frontend", "前端任务 → UI 实现管线（L3：uiquality/uicheck 门禁）")
    if cls.task_type == "backend":
        return ("backend", "后端任务 → 后端实现管线（L3：backendquality/completion 门禁）")
    route = "frontend" if ("ui" in lowered or "页面" in task_text) else "backend"
    return (route, f"通用任务 → {route} 管线（默认保守路由）")


def route_pipeline(task_text: str, constitution: Optional[dict] = None) -> dict:
    """L8 管线形状路由：领域（classify）× 风险等级（workflow）
    × 产品化级别 × 变更预算（constitution）→ 推荐模板 + 理由。
    实战出处：snaplink 白名单/对抗管线 vs iris-ui release 授权门。
    """
    signals = _route_signals(task_text)
    cls, wf, prod = signals["cls"], signals["workflow"], signals["product"]
    budget = ((constitution or {}).get("change_budget") or {})
    allowlist = budget.get("allowlist") or []
    lowered = task_text.lower()

    release_signal = signals["release"]
    high_risk = wf in ("L2_high_risk", "L3_platform") or release_signal
    route, reason = _route_choice(cls, wf, lowered, task_text, release_signal)
    reasons, gates = [reason], []

    if prod != "L0_local_feature":
        gates.append("baseline")
        reasons.append(f"产品化 {prod} → baseline 基线门禁（L1）")
    if allowlist:
        gates.append("changebudget")
        reasons.append(f"宪法声明 {len(allowlist)} 个白名单路径 → changebudget 门禁（L9）")
    if high_risk:
        gates.extend(["evidence", "nongoal"])
        reasons.append("高风险 → evidence/nongoal 门禁（L7/L6）")

    entry = _ROUTES[route]
    return {"route": route, "pipeline": entry["path"],
            "label": entry["label"], "reasons": reasons,
            "gates": sorted(set(gates)),
            "signals": {"task_type": cls.task_type,
                         "workflow_level": wf,
                         "productization_level": prod,
                         "release": release_signal}}
# 触发源分类（零 LLM 关键词启发式）：从方向/状态/原因文本推断
# 事件属于哪类驱动——"为什么走这条路线"的证据链分类。
_TRIGGER_SOURCES = (
    ("contract_gap", ("契约", "contract", "接口", "边界", "api", "后端",
                       "目录", "catalog")),
    ("product_feedback", ("产品", "反馈", "体验", "ux", "ui", "设计",
                           "product", "feedback", "redesign")),
    ("tech_debt", ("技术债", "复杂度", "重构", "嵌套", "god", "i18n",
                    "魔法间距", "tech", "debt")),
    ("architecture", ("架构", "模块", "隔离", "依赖", "architecture",
                       "boundary", "分层")),
    ("quality_gate", ("门禁", "验证", "validator", "gate", "测试",
                       "test", "coverage", "audit")),
    ("parity", ("对齐", "四框架", "跨框架", "parity", "漂移", "一致")),
    ("release", ("发布", "分发", "发布", "release", "publish", "打包")),
)


_TRIGGER_REJECTED = ("REJECTED", "FAILED", "FAIL")


def classify_trigger(text: str) -> str:
    """触发源分类（零 LLM）：contract_gap / product_feedback /
    tech_debt / architecture / quality_gate / parity / release / other。"""
    lowered = (text or "").lower()
    for name, terms in _TRIGGER_SOURCES:
        if any(term in lowered for term in terms):
            return name
    return "other"


def _collect_events(state: Path, max_events: int) -> list:
    """有界倒序读取 state.jsonl 尾部事件。"""
    if max_events <= 0 or not state.is_file():
        return []
    events = []
    try:
        for line in reverse_lines(state, maximum=256 * 1024):
            if line is None or not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("event"):
                events.append(row)
                if len(events) >= max_events:
                    break
    except OSError:
        return []
    return list(reversed(events))


def _summarize_events(events: list) -> dict:
    """触发源/状态/模块/被拒统计。"""
    sources, statuses, modules, rejected = {}, {}, {}, []
    for row in events:
        status = str(row.get("status") or "")
        if status:
            statuses[status] = statuses.get(status, 0) + 1
        module = str(row.get("module") or "")
        if module:
            modules[module] = modules.get(module, 0) + 1
        direction = str(row.get("direction") or "")
        source = classify_trigger(direction + " " + str(row.get("reason") or ""))
        sources[source] = sources.get(source, 0) + 1
        if any(token in status for token in _TRIGGER_REJECTED):
            rejected.append({"direction": direction[:120],
                             "status": status,
                             "reason": str(row.get("reason") or "")[:100]})
    return {"sources": dict(sorted(sources.items(), key=lambda item: -item[1])),
            "statuses": dict(sorted(statuses.items(), key=lambda item: -item[1])),
            "modules": dict(sorted(modules.items(), key=lambda item: -item[1])[:8]),
            "rejected": rejected[-12:]}


def track_evolution(root: Path, max_events: int = 2000) -> dict:
    """演化轨迹分析：读用户级 evolution/state.jsonl（事件）→ 时间线 +
    触发源分类 + 状态分布 + 模块热度 + 拒绝模式。有界行缓冲。

    数据源事件：init / override / check / campaign_done（追加式）。
    """
    events = []
    for state in _state_paths(root):
        events.extend(_collect_events(state, max_events))
    events.sort(key=lambda row: str(row.get("ts") or ""))
    events = events[-max_events:]
    if not events:
        return {"events": [], "summary": {"total": 0}}
    summary = _summarize_events(events)
    return {"events": events, "summary": {"total": len(events)}, **summary}


def format_track(report: dict) -> str:
    """演化路径分析 md（TRACK.md 形态）。"""
    if not report.get("events"):
        return "# 演化轨迹\n\n（暂无事件——跑过 campaign/override/check 后自动沉淀）"
    lines = ["# 演化轨迹（Evolution Track）", ""]
    lines.append(f"事件总数: {report['summary']['total']}")
    sources = report["sources"]
    lines.append("\n## 触发源分布（为什么走这些路线）")
    for name, count in sources.items():
        lines.append(f"- {name}: {count}")
    statuses = report["statuses"]
    lines.append("\n## 状态分布")
    for name, count in statuses.items():
        lines.append(f"- {name}: {count}")
    modules = report["modules"]
    if modules:
        lines.append("\n## 模块热度")
        for name, count in modules.items():
            lines.append(f"- {name}: {count}")
    rejected = report["rejected"]
    if rejected:
        lines.append("\n## 近期被拒（收敛信号）")
        for item in rejected:
            lines.append(f"- [{item['status']}] {item['direction']} "
                         f"({item['reason']})")
    lines.append("\n## 时间线（最近 20 条）")
    for row in report["events"][-20:]:
        lines.append(f"- {str(row.get('ts', ''))[:19]} "
                     f"{row.get('event')} "
                     f"{str(row.get('module') or '')} "
                     f"{str(row.get('direction') or '')[:60]} "
                     f"{row.get('status') or ''}")
    return "\n".join(lines)


def _cmd_track(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    report = track_evolution(root)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    text = format_track(report)
    if args.out:
        target = root / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        log.info("evolution track: %s", target)
    else:
        print(text)
    return 0


def require_evolution_metadata(rule_id: str, draft: dict) -> None:
    """evolution 域 promote 强制元数据：归纳必须声明出处与强度。

    规律是记忆不是宪法——没有 samples（出处）与 confidence（强度）的
    规律不允许进入注册表（fail closed）。learn.promote 调用。"""
    samples = draft.get("samples") or []
    if not isinstance(samples, list) or not samples or not all(
            isinstance(s, str) and s.strip() for s in samples):
        raise ValueError(
            f"evolution draft '{rule_id}' 缺 samples（归纳出处）——"
            "规律来自哪个实战项目/事故？请在草案填写 samples 后再提升")
    confidence = draft.get("confidence", 0.5)
    if (not isinstance(confidence, (int, float))
            or isinstance(confidence, bool) or not 0 <= confidence <= 1):
        raise ValueError(
            f"evolution draft '{rule_id}' confidence 必须是 0-1 数值"
            "（归纳强度；<0.6 默认只以 suggestion 形态生效）")


def attach_constitution(assessment: dict) -> None:
    """项目宪法摘要（L6/L9）：评估者必须看到项目边界。

    明确不做清单是方向红线；变更预算约束变更半径；豁免列表说明哪些
    规律已被项目显式降级（不重复建议）。无宪法 → 不附加（不臆造）。
    """
    constitution = load_constitution(Path.cwd())
    if not constitution:
        return
    assessment["constitution"] = {
        "non_goals": [g for g in (constitution.get("explicit_non_goals") or [])
                      if isinstance(g, str) and g.strip()][:5],
        "budget": {k: v for k, v in (constitution.get("change_budget") or {}).items()
                    if v},
        "overrides": sorted((constitution.get("overrides") or {}).keys()),
    }


def format_constitution_block(assessment: dict) -> str:
    """宪法摘要块（--json 时在 assessment['constitution']）。"""
    constitution = assessment.get("constitution")
    if not constitution:
        return ""
    lines = ["## 项目宪法（docs/evolution/constitution.yaml）"]
    goals = constitution.get("non_goals") or []
    if goals:
        lines.append(f"明确不做（红线）: {'; '.join(goals)}")
    budget = constitution.get("budget") or {}
    if budget:
        lines.append("变更预算: " + "; ".join(
            f"{k}={v}" for k, v in budget.items()))
    overrides = constitution.get("overrides") or []
    if overrides:
        lines.append(f"规律豁免: {', '.join(overrides)}")
    return "\n".join(lines)


def _constitution_boilerplate() -> str:
    return (
        "# 项目宪法（由 pi-batch evolution init 生成，按项目实况编辑）\n"
        "product_position:  \n"
        "explicit_non_goals:\n"
        "  - 未经维护者授权不执行发布/部署动作\n"
        "  - 不发明服务端/核心层不存在的接口\n"
        "security_invariants:\n"
        "  - 秘密最小驻留\n"
        "  - 写操作不自动重放\n"
        "change_budget:\n"
        "  max_files: 5\n"
        "  max_lines: 300\n"
        "  allowlist: []\n"
        "  denylist: []\n"
        "overrides: {}\n"
    )


def _init_constitution(root: Path) -> Path:
    target = root / CONSTITUTION_REL
    if target.is_file():
        raise ValueError(f"constitution already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_constitution_boilerplate(), encoding="utf-8")
    return target


def _append_state(root: Path, event: dict) -> None:
    state = _runtime_state_path(root)
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            {"ts": datetime.now(timezone.utc).isoformat(), **event},
            ensure_ascii=False) + "\n")


def _cmd_init(args: argparse.Namespace) -> int:
    try:
        target = _init_constitution(Path(args.dir).resolve())
    except ValueError as exc:
        log.error("evolution init: %s", exc)
        return 1
    _append_state(Path(args.dir).resolve(), {"event": "init", "target": str(target)})
    log.info("evolution init: %s (edit it to match the project)", target)
    return 0


def _cmd_override(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    constitution = load_constitution(root)
    overrides = dict(constitution.get("overrides") or {})
    overrides[args.rule] = {
        "reason": args.reason,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    constitution["overrides"] = overrides
    target = root / CONSTITUTION_REL
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(constitution, allow_unicode=True,
                                     sort_keys=False), encoding="utf-8")
    _append_state(root, {"event": "override", "rule": args.rule,
                         "reason": args.reason})
    log.info("evolution override: rule '%s' downgraded to suggestion "
             "(reason recorded, %s)", args.rule, target)
    return 0


def _cmd_check(args: argparse.Namespace) -> int:
    """只读宪法门禁：基线（L1）+ 明确不做（L6）+ 变更预算（L9）。

    authority/evidence 是产物级门禁，作为 validator 在流水线内执行
    （pi-batch.yaml validators 注册），此处不重复。
    """
    import subprocess
    import tempfile
    root = Path(args.dir).resolve()
    script = Path(config.TOOL_ROOT) / "scripts"
    checks = [
        ("baseline", [sys.executable, str(script / "check-baseline.py"),
                      str(root), "--json"]),
        ("changebudget", [sys.executable, str(script / "check-changebudget.py"),
                          "--dir", str(root), "--json"]),
    ]
    artifact = ""
    if args.task_text:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(args.task_text)
            artifact = handle.name
        checks.insert(1, ("nongoal", [
            sys.executable, str(script / "check-nongoal.py"), artifact,
            "--dir", str(root), "--json"]))
    else:
        print("[SKIP] nongoal: no --task-text artifact to compare")
    try:
        failed = False
        for name, cmd in checks:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            status = "PASS" if proc.returncode == 0 else "FAIL"
            print(f"[{status}] {name}: {proc.stdout.strip()}")
            if proc.stderr.strip():
                print(f"  {proc.stderr.strip()}")
            if proc.returncode != 0:
                failed = True
    finally:
        if artifact:
            Path(artifact).unlink(missing_ok=True)
    _append_state(root, {"event": "check", "failed": failed})
    return 1 if failed else 0


def _report_lines(registry: dict, overrides: dict) -> list:
    """规律清单行（含 invariant / override / low-confidence 标记）。"""
    lines = []
    for rule_id, rule in (registry.get("rules") or {}).items():
        level = rule.get("level", "suggestion")
        samples = rule.get("samples") or []
        confidence = float(rule.get("confidence", 0.5))
        marker = ""
        if rule_id in overrides:
            marker = f" [OVERRIDDEN: {overrides[rule_id].get('reason', '')}]"
        elif level == "invariant":
            marker = " [invariant]"
        elif confidence < 0.6:
            marker = " [low-confidence → advisory only]"
        lines.append(f"- {rule_id} (level={level}, confidence={confidence}, "
                     f"samples={len(samples)}: {', '.join(samples) or '-'}){marker}")
    return lines


def _self_review_lines(registry: dict, overrides: dict) -> list:
    """self-review：被豁免/低置信度规律 + 可挑战提示。"""
    lines = ["", "self-review（规律自身可被挑战）："]
    challenged = sorted(overrides)
    if challenged:
        lines.append(f"- 被豁免规律 {len(challenged)} 条——豁免期满后复查："
                     f"{', '.join(challenged)}")
    low = sorted(rid for rid, rule in (registry.get("rules") or {}).items()
                 if float(rule.get("confidence", 0.5)) < 0.6)
    if low:
        lines.append(f"- 低置信度规律 {len(low)} 条（默认只建议不拦截）："
                     f"{', '.join(low)}")
    lines.append("- 检查规律命中率是否下降、规律外好方案是否被拒过"
                 "（样本失效 → learn deprecate）")
    return lines


def _cmd_report(args: argparse.Namespace) -> int:
    """规律命中/豁免/stale 报告（从注册表 + 宪法 + runtime state.jsonl）。
    --self-review 列出可挑战项（规律自身可被挑战，restraint.md §5）。"""
    root = Path(args.dir).resolve()
    constitution = load_constitution(root)
    registry = load_registry(domain="evolution")
    overrides = (constitution.get("overrides") or {})
    lines = [f"# Evolution report — {root}", ""]
    lines.extend(_report_lines(registry, overrides))
    if overrides:
        lines.append("")
        lines.append("overrides（豁免留痕，90 天复查）：")
        for rule_id, record in overrides.items():
            lines.append(f"- {rule_id}: {record.get('reason', '')} "
                         f"({record.get('ts', '')})")
    if args.self_review:
        lines.extend(_self_review_lines(registry, overrides))
    states = _state_paths(root)
    if states:
        events = []
        for state in states:
            events.extend(_collect_events(state, 2000))
        events.sort(key=lambda row: str(row.get("ts") or ""))
        events = events[-2000:]
        lines.append(f"\nstate events: {len(events)} ({_state_read_path(root)})")
        if args.self_review:
            overrides_events = [e for e in events if e.get("event") == "override"]
            lines.append(f"override 事件: {len(overrides_events)}")
    print("\n".join(lines))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pi-batch.py evolution",
        description="Evolution patterns: route battle-tested laws by project "
                    "shape, read/override the project constitution.")
    sub = parser.add_subparsers(dest="command")
    p_apply = sub.add_parser("apply", help="规律适用清单（形态×反例信号×宪法）")
    p_apply.add_argument("task", help="任务文本/方向描述")
    p_apply.add_argument("--dir", default=".", help="project root (constitution)")
    p_apply.add_argument("--json", action="store_true")
    sub.add_parser("init", help="生成项目宪法骨架 (docs/evolution/constitution.yaml)") \
        .add_argument("--dir", default=".")
    p_check = sub.add_parser("check", help="项目宪法门禁（baseline/nongoal/changebudget）")
    p_check.add_argument("--dir", default=".")
    p_check.add_argument("--task-text", default="", help="额外检查一个方向文本")
    p_route = sub.add_parser("route", help="L8 管线形状路由（风险×产品化×预算 → 模板）")
    p_route.add_argument("task", help="任务文本/方向描述")
    p_route.add_argument("--dir", default=".", help="project root (constitution budget)")
    p_route.add_argument("--json", action="store_true")
    p_override = sub.add_parser("override", help="宪法豁免一条规律（留痕）")
    p_override.add_argument("--rule", required=True)
    p_override.add_argument("--reason", required=True)
    p_override.add_argument("--dir", default=".")
    p_report = sub.add_parser("report", help="规律命中/豁免/stale 报告")
    p_report.add_argument("--dir", default=".")
    p_report.add_argument("--self-review", action="store_true",
                          help="规律自身可被挑战：豁免/低置信度/stale 清单")
    p_track = sub.add_parser("track", help="演化路径分析（state.jsonl 事件时间线）")
    p_track.add_argument("--dir", default=".")
    p_track.add_argument("--out", default="", help="写 TRACK.md 到 DIR/<out>")
    p_track.add_argument("--json", action="store_true")
    return parser


def evolution_main(argv: list) -> None:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "apply":
        registry = load_registry(domain="evolution")
        constitution = load_constitution(Path(args.dir).resolve())
        manifest = applicable_rules(args.task, registry, constitution)
        if args.json:
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return
        print(f"shape: {manifest['shape']}")
        print("rules:")
        for item in manifest["rules"]:
            print(f"  - {item['id']} [{item['level']}]"
                  + (f" ({item.get('reason')})" if item.get("reason") else ""))
        for item in manifest["downgraded"]:
            print(f"  downgraded: {item['id']} {item['level']} → {item['to']} "
                  f"— {item['reason']}")
        for item in manifest["skipped"]:
            print(f"  skipped: {item['id']} — {item['reason']}")
        return
    if args.command == "route":
        constitution = load_constitution(Path(args.dir).resolve())
        route = route_pipeline(args.task, constitution)
        if args.json:
            print(json.dumps(route, ensure_ascii=False, indent=2))
            return
        print(f"route: {route['route']}")
        print(f"pipeline: {route['pipeline'] or '(无模板——见 label)'}")
        print(f"label: {route['label']}")
        for reason in route["reasons"]:
            print(f"  - {reason}")
        if route["gates"]:
            print(f"gates: {', '.join(route['gates'])}")
        return
    exit_code = {"init": _cmd_init, "check": _cmd_check,
                 "override": _cmd_override, "report": _cmd_report,
                 "track": _cmd_track}[args.command](args)
    if exit_code:
        sys.exit(exit_code)
