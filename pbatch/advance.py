"""Iterative project scanner and repair planner: `pi-batch advance`.

Scans a project across every spec dimension (UI spacing/colors/styles,
frontend engineering errors/complexity, backend engineering/architecture),
groups the findings into fine-grained per-dimension batches, prioritizes
them (P0 recoverability/security > P1 visual tokens > P2 architecture),
and records the iteration state so progress can resume round after round.

Pipeline per round:
  1. SCAN: run all registered checkers (--json) over the target dir
  2. GROUP: classify every violation into a dimension
  3. PRIORITIZE: P0 -> P1 -> P2 batches, each with samples + suggested fix
  4. RECORD: append to docs/advance/state.jsonl (per-round, per-dimension)
  5. LOOP: rescan until every dimension is clean or --max-rounds reached

Usage:
    pi-batch advance --dir ../snaplink-console          # plan (no changes)
    pi-batch advance --dir ../snaplink-console --json   # machine plan
    pi-batch advance --dir . --max-rounds 3             # round bookkeeping
    pi-batch advance --help

The engine produces the iteration PLAN (what to fix, in what order, per
dimension); it never edits the target project.  Deterministic fixes and agent
implementation reuse the rest of the toolchain (validators, assess, pipelines).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .config import log

STATE_DIR = Path("docs/advance")
STATE_FILE = STATE_DIR / "state.jsonl"

# Dimension registry: which checker output maps to which spec dimension.
DIMENSIONS = [
    {"id": "ui_spacing", "checker": "check-ui-spec.py", "mode": "spacing",
     "priority": 1, "label": "魔法间距（非 8pt token）",
     "fix": "吸附到最近 token（scripts/check-ui-spec.py --mode spacing 逐文件核对）"},
    {"id": "ui_color", "checker": "check-ui-spec.py", "mode": "color",
     "priority": 1, "label": "硬编码颜色",
     "fix": "提取为主题 token 类（AppColors / tokens），语义映射替换"},
    {"id": "ui_style", "checker": "check-ui-spec.py", "mode": "style",
     "priority": 1, "label": "inline style",
     "fix": "改用平台样式机制（styled/token/class）"},
    {"id": "fe_errors", "checker": "check-frontend-quality.py", "mode": "error",
     "priority": 0, "label": "前端工程错误（吞异常/不安全 html/测试跳过）",
     "fix": "吞异常加日志；不安全 html 改安全渲染；去掉测试 skip"},
    {"id": "fe_n1", "checker": "check-frontend-quality.py", "mode": "n1",
     "priority": 0, "label": "循环 await（N+1）",
     "fix": "Future.wait/Promise.all 并行化或批量接口"},
    {"id": "fe_complexity", "checker": "check-frontend-quality.py", "mode": "strict",
     "priority": 2, "label": "上帝文件/复杂度/嵌套",
     "fix": "按业务能力拆组件/提 helper（克制：内聚的 API 客户端保留）"},
    {"id": "be_errors", "checker": "check-backend-quality.py", "mode": "error",
     "priority": 0, "label": "后端工程错误（吞异常/危险 DDL/依赖方向）",
     "fix": "加日志/迁移文件/修正依赖方向"},
    {"id": "be_architecture", "checker": "check-backend-quality.py", "mode": "strict",
     "priority": 2, "label": "上帝服务/单实现接口/复杂度",
     "fix": "组合优于继承、去无意义抽象（design-patterns 决策表）"},
    {"id": "knowledge", "checker": "check-knowledge-freshness.py", "mode": "doc",
     "priority": 2, "label": "知识维护（模块文档/ADR/关键字段审计）",
     "fix": "补 README/ADR（docs 决策记录）；关键金额字段加审计信号"
            "（maintenance-intelligence 六层维护第 6 层）"},
    {"id": "design_intelligence", "checker": "check-design-intelligence.py", "mode": "kpi",
     "priority": 1, "label": "设计智能（KPI 强调/空状态教学/状态双编码）",
     "fix": "按 design-intelligence 规范：大数字强调样式、空状态带动作、"
            "状态色+图标双编码"},
    {"id": "backend_experience", "checker": "check-backend-experience.py", "mode": "state",
     "priority": 2, "label": "业务闭环（状态机/审计/异步生命周期/聚合上下文）",
     "fix": "状态集中为枚举/常量集；写操作带审计；长任务异步化；搜索端点"
            "返回聚合上下文（design-intelligence 后端 01/02）"},
]

_PRIORITY_LABEL = {0: "P0", 1: "P1", 2: "P2"}


class CheckerError(RuntimeError):
    """A registered checker could not produce a trustworthy JSON report."""


def _checker_script(name: str) -> str:
    """Locate a checker script (repo scripts/ or tools/ai-dev-gates/)."""
    candidates = [
        Path("scripts") / name,
        Path("tools/ai-dev-gates") / name,
        Path(config.TOOL_ROOT) / "scripts" / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(candidates[0])


def _checker_args(script: str, target: str) -> list[str]:
    """Build the supported CLI for one registered checker."""
    args = [sys.executable, script, "--dir", target]
    name = Path(script).name
    if name == "check-ui-spec.py":
        args.append("--all")
    elif name in {"check-frontend-quality.py", "check-backend-quality.py"}:
        args.append("--strict")
    args.append("--json")
    return args


def _validate_report(script: str, payload: object) -> dict:
    """Validate the common checker report shape before consuming it."""
    if not isinstance(payload, dict):
        raise CheckerError(f"{script}: JSON report must be an object")
    violations = payload.get("violations")
    files_scanned = payload.get("files_scanned")
    if not isinstance(violations, list) or not all(
            isinstance(item, dict) for item in violations):
        raise CheckerError(f"{script}: JSON report has invalid violations")
    if files_scanned is None:
        payload["files_scanned"] = 0
    elif not isinstance(files_scanned, int) or files_scanned < 0:
        raise CheckerError(f"{script}: JSON report has invalid files_scanned")
    return payload


def _run_checker(script: str, target: str) -> dict:
    """Run one checker and fail closed if its report cannot be trusted."""
    if not Path(script).is_file():
        raise CheckerError(f"checker not found: {script}")
    try:
        result = subprocess.run(
            _checker_args(script, target), capture_output=True, text=True,
            timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CheckerError(f"{script}: checker execution failed: {exc}") from exc
    if result.returncode not in (0, 1):
        detail = (result.stderr or result.stdout).strip()[:500]
        raise CheckerError(
            f"{script}: checker exited {result.returncode}: {detail}")
    try:
        payload, _ = json.JSONDecoder().raw_decode(result.stdout.lstrip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise CheckerError(f"{script}: checker emitted invalid JSON") from exc
    return _validate_report(script, payload)


# detail 关键词 → 维度（数据驱动，避免大 if 链）。
_DETAIL_DIMENSIONS: list = [
    ("fe_errors", ["swallowed exception", "unsafe innerHTML", ".skip/.only"]),
    ("be_errors", ["without assertions", "DDL", "dependency direction",
                   "status assignment", "swallow"]),
    ("fe_n1", ["N+1", "loop body"]),
    ("fe_complexity", ["nesting depth", "god-file", "decision points",
                       "state hooks", "event handlers", "api calls"]),
    ("be_architecture", ["constructor deps", "public methods",
                         "single-implementation", "Base class"]),
    ("ui_spacing", ["spacing"]),
    ("ui_color", ["color"]),
    ("ui_style", ["style"]),
    ("knowledge", ["module_without_docs", "no_decision_records",
                   "critical_number_without_audit"]),
    ("design_intelligence", ["semantic_color_literal", "kpi_emphasis_missing",
                             "empty_state_no_action", "status_not_dual_encoded"]),
    ("backend_experience", ["status_literals_without_state_set",
                            "write_without_audit_signal",
                            "long_op_without_async_lifecycle",
                            "search_endpoint_without_context"]),
]


_CHECKER_FALLBACK = {
    "check-ui-spec.py": "ui_style",
    "check-frontend-quality.py": "fe_complexity",
    "check-backend-quality.py": "be_architecture",
    "check-knowledge-freshness.py": "knowledge",
    "check-design-intelligence.py": "design_intelligence",
    "check-backend-experience.py": "backend_experience",
}

_CHECKER_DIMENSIONS = {
    name: {dim["id"] for dim in DIMENSIONS if dim["checker"] == name}
    for name in _CHECKER_FALLBACK
}


def classify_violation(detail: str, checker: str = "") -> str:
    """Map one violation to a dimension, constrained by its source checker."""
    fallback = _CHECKER_FALLBACK.get(Path(checker).name) if checker else None
    for dimension, markers in _DETAIL_DIMENSIONS:
        if any(marker in detail for marker in markers):
            if not checker or dimension in _CHECKER_DIMENSIONS.get(Path(checker).name, set()):
                return dimension
    return fallback or "be_architecture"


def _registered_checkers() -> list[str]:
    """Return checker names once, preserving dimension declaration order."""
    return list(dict.fromkeys(dim["checker"] for dim in DIMENSIONS))


def scan_project(target: str) -> dict:
    """Run all registered checkers; returns per-dimension findings."""
    reports = {
        checker: _run_checker(_checker_script(checker), target)
        for checker in _registered_checkers()
    }
    dimensions = {}
    for dim in DIMENSIONS:
        dimensions[dim["id"]] = {"label": dim["label"], "priority": dim["priority"],
                                 "fix": dim["fix"], "count": 0, "files": set(), "samples": []}
    for checker, report in reports.items():
        for violation in report.get("violations", []):
            detail = str(violation.get("detail", ""))
            dim_id = classify_violation(detail, checker)
            dim = dimensions.get(dim_id)
            if dim is None:
                continue
            dim["count"] += 1
            file_name = str(violation.get("file", ""))
            if file_name:
                dim["files"].add(file_name)
            if len(dim["samples"]) < 3:
                dim["samples"].append(detail[:90])
    for dim in dimensions.values():
        dim["files"] = sorted(dim["files"])
    checker_files = {
        checker: report["files_scanned"] for checker, report in reports.items()
    }
    return {"target": target, "files_scanned": max(checker_files.values(), default=0),
            "checker_files_scanned": checker_files, "dimensions": dimensions}


def build_batches(scan: dict) -> list:
    """P0 -> P1 -> P2 batches: one batch per dimension with findings."""
    return [
        {"priority": _PRIORITY_LABEL[d["priority"]], "id": dim_id,
         "count": d["count"], "label": d["label"], "fix": d["fix"],
         "files": d["files"][:8], "samples": d["samples"]}
        for dim_id, d in sorted(scan["dimensions"].items(),
                                key=lambda kv: (kv[1]["priority"], -kv[1]["count"]))
        if d["count"] > 0
    ]


def record_round(scan: dict, round_no: int) -> None:
    """Append one round's per-dimension state (resumable, append-only)."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_file = STATE_DIR / "state.jsonl"
    entry = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "round": round_no,
        "target": scan["target"],
        "total": sum(d["count"] for d in scan["dimensions"].values()),
        "dimensions": {dim_id: d["count"] for dim_id, d in scan["dimensions"].items()},
    }
    with state_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _density_bar(count: int, total: int, width: int = 12) -> str:
    """剩余占比条形（数据表达：健康度式，一眼看懂各维度占比）。"""
    if total <= 0:
        return ""
    filled = max(1, round(count / total * width))
    return "█" * filled + "░" * (width - filled)


