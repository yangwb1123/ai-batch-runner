"""`pi-batch health` — 六层维护可观测性报告（运营智能落地）。

对项目做一次"系统健康体检"（纯 stdlib、只读、无副作用）：

- 测试健康：测试数（pytest 收集计数，不执行）
- 门禁健康：检查器自扫描（designintelligence/knowledge/backend-quality）
- 规范资产：ui-specs/backend-specs 文件数、规则数
- 知识维护：模块文档覆盖（knowledge 检查器）
- 技术债：quality 违规数
- 维护记录：docs/rules/drafts 草案数、docs/advance/state.jsonl 轮数

输出按信息优先级：核心（FAIL/0 违规）→ 概览 → 详细。
`--json` 机器可读。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = ".git,.dart_tool,test,build,vendor,checks"


def _count_tests() -> int:
    """Count test functions without running them (stdlib scan)."""
    total = 0
    for path in (ROOT / "tests").rglob("test_*.py"):
        if ".dart_tool" in path.parts:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total += len(re.findall(r"^def test_|^    def test_", source, re.M))
    return total


def _checker_violations(name: str) -> int:
    script = ROOT / "scripts" / f"{name}.py"
    if not script.exists():
        return -1
    cmd = [sys.executable, str(script), "--dir", str(ROOT), "--json"]
    # check-backend-quality 无 --skip（内置 DEFAULT_EXCLUDES）；其余接受逗号参数。
    if name != "check-backend-quality":
        cmd += ["--skip", SKIP]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode not in (0, 1):
        return -1
    try:
        start = result.stdout.index("{")
        # stdout 可能带尾行（如 "BACKEND-QUALITY: OK"）——只解析首个 JSON 对象。
        decoded, _ = json.JSONDecoder().raw_decode(result.stdout[start:])
        return decoded.get("total", -1)
    except (ValueError, json.JSONDecodeError):
        return -1


def _quality_violations() -> int:
    result = subprocess.run(
        [sys.executable, str(ROOT / "quality.py"), str(ROOT)],
        capture_output=True, text=True, timeout=120,
    )
    match = re.search(r"(\d+) violation", result.stdout)
    return int(match.group(1)) if match else 0


def _spec_counts() -> dict:
    ui = len(list((ROOT / "ui-specs").rglob("*.md")))
    be = len(list((ROOT / "backend-specs").rglob("*.md")))
    drafts = len(list((ROOT / "docs" / "rules" / "drafts").glob("*.yaml")))
    rounds = 0
    state = ROOT / "docs" / "advance" / "state.jsonl"
    if state.exists():
        try:
            rounds = sum(1 for _ in state.open(encoding="utf-8"))
        except OSError:
            rounds = 0
    return {"ui_specs": ui, "backend_specs": be, "rule_drafts": drafts,
            "advance_rounds": rounds}


def health_report(root: Path = ROOT) -> dict:
    """Gather health signals. Read-only; slow checkers capped by timeout."""
    tests = _count_tests()
    checkers = {
        "designintelligence": _checker_violations("check-design-intelligence"),
        "knowledge": _checker_violations("check-knowledge-freshness"),
        "backendquality": _checker_violations("check-backend-quality"),
    }
    quality = _quality_violations()
    specs = _spec_counts()
    return {
        "tests": tests,
        "checkers": checkers,
        "quality_violations": quality,
        "specs": specs,
        "healthy": (
            quality == 0
            and all(v == 0 for v in checkers.values())
            and tests > 0
        ),
    }


def _render(report: dict) -> str:
    """信息优先级输出：核心结论 → 概览 → 详细（3 秒理解）。"""
    lines = ["# System Health", ""]
    status = "HEALTHY" if report["healthy"] else "ATTENTION NEEDED"
    lines.append(f"## {status}")
    lines.append("")
    lines.append(f"- 测试: {report['tests']}")
    lines.append(f"- 技术债(quality): {report['quality_violations']}")
    for name, count in report["checkers"].items():
        label = "未知" if count < 0 else f"{count} 违规"
        lines.append(f"- 门禁 {name}: {label}")
    lines.append("")
    lines.append("## 规范资产")
    lines.append(f"- ui-specs: {report['specs']['ui_specs']} 篇")
    lines.append(f"- backend-specs: {report['specs']['backend_specs']} 篇")
    lines.append(f"- 规则草案: {report['specs']['rule_drafts']}")
    lines.append(f"- advance 轮数: {report['specs']['advance_rounds']}")
    return "\n".join(lines)


def health_main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pi-batch.py health",
                                     description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="machine-readable report")
    args = parser.parse_args(argv)
    report = health_report()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(_render(report))
    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(health_main())
