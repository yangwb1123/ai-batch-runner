"""Agent 工程组织规范校验器（`pi-batch org`）.

agent-engineering/*.yaml 是 17 个流程节点的"岗位说明书"（9 要素）。
`org check` 强制校验：9 要素齐全、stage 0-16 连续、id 唯一、auto_checks
引用的命令真实存在（规范可验证，不是文档）。

Usage:
    pi-batch org check [--json]
    pi-batch org show [NODE_ID]
    pi-batch org flow
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import config, org_workflow
from .config import yaml

ORG_DIR = Path("agent-engineering")
REQUIRED_KEYS = ("id", "name", "role", "stage", "parent",
                 "responsibilities", "inputs", "outputs", "check_rules",
                 "forbidden", "best_practices", "failure_cases",
                 "auto_checks", "review_checklist", "role_template")
SUPPORTED_PLACEHOLDERS = ("{task}", "{code}", "{file}", "{log}")
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def _unsupported_placeholders(value: str) -> list[str]:
    """Return unknown command placeholders in first-seen order."""
    seen = set()
    return [token for token in _PLACEHOLDER_RE.findall(str(value))
            if token not in SUPPORTED_PLACEHOLDERS and not (token in seen or seen.add(token))]


def _gate_params(task: str, code_dir: str, file_path: str,
                 log_path: str) -> dict[str, str]:
    """Build gate substitutions from the single supported-token registry."""
    return dict(zip(SUPPORTED_PLACEHOLDERS,
                    (task, code_dir, file_path, log_path)))


def _resolved_org_dir(org_dir: Optional[Path] = None) -> Path:
    """Resolve the default directory from the installed/source asset root."""
    if org_dir is not None:
        return Path(org_dir).expanduser()
    configured = Path(ORG_DIR).expanduser()
    if configured.is_absolute():
        return configured
    return Path(config.TOOL_ROOT) / configured


def _resource_roots(org_dir: Optional[Path] = None) -> list[Path]:
    """Project-local then distributed roots used by organization assets."""
    organization = _resolved_org_dir(org_dir)
    package_parent = Path(__file__).resolve().parent.parent
    roots = [Path.cwd(), organization.parent, Path(config.TOOL_ROOT),
             package_parent]
    unique = []
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def _existing_resource(value: str, org_dir: Optional[Path] = None) -> Optional[Path]:
    """Find a project-local or installed asset referenced by a node."""
    path = Path(value).expanduser()
    candidates = [path] if path.is_absolute() else [
        root / path for root in _resource_roots(org_dir)
    ]
    return next((candidate for candidate in candidates if candidate.exists()), None)


def _missing_assets_problem(org_dir: Optional[Path] = None) -> str:
    return f"organization assets missing or empty: {_resolved_org_dir(org_dir)}"


def load_nodes(org_dir: Optional[Path] = None) -> list:
    """读全部节点规范（无效文件 fail closed）。"""
    org_dir = _resolved_org_dir(org_dir)
    nodes = []
    if not org_dir.exists():
        return nodes
    for path in sorted(org_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id"):
            nodes.append(data)
    return nodes


def _known_commands(org_dir: Optional[Path] = None) -> set:
    """已知命令集合：pi-batch 子命令 + scripts/*.py + 常见工具。"""
    from .cli import _SUBCOMMANDS
    commands = set(_SUBCOMMANDS)
    for root in _resource_roots(org_dir):
        for directory in ("scripts", "pbatch"):
            for path in (root / directory).glob("*.py"):
                commands.add(f"{directory}/{path.name}")
    commands.update({"pi-batch", "pytest", "python", "bash", "go", "make",
                     "git"})
    return commands


def _check_exists(check: str, commands: set,
                  org_dir: Optional[Path] = None) -> bool:
    """auto_check 校验：pi-batch 子命令 / 脚本 / 文件目录引用。"""
    parts = str(check).split()
    if not parts:
        return False
    first = parts[0]
    if first == "pi-batch":
        return len(parts) > 1 and parts[1] in commands
    if first in commands:
        return True
    return _existing_resource(first, org_dir) is not None


def _auto_check_problem(node_id: str, check: str, commands: set,
                        org_dir: Optional[Path] = None) -> str:
    """Return one actionable static problem for an auto_check, if any."""
    unsupported = _unsupported_placeholders(check)
    if unsupported:
        return (f"{node_id}: auto_check 占位符不支持: "
                f"{', '.join(unsupported)}")
    if not _check_exists(check, commands, org_dir):
        return f"{node_id}: auto_check 命令不存在: {check}"
    return ""


def _node_schema_problems(nodes: list,
                          org_dir: Optional[Path]) -> tuple[list, dict]:
    """Validate node identity, required fields and role-template assets."""
    problems = []
    by_id = {}
    for node in nodes:
        node_id = node.get("id", "")
        if node_id in by_id:
            problems.append(f"duplicate id: {node_id}")
        by_id[node_id] = node
        for key in REQUIRED_KEYS:
            if key not in node:
                problems.append(f"{node_id}: 缺少要素 '{key}'")
        template = node.get("role_template", "")
        if template and _existing_resource(template, org_dir) is None:
            problems.append(f"{node_id}: role_template 不存在: {template}")
    return problems, by_id


def _node_reference_problems(nodes: list, by_id: dict, commands: set,
                             org_dir: Optional[Path]) -> list:
    """Validate parent links and executable auto-check references."""
    problems = []
    for node in nodes:
        node_id = node.get("id", "")
        parent = node.get("parent", "")
        if parent and parent not in by_id:
            problems.append(f"{node_id}: parent '{parent}' 不存在")
        for check in node.get("auto_checks", []):
            problem = _auto_check_problem(node_id, str(check), commands, org_dir)
            if problem:
                problems.append(problem)
    return problems


def org_check(nodes: Optional[list] = None,
             org_dir: Optional[Path] = None) -> dict:
    """规范完整性：9 要素 / 顺序 / id 唯一 / 命令存在。"""
    nodes = nodes if nodes is not None else load_nodes(org_dir)
    problems = []
    if not nodes:
        problems.append(_missing_assets_problem(org_dir))
    commands = _known_commands(org_dir)
    schema_problems, by_id = _node_schema_problems(nodes, org_dir)
    problems.extend(schema_problems)
    problems.extend(_node_reference_problems(nodes, by_id, commands, org_dir))
    stages = sorted(node.get("stage", -1) for node in nodes)
    expected = list(range(len(nodes))) if nodes else []
    if stages != expected:
        problems.append(f"stage 不连续: {stages}（期望 {expected}）")
    return {"valid": not problems, "problems": problems,
            "node_count": len(nodes),
            "stages": stages}


def render_role_prompt(node: dict) -> str:
    """岗位说明书 → 角色注入片段（供 meta 编排/角色 prompt 使用）。"""
    lines = [f"## Role: {node.get('name', '')}（{node.get('role', '')}）", ""]
    lines.append("### 职责")
    for item in node.get("responsibilities", []):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("### 输入")
    lines.append("- " + " / ".join(node.get("inputs", [])))
    lines.append("")
    lines.append("### 输出契约")
    lines.append("- " + " / ".join(node.get("outputs", [])))
    lines.append("")
    lines.append("### 检查规则（必须遵守）")
    for item in node.get("check_rules", []):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("### 禁止事项")
    for item in node.get("forbidden", []):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("### 最佳实践")
    for item in node.get("best_practices", []):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("### Review Checklist（完成前逐项自检）")
    for item in node.get("review_checklist", []):
        lines.append(f"- [ ] {item}")
    lines.append("")
    lines.append("### 自动检测（完成后必须运行）")
    for check in node.get("auto_checks", []):
        lines.append(f"- `{check}`")
    return "\n".join(lines)


def run_gate(node_id: str, task: str = "", code_dir: str = "",
             file_path: str = "", log_path: str = "") -> dict:
    """执行节点 auto_checks 作为流程门禁：占位符注入 → 有界子进程 →
    汇总（缺参命令标记 skipped，不伪装通过）。"""
    nodes = load_nodes()
    if not nodes:
        return {"error": _missing_assets_problem()}
    node = next((n for n in nodes if n.get("id") == node_id), None)
    if node is None:
        return {"error": f"unknown node {node_id!r}"}
    results = []
    params = _gate_params(task, code_dir, file_path, log_path)
    for check in node.get("auto_checks", []):
        unsupported = _unsupported_placeholders(str(check))
        if unsupported:
            results.append({"check": check, "status": "skipped",
                            "reason": f"不支持的占位符 {', '.join(unsupported)}"})
            continue
        missing = [key for key, value in params.items()
                   if key in str(check) and not value]
        if missing:
            results.append({"check": check, "status": "skipped",
                            "reason": f"缺少参数 {', '.join(missing)}"})
            continue
        results.append(_execute_check(check, params))
    return _summarize(node_id, results)


def _render_check_argv(check, params: dict) -> tuple[Optional[list], str]:
    """Expand trusted command templates into argv without invoking a shell.

    Placeholder values are inserted after ``shlex.split``.  A task containing
    ``;``, ``$()`` or pipes therefore remains one argument and cannot become
    a second command.  Shell syntax in the static organization file is not a
    supported auto-check contract; use an explicit executable instead.
    """
    template = str(check)
    sentinels = {}
    for index, (key, value) in enumerate(params.items()):
        sentinel = f"PBATCH_PARAM_{index}_TOKEN"
        sentinels[sentinel] = value
        template = template.replace(key, sentinel)
    leftovers = _PLACEHOLDER_RE.findall(template)
    if leftovers:
        return None, f"未替换占位符 {', '.join(leftovers)}（配置错误）"
    try:
        argv = shlex.split(template)
    except ValueError as exc:
        return None, f"命令引号无效: {exc}"
    if not argv:
        return None, "auto_check 命令为空"
    for index, token in enumerate(argv):
        for sentinel, value in sentinels.items():
            argv[index] = token.replace(sentinel, value)
    return _normalize_check_argv(argv), ""


def _normalize_check_argv(argv: list) -> list:
    """Apply the repository's script and ``pi-batch`` executable aliases."""
    if argv[0].startswith("scripts/"):
        script = _existing_resource(argv[0])
        if script is not None:
            argv[0] = str(script)
        argv.insert(0, sys.executable)
    if argv[0] == "pi-batch":
        argv = [sys.executable, "-m", "pbatch", *argv[1:]]
    return argv


def _execute_check(check, params: dict) -> dict:
    """执行单个 auto_check（有界子进程；reflect 低分 = attention）。"""
    argv, error = _render_check_argv(check, params)
    if error:
        return {"check": check, "status": "failed", "reason": error}
    try:
        result = subprocess.run(argv, shell=False, capture_output=True,
                                text=True, timeout=120)
        ok = result.returncode == 0
        status = ("passed" if ok else
                  "attention" if "reflect" in str(check) else "failed")
        return {"check": check, "status": status,
                "exit_code": result.returncode,
                "evidence": (result.stdout + result.stderr).strip()[-200:]}
    except subprocess.TimeoutExpired:
        return {"check": check, "status": "failed", "reason": "timeout"}
    except OSError as exc:
        return {"check": check, "status": "failed", "reason": str(exc)}


def _summarize(node_id: str, results: list) -> dict:
    """gate 汇总：failed 阻塞，attention 展示不阻塞。"""
    passed = sum(1 for r in results if r["status"] == "passed")
    failed = sum(1 for r in results if r["status"] == "failed")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    attention = sum(1 for r in results if r["status"] == "attention")
    return {"node": node_id, "passed": passed, "failed": failed,
            "skipped": skipped, "attention": attention,
            "gate_passed": failed == 0, "results": results}


def _cmd_run(args) -> None:
    """流程串联：impact 分流 → 节点 gate 顺序执行 → 汇总（可落盘）。"""
    report = run_flow(args.task, args.code, args.level, args.file,
                      args.artifact, args.log)
    if args.save:
        path = save_flow(report)
        print(f"saved: {path}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"flow [{report['run_level']}]（impact 判定 "
              f"{report['impact_level']}，成本 {report['change_cost']}）")
        print(f"角色: {', '.join(report['required_agents']) or '-'}")
        for node in report["nodes"]:
            state = "✓" if node["gate_passed"] else "✗"
            print(f"  {state} {node['node']:<14} "
                  f"{node['passed']}✓ {node['failed']}✗ "
                  f"{node['attention']}! {node['skipped']}·")
        print("flow " + ("passed" if report["flow_passed"] else "FAILED"))
    raise SystemExit(0 if report["flow_passed"] else 1)


def _cmd_prompt(args) -> None:
    """岗位说明书 → 角色注入片段。"""
    node = next((n for n in load_nodes() if n.get("id") == args.node), None)
    if node is None:
        print(f"unknown node {args.node!r}", file=sys.stderr)
        raise SystemExit(2)
    print(render_role_prompt(node))


def _cmd_gate(args) -> None:
    """节点门禁：auto_checks 注入参数后执行并汇总。"""
    report = run_gate(args.node, args.task, args.code, args.file,
                      args.log)
    if "error" in report:
        print(f"org gate: {report['error']}", file=sys.stderr)
        raise SystemExit(2)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"gate [{report['node']}]: "
              f"{report['passed']} 通过 / {report['failed']} 失败 / "
              f"{report['skipped']} 跳过 / {report['attention']} 注意")
        for result in report["results"]:
            mark = {"passed": "✓", "failed": "✗", "skipped": "·",
                    "attention": "!"}[result["status"]]
            print(f"  {mark} {result['check'][:70]}")
            if result["status"] in ("failed", "attention"):
                detail = result.get("evidence") or result.get("reason", "")
                print(f"      {detail[:120]}")
    raise SystemExit(0 if report["gate_passed"] else 1)


def _build_parser():
    """org 子命令参数：check/show/prompt/gate/flow。"""
    parser = argparse.ArgumentParser(
        prog="pi-batch.py org",
        description="Agent 工程组织规范：17 节点岗位说明书")
    sub = parser.add_subparsers(dest="command", required=True)
    p_check = sub.add_parser("check", help="校验规范完整性")
    p_check.add_argument("--json", action="store_true")
    p_show = sub.add_parser("show", help="查看节点规范")
    p_show.add_argument("node", nargs="?", default="")
    p_prompt = sub.add_parser("prompt", help="岗位说明书 → 角色注入片段")
    p_prompt.add_argument("node")
    p_gate = sub.add_parser("gate", help="执行节点 auto_checks 作为门禁")
    p_gate.add_argument("node")
    p_gate.add_argument("--task", default="", help="需求文本（{task} 注入）")
    p_gate.add_argument("--code", default="", help="代码目录（{code} 注入）")
    p_gate.add_argument("--file", default="", help="文件路径（{file} 注入）")
    p_gate.add_argument("--log", default="", help="日志路径（{log} 注入）")
    p_gate.add_argument("--json", action="store_true")
    p_run = sub.add_parser("run", help="流程串联执行（impact 分流 → 节点 gate）")
    p_run.add_argument("--task", required=True)
    p_run.add_argument("--code", default="")
    p_run.add_argument("--file", default="")
    p_run.add_argument("--log", default="")
    p_run.add_argument("--level", default="",
                       choices=("", "L0", "L1", "L2", "L3"),
                       help="工作等级（缺省由 impact 判定）")
    p_run.add_argument("--json", action="store_true")
    p_run.add_argument("--save", action="store_true",
                       help="落盘流程工件（用户级 runtime/flows/）")
    p_run.add_argument("--artifact", default="",
                       help="节点输出工件目录（下游输入，每节点一个 txt）")
    p_lesson = sub.add_parser("lesson", help="失败案例 → learn 沉淀建议")
    p_lesson.add_argument("node")
    p_runs = sub.add_parser("runs", help="历史流程工件清单")
    p_runs.add_argument("--json", action="store_true")
    sub.add_parser("flow", help="全流程节点图")
    return parser


# Compatibility exports for callers that imported workflow constants here.
LEVEL_NODES = org_workflow.LEVEL_NODES


def run_flow(task: str, code_dir: str = "", level: str = "",
             file_path: str = "", artifact_dir: str = "",
             log_path: str = "") -> dict:
    """流程串联执行：impact 分流 → 按工作等级顺序跑节点 gate。"""
    return org_workflow.run_flow(
        task, code_dir, level, file_path, artifact_dir, log_path, run_gate)


def _write_artifact(artifact_dir: str, node_id: str, gate: dict) -> None:
    """节点输出工件落盘（下游节点输入；只写证据摘录，不写全量）。"""
    org_workflow.write_artifact(artifact_dir, node_id, gate)


def flows_dir(cwd: str = "") -> Path:
    """流程工件目录（data_dir 感知）。"""
    from .config import data_dir
    return data_dir(cwd) / "flows"


def save_flow(report: dict) -> str:
    """流程工件落盘（可审计、可续查）：用户级 runtime/flows/<ts>-<slug>.json。"""
    return org_workflow.save_flow(report, flows_dir())


def list_flows() -> list:
    """历史流程工件（新→旧，有界读取）。"""
    return org_workflow.list_flows(flows_dir())


def lesson_for(node: dict) -> str:
    """失败案例 → learn draft 沉淀（案例从文档变成可行动知识）。"""
    return org_workflow.lesson_for(node)


def _cmd_check(args) -> None:
    """规范完整性校验（fail closed）。"""
    report = org_check()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        stages = report["stages"]
        span = f"{stages[0]}-{stages[-1]}" if stages else "-"
        print(f"agent-org: {report['node_count']} 个节点 | "
              f"stages {span}")
        for problem in report["problems"]:
            print(f"  ⚠ {problem}")
        print("valid" if report["valid"] else "INVALID")
    raise SystemExit(0 if report["valid"] else 1)


def _cmd_lesson(args) -> None:
    """失败案例 → learn 沉淀建议。"""
    node = next((n for n in load_nodes() if n.get("id") == args.node), None)
    if node is None:
        print(f"unknown node {args.node!r}", file=sys.stderr)
        raise SystemExit(2)
    print(lesson_for(node))


def _cmd_runs(args) -> None:
    """历史流程工件清单（新→旧）。"""
    records = list_flows()
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return
    print(f"# Flow Records ({len(records)})")
    for record in records:
        print(f"  {record.get('file', ''):<30} "
              f"{record.get('run_level', '?'):<4} "
              f"{'✓' if record.get('flow_passed') else '✗'} "
              f"{record.get('task', '')[:40]}")


def _cmd_flow() -> None:
    """全流程节点图（0-16 顺序）。"""
    nodes = load_nodes()
    if not nodes:
        print(_missing_assets_problem(), file=sys.stderr)
        raise SystemExit(1)
    for node in sorted(nodes, key=lambda n: n.get("stage", 0)):
        arrow = " → " if node.get("stage", 0) < 16 else ""
        print(f"{node.get('stage', '?'):>2} {node.get('name', '?'):<24}"
              f"{arrow}")


def _cmd_show(args) -> None:
    """查看节点规范（JSON）。"""
    nodes = load_nodes()
    target = args.node
    node = next((n for n in nodes if n.get("id") == target), None)
    if node is None:
        print(f"unknown node {target!r}（可用: "
              + ", ".join(n.get("id", "") for n in nodes) + "）",
              file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(node, ensure_ascii=False, indent=2))


def org_main(argv: Optional[list] = None) -> int:
    """`pi-batch org check|show|prompt|gate|run|runs|lesson|flow`。"""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        _cmd_check(args)  # 内部 raise
    elif args.command == "prompt":
        _cmd_prompt(args)
    elif args.command == "gate":
        _cmd_gate(args)  # 内部 raise
    elif args.command == "run":
        _cmd_run(args)  # 内部 raise
    elif args.command == "lesson":
        _cmd_lesson(args)
    elif args.command == "runs":
        _cmd_runs(args)
    elif args.command == "flow":
        _cmd_flow()
    else:
        _cmd_show(args)
    raise SystemExit(0)