def format_plan(batches: list, total: int) -> str:
    """Human-readable iteration plan (per-dimension batches, prioritized)."""
    lines = [f"## 迭代计划（共 {total} 项，按维度分批）"]
    if not batches:
        lines.append("全部维度干净——无需推进。")
        return "\n".join(lines)
    for batch in batches:
        lines.append(f"\n[{batch['priority']}] {batch['label']}（{batch['count']} 项）"
                     f" {_density_bar(batch['count'], total)}")
        lines.append(f"  建议: {batch['fix']}")
        for sample in batch["samples"]:
            lines.append(f"  示例: {sample}")
        if batch["files"]:
            lines.append("  文件: " + ", ".join(batch["files"][:5]) +
                         ("…" if len(batch["files"]) > 5 else ""))
    return "\n".join(lines)


def _total_findings(scan: dict) -> int:
    """Count all findings in a scan."""
    return sum(dimension["count"] for dimension in scan["dimensions"].values())


def _run_recorded_rounds(target: str, first_round: int,
                         max_rounds: int) -> tuple[dict, int]:
    """Rescan and record each requested round, stopping once clean."""
    scan, rounds_run = {}, 0
    for round_no in range(first_round, first_round + max_rounds):
        scan = scan_project(target)
        record_round(scan, round_no)
        rounds_run += 1
        if _total_findings(scan) == 0:
            break
    return scan, rounds_run


