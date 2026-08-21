#!/usr/bin/env python3
"""为产品仓库生成 docs/agent-context/ 平台上下文包（幂等，可重复执行）。

从 platform-map.yaml 读取平台事实，按仓库裁剪生成：
  context-map.yaml       context 路由表（关键词 → 文档）
  platform-contract.md   本仓库职责边界 + 数据所有权 + 审计分级 + 命名规范
  README.md              说明

用法:
  python3 gen-agent-context.py                     # 生成到 6 个产品仓库
  python3 gen-agent-context.py --only aero-vault   # 单仓库
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
from platform_common import parse_mini_yaml  # noqa: E402

REPOS = {
    "snaplink": Path.home() / "workspace/demo/snaplink",
    "snaplink-console": Path.home() / "workspace/demo/snaplink-console",
    "snaplink-audit-governance": Path.home() / "workspace/demo/snaplink-audit-governance",
    "aero-id": Path.home() / "aero-id",
    "aero-vault": Path.home() / "aero-vault",
    "aero-im": Path.home() / "aero-im",
}

CONTEXT_MAP = """# Context routing map (generated from the platform map; edit with care).
routes:
  - when:
      task_contains: [平台, 组合, 契约, 边界, 职责, 审计, 权限, 租户, 事件,
                      platform, contract, composition, boundary, audit,
                      permission, scope, tenant, event]
    load: [docs/agent-context/platform-contract.md]
"""


def build_contract(name: str, pmap: dict) -> str:
    project = pmap["projects"][name]
    lines = [
        f"# 平台契约（本仓库视角：{name}）",
        "",
        f"> 自动生成自 platform-map.yaml（组合模型）。本仓库是**独立可部署产品**，",
        "> 通过标准协议与其他服务组合；核心代码只依赖端口，不依赖兄弟项目类名。",
        "",
        f"## 本仓库角色",
        f"`{project.get('role', '?')}`",
        "",
        "## 拥有的领域资源（唯一写入源）",
    ]
    for own in project.get("owns", []):
        lines.append(f"- `{own}`")
    lines.append("")
    lines.append("## 能力清单（capabilities）")
    for cap in project.get("capabilities", []):
        lines.append(f"- {cap}")
    lines.append("")
    lines.append("## 集成端口（可选适配器，不硬编码）")
    integrations = project.get("integrations") or {}
    if isinstance(integrations, str):
        integrations = {}  # empty flow dict {} parsed as a scalar
    for port, adapters in integrations.items():
        rendered = ", ".join(adapters) if isinstance(adapters, list) else adapters
        lines.append(f"- `{port}`: {rendered}")
    if not integrations:
        lines.append("- 无（本仓库是被集成方）")
    lines.append("")
    lines.append("## 数据所有权（全局唯一写入源）")
    lines.append("| 数据 | 权威源 |")
    lines.append("|---|---|")
    for data, owner in (pmap.get("data_ownership") or {}).items():
        mark = "**本仓库**" if owner == name else owner
        lines.append(f"| {data} | {mark} |")
    lines.append("")
    lines.extend(_dependency_sections(pmap))
    lines.append("")
    lines.append("## 命名规范（跨服务统一，防六套标准）")
    lines.append("- 权限：`<domain>.<resource>.<action>`（如 vault.file.delete）")
    lines.append("- 事件：`<domain>.<resource>.<action>@vN`（如 vault.file.deleted@1.1）")
    lines.append("- 全平台使用不可变 UUID 作为跨系统主键；不依赖用户名/邮箱")
    lines.append("")
    lines.append("## 变更纪律")
    lines.append("- 单项目需求只改本仓库；跨项目变更必须同时改变双方契约并有 Change Manifest")
    lines.append("- AI 产出是提案：按 Verified/Partial/Missing/Proposed 核对后提升")
    return "\n".join(lines)


def _dependency_sections(pmap: dict) -> list[str]:
    """同步依赖 + 审计集成段落（独立成函数以控制 build_contract 行数）。"""
    lines = ["## 同步依赖规则", f"- 同步链上限：{pmap.get('dependency_rules', {}).get('max_sync_chain', '?')}"]
    for rule in pmap.get("dependency_rules", {}).get("allowed_sync", []):
        lines.append(f"- 允许：{rule}")
    for rule in pmap.get("dependency_rules", {}).get("forbidden", []):
        lines.append(f"- 禁止：{rule}")
    lines.append("")
    lines.append("## 审计集成（L0/L1/L2 分级，full 组合部署用 L2）")
    for level, desc in (pmap.get("audit_levels") or {}).items():
        lines.append(f"- {level}: {desc}")
    return lines


def gen(name: str, pmap: dict) -> None:
    repo = REPOS[name]
    if not repo.is_dir():
        print(f"[SKIP] {name}: {repo} not found")
        return
    ctx_dir = repo / "docs/agent-context"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    (ctx_dir / "context-map.yaml").write_text(CONTEXT_MAP, encoding="utf-8")
    (ctx_dir / "platform-contract.md").write_text(build_contract(name, pmap), encoding="utf-8")
    (ctx_dir / "README.md").write_text(
        "# Agent Context（平台契约）\n\n"
        "- `context-map.yaml`：context 子命令路由表\n"
        "- `platform-contract.md`：本仓库职责边界与平台规则（自动生成，改 platform-map 后重跑）\n\n"
        "验证：`python3 pi-batch.py context \"审计事件\" --paths src/`\n",
        encoding="utf-8")
    print(f"[OK] {name}: docs/agent-context/ generated")


def main() -> int:
    only = ""
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    pmap = parse_mini_yaml((ROOT / "platform-map.yaml").read_text(encoding="utf-8"))
    for name in REPOS:
        if only and name != only:
            continue
        gen(name, pmap)
    return 0


if __name__ == "__main__":
    sys.exit(main())
