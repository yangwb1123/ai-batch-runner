#!/usr/bin/env python3
"""ERP 场景路线图交付物完整性门禁（纯标准库，fail closed）。

上一轮教训：revision 阶段 agent 用工具写文件、最终回复只回摘要，
协调器捕获到 15 行"自指空壳"（自称"226 行路线图已产出"指向自身），
被终裁门实锤 FAIL。本门禁机械拦截三类空壳：

1. 长度空壳：行数 < min_lines（默认 100）→ 拒；
2. 自指空壳：前 5 行出现"已产出/已写入/已生成"且引用自身路径/文件名 → 拒；
3. 结构空壳：缺少 required_markers 中任一关键节标题 → 拒。

用法：
    python scripts/check-doc-complete.py <artifact> [--min-lines N] [--json]

注册（pi-batch.yaml / 阶段 validate_cmd）：
    doccomplete: "python {cwd}/examples/snaplink-platform/scripts/check-doc-complete.py {output}"
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_MIN_LINES = 100
# 关键节标题（按出现顺序不要求，只要求都存在）
REQUIRED_MARKERS = [
    "# 修订版最终路线图",       # 文档身份
    "2.3",                       # 幂等/409 三分支语义
    "M1 准入",                   # 门禁硬门槛
    "M2 验收",                   # E2E 验收
    "owner 决策",                # 责任人表
    "不接清单",                  # 克制原则
]

# 自指模式：前几行声称"文档已产出到 <自身路径>"，正文却是摘要
SELF_REF_RE = re.compile(
    r"(已产出|已写入|已生成|已保存|written to|saved to|output to)\s*[:：]?\s*[`*]*[^`\n]{0,80}?\.md"
)

def check(path: Path, min_lines: int) -> tuple[bool, list[str]]:
    problems: list[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) < min_lines:
        problems.append(f"empty-shell: {len(lines)} lines < {min_lines} required")
    head = "\n".join(lines[:5])
    if SELF_REF_RE.search(head):
        problems.append(
            "self-referential shell: first 5 lines claim the document was "
            "written to a path instead of containing the body"
        )
    missing = [m for m in REQUIRED_MARKERS if m not in text]
    if missing:
        problems.append(f"structure shell: missing markers: {', '.join(missing)}")
    return (not problems), problems

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("artifact")
    ap.add_argument("--min-lines", type=int, default=DEFAULT_MIN_LINES)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    path = Path(args.artifact)
    if not path.is_file():
        msg = f"doccomplete: artifact missing: {path}"
        print(msg, file=sys.stderr)
        return 1
    ok, problems = check(path, args.min_lines)
    if ok:
        print(f"doccomplete: PASS ({path})")
        return 0
    for p in problems:
        print(f"doccomplete: FAIL - {p}", file=sys.stderr)
    return 1

if __name__ == "__main__":
    sys.exit(main())
