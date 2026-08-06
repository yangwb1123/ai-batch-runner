#!/usr/bin/env python3
"""阶段产物"拒绝工作"检测（fail closed）。

LLM 在输入缺失/上游失败时会诚实地输出"无法继续"文档，而 runner 只
检查非空输出会误判为 PASS。本校验器把这类拒绝标记识别为失败，防止
虚假通过沿流水线传播（真实校准：intake 缺需求注入时整条链虚假 PASS）。

用法：python check-no-refusal.py ARTIFACT
退出码：0 = 通过；1 = 检测到拒绝标记/空输出。
"""

import sys
from pathlib import Path

REFUSAL_MARKERS = [
    "输入缺失", "未指定", "输入未提供", "无法继续",
    "FAILED — 输入", "FAILED — 输入缺失",
    "input missing", "no input provided", "cannot proceed",
    "上游能力未映射",
]


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: check-no-refusal.py ARTIFACT")
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"REFUSAL-CHECK: artifact missing: {path}")
        return 1
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        print("REFUSAL-CHECK: empty output")
        return 1
    hits = [m for m in REFUSAL_MARKERS if m in text]
    if hits:
        print(f"REFUSAL-CHECK: output refuses to work (markers: {hits}); failing closed")
        return 1
    print("REFUSAL-CHECK: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
