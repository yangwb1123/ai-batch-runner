"""Evaluation Engineering: a regression suite for the rule system itself.

`pi-batch eval` runs the evals/*.yaml cases against the live classifier /
rule matcher / assessor and fails when a keyword or registry change broke
behavior — the "Evals 缺失，规范调整后无法知道能力是提升还是退化" gap.

Eval case schema:

    name: classifier
    cases:
      - id: c1
        input: "用 Flutter 实现 ERP 排程列表页"
        assert:
          task_type: frontend_ui     # classify_text
          platform: dart
          tier: standard             # match_rules (from scale keywords)
          has_rule: [visual-core]    # rule ids present
          lacks_rule: [async-data]   # rule ids absent
          product_level: L0_local_feature   # product_manifest
          workflow_level: L1_standard       # assessor workflow_level

Assertion keys are dispatched to the matching analyzer; list values mean
"all of these must hold". Exit code 1 on any failure (CI-friendly).
The equivalent `name` + `expect` spelling is accepted; `includes`/`excludes`
normalize to `has_rule`/`lacks_rule` before evaluation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import config
from .assessor import workflow_level
from .classifier import classify_text
from .config import log, yaml
from .product import product_manifest
from .rule_matcher import match_rules
from .text_io import read_text_bounded

EVAL_DIR = Path(config.TOOL_ROOT) / "evals"

_ASSERTION_KEYS = frozenset({
    "task_type", "platform", "profile", "confident", "system_type",
    "tier", "risk", "page_types", "has_rule", "lacks_rule",
    "product_level", "workflow_level", "scale",
})
_CASE_KEYS = frozenset({"id", "name", "input", "assert", "expect"})
_EXPECT_ALIASES = {"includes": "has_rule", "excludes": "lacks_rule"}


def _case_identity(case, index: int = 0) -> str:
    if not isinstance(case, dict):
        return f"case-{index + 1}"
    return str(case.get("id") or case.get("name") or f"case-{index + 1}")


def _normalize_case(case, index: int = 0) -> dict:
    """Normalize both supported case spellings and reject ambiguous schema."""
    if not isinstance(case, dict):
        raise ValueError("case must be a mapping")
    unknown = sorted(set(case) - _CASE_KEYS)
    if unknown:
        raise ValueError("unknown case field(s): " + ", ".join(unknown))
    if "id" in case and "name" in case:
        raise ValueError("use either 'id' or 'name', not both")
    case_id = str(case.get("id") or case.get("name") or "").strip()
    if not case_id:
        raise ValueError("case needs a non-empty 'id' or 'name'")
    if not isinstance(case.get("input"), str) or not case["input"].strip():
        raise ValueError(f"case '{case_id}' needs non-empty string 'input'")
    assertions = _normalize_assertions(case, case_id)
    return {"id": case_id, "input": case["input"], "assert": assertions}


def _normalize_assertions(case: dict, case_id: str) -> dict:
    if "assert" in case and "expect" in case:
        raise ValueError(f"case '{case_id}' cannot contain both 'assert' and 'expect'")
    source = case.get("assert") if "assert" in case else case.get("expect")
    if not isinstance(source, dict) or not source:
        raise ValueError(f"case '{case_id}' needs at least one assertion")
    assertions = {}
    for raw_key, expected in source.items():
        key = _EXPECT_ALIASES.get(str(raw_key), str(raw_key))
        if key not in _ASSERTION_KEYS:
            raise ValueError(f"case '{case_id}' has unknown assertion '{raw_key}'")
        if key in assertions:
            raise ValueError(f"case '{case_id}' repeats assertion '{key}'")
        if isinstance(expected, list) and not expected:
            raise ValueError(f"case '{case_id}' assertion '{raw_key}' cannot be empty")
        assertions[key] = expected
    return assertions


def _normalize_suite(data: dict, path: Path) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("needs a 'cases' list")
    unknown = sorted(set(data) - {"name", "cases"})
    if unknown:
        raise ValueError("unknown suite field(s): " + ", ".join(unknown))
    name = data.get("name", path.stem)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("suite needs a non-empty string 'name'")
    cases = [_normalize_case(case, index) for index, case in enumerate(data["cases"])]
    if not cases:
        raise ValueError("needs at least one case")
    ids = [case["id"] for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise ValueError("duplicate case id(s): " + ", ".join(duplicates))
    return {"path": path, "name": name, "cases": cases}


def load_eval_files() -> list:
    """All evals/*.yaml suites; malformed files fail loudly (fail closed)."""
    suites = []
    if not yaml or not EVAL_DIR.is_dir():
        log.error("No evals/ directory — create evals/*.yaml case files")
        sys.exit(2)
    for path in sorted(EVAL_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(read_text_bounded(
                path, config.INPUT_MAX_BYTES, "eval file")) or {}
            suites.append(_normalize_suite(data, path))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.error("Invalid eval suite %s: %s", path, exc)
            sys.exit(2)
    if not suites:
        log.error("No eval suites found in %s", EVAL_DIR)
        sys.exit(2)
    return suites


def _assert_value(key: str, input_text: str) -> object:
    """Dispatch one assertion key to the live analyzer."""
    if key in ("task_type", "platform", "profile", "confident", "system_type"):
        cls = classify_text(input_text)
        return {"task_type": cls.task_type, "platform": cls.platform,
                "profile": cls.profile, "confident": cls.confident,
                "system_type": cls.system_type}[key]
    if key in ("tier", "risk", "page_types"):
        matched = match_rules(input_text)
        return {"tier": matched["tier"], "risk": matched["risk"],
                "page_types": matched["page_types"]}[key]
    if key in ("has_rule", "lacks_rule"):
        return {item["id"] for item in match_rules(input_text)["rules"]}
    if key == "product_level":
        return product_manifest(input_text)["level"]
    if key == "workflow_level":
        return workflow_level(input_text)["level"]
    if key == "scale":
        from .assessor import scale_signal
        return scale_signal(input_text)
    raise ValueError(f"unknown eval assertion key: {key}")


def run_suite(suite: dict, only_id: str = "") -> list:
    """[(case_id, ok, detail)] for one suite."""
    results = []
    for index, raw_case in enumerate(suite.get("cases", [])):
        case_id = _case_identity(raw_case, index)
        if only_id and case_id != only_id:
            continue
        try:
            case = _normalize_case(raw_case, index)
        except ValueError as exc:
            results.append((case_id, False, f"schema: {exc}"))
            continue
        failures = [failure for key, expected in case["assert"].items()
                    if (failure := _assertion_failure(key, expected, case["input"]))]
        results.append((case_id, not failures, "; ".join(failures) if failures else ""))
    return results


def _assertion_failure(key: str, expected, input_text: str) -> str:
    try:
        actual = _assert_value(key, input_text)
    except ValueError as exc:
        return f"{key}: {exc}"
    expected_list = expected if isinstance(expected, list) else [expected]
    if key == "has_rule":
        missing = [item for item in expected_list if item not in actual]
        return f"{key} missing {missing} (got {sorted(actual)})" if missing else ""
    if key == "lacks_rule":
        present = [item for item in expected_list if item in actual]
        return f"{key} present {present} (got {sorted(actual)})" if present else ""
    if isinstance(actual, (list, set, tuple)):
        missing = [item for item in expected_list if item not in actual]
        return f"{key} missing {missing} (got {sorted(actual)})" if missing else ""
    if actual not in expected_list:
        return f"{key}: expected {expected_list}, got {actual!r}"
    return ""


def _count_cases(suite: dict) -> int:
    """Case count for a suite."""
    cases = suite.get("cases")
    if isinstance(cases, list):
        return len(cases)
    return 0


def _suite_domains(suite: dict) -> list:
    """Domains exercised by a normalized suite's assertion fields."""
    domains = []
    cases = suite.get("cases")
    if not isinstance(cases, list):
        return []
    for case in cases:
        if not isinstance(case, dict):
            continue
        assertions = case.get("assert", {})
        for key in ("task_type", "system_type", "tier", "product_level",
                    "workflow_level", "scale"):
            value = assertions.get(key) if isinstance(assertions, dict) else None
            values = value if isinstance(value, list) else [value]
            for item in values:
                if item and str(item) not in domains:
                    domains.append(str(item))
    return domains[:5]


def _coverage(suites: list) -> list:
    return [{"suite": suite.get("name", "?"),
             "cases": _count_cases(suite),
             "domains": _suite_domains(suite)} for suite in suites]


def _print_coverage(total: int, coverage: list) -> None:
    """Per-suite case counts + domain coverage (data-expression)."""
    print("## Eval 域覆盖")
    for item in coverage:
        print(f"- {item['suite']}: {item['cases']} 用例"
              f" (域: {', '.join(item['domains']) or 'n/a'})")
    print(f"总计 {total} 用例 / {len(coverage)} 套件")


def eval_main(argv: list) -> None:
    """`pi-batch eval [--filter CASE_ID] [--json]` — run the rule-system
    regression suite."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="pi-batch.py eval",
        description="Run the rule-system regression suite (evals/*.yaml).")
    parser.add_argument("--filter", default="", help="run only this case id")
    parser.add_argument(
        "--quick", action="store_true",
        help="run only core regression cases (rules + classifier domains)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--coverage", action="store_true",
        help="print per-suite case counts and domain coverage")
    args = parser.parse_args(argv)
    total, failed = 0, []
    suites = load_eval_files()
    active_suites = [suite for suite in suites
                     if not args.quick or suite.get("name") in {"rules", "classifier"}]
    for suite in active_suites:
        for case_id, ok, detail in run_suite(suite, args.filter):
            total += 1
            status = "PASS" if ok else "FAIL"
            log.info("EVAL %s/%s: %s", suite["name"], case_id, status)
            if not ok:
                failed.append(f"{suite['name']}/{case_id}: {detail}")
                log.error("EVAL %s/%s FAIL: %s", suite["name"], case_id, detail)
    if args.filter and total == 0:
        failed.append(f"filter {args.filter!r} matched no eval cases")
        log.error("EVAL: --filter %r matched no cases", args.filter)
    coverage = _coverage(active_suites) if args.coverage else []
    if args.json:
        payload = {"total": total, "failed": len(failed), "failures": failed}
        if args.coverage:
            payload["coverage"] = coverage
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.coverage:
        _print_coverage(total, coverage)
    if failed:
        log.error("EVAL: %d/%d failed", len(failed), total)
        sys.exit(1)
    log.info("EVAL: all %d passed", total)