def _advance_scan(args) -> tuple[dict, int]:
    """Run either one planning scan or the requested recorded rescans."""
    if args.max_rounds == 0:
        return scan_project(args.dir), 0
    return _run_recorded_rounds(args.dir, args.round, args.max_rounds)


def advance_main(argv: list) -> int:
    """`pi-batch advance [--dir PATH] [--json] [--max-rounds N] [--round R]`."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="pi-batch.py advance",
        description="Self-driving iteration engine: scan every spec dimension, "
                    "group into prioritized batches, record round state.")
    parser.add_argument("--dir", default=".", help="target project directory")
    parser.add_argument("--json", action="store_true", help="machine-readable plan")
    parser.add_argument("--max-rounds", type=int, default=0,
                        help="record round bookkeeping (0 = scan only)")
    parser.add_argument("--round", type=int, default=1, help="round number")
    args = parser.parse_args(argv)
    if args.max_rounds < 0 or args.round < 1:
        parser.error("--max-rounds must be >= 0 and --round must be >= 1")
    try:
        scan, rounds_run = _advance_scan(args)
    except CheckerError as exc:
        log.error("advance: %s", exc)
        if args.json:
            print(json.dumps({"target": args.dir, "error": str(exc)},
                             ensure_ascii=False))
        return 2
    batches = build_batches(scan)
    if rounds_run:
        log.info("advance: recorded %d round(s) in %s", rounds_run, STATE_FILE)
    if args.json:
        print(json.dumps({"target": scan["target"],
                          "files_scanned": scan["files_scanned"],
                          "rounds_run": rounds_run,
                          "total": _total_findings(scan),
                          "batches": batches}, ensure_ascii=False, indent=2))
        return 0
    print(format_plan(batches, _total_findings(scan)))
    return 0
