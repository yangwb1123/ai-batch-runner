#!/usr/bin/env python3
"""iris-ui 高维 UI 规范机械门禁（纯 stdlib）。

目标：把 iris-ui 组件库推到"统一、产品完成度高"——组件样式必须全部
由 --iris-* token 家族驱动，禁止裸值漂移。四个维度：

  hex      硬编码颜色字面量（裸 hex / var() fallback 与 token 定义不一致）
  spacing  padding/margin/gap 裸像素值不在 token 间距刻度内
  shadow   boxShadow 硬编码（未消费 --iris-shadow-*）
  font     fontSize 裸像素值（未消费 --iris-font-size-*）

用法：
  python iris-ui-spec.py --mode all --dir <iris-ui-root>/packages/react/src
  python iris-ui-spec.py --mode hex --dir <root> --json -o report.json
  # 无 --dir 时默认扫描四个适配器 + 插件包

退出码：0 = 合规；1 = 有违规。违规明细走 stderr（ai-batch-runner 会把
stderr 喂回重试 prompt）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ── token 间距刻度（与 ui-specs 8pt 家族对齐；0 仅允许 reset）──
SPACING_SCALE = {0, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64}

# 扫描时跳过的目录（token 定义源 / 颜色本质组件 / 构建产物 / 测试）
SKIP_DIRS = {
    "node_modules", "dist", ".git", ".turbo", ".pi-batch", "coverage",
    "icons", "color-picker", "tokens", "theme", "skins",
    "apps", "playground", "docs", "testdata", "generated", ".vitepress",
}
SKIP_FILE_SUFFIX = (".test.", ".spec.", ".bench.")
EXTS = (".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte")

TOKENS_LIGHT = Path("/home/u1/iris-ui/packages/tokens/src/light.ts")
TOKENS_DARK = Path("/home/u1/iris-ui/packages/tokens/src/dark.ts")

# ── token 定义解析：'iris.xxx': value ─────────────────────────────
_TOKEN_RE = re.compile(r"'iris\.[a-zA-Z0-9.]+'\s*:\s*'([^']*)'")

# ADR-006 派生 token：themeCssVarEntries 运行时从语义色推导，无需在
# light/dark 定义（--iris-{semantic}-subtle 等）。
DERIVED_TOKENS = {
    "--iris-primary-subtle",
    "--iris-success-subtle",
    "--iris-warning-subtle",
    "--iris-danger-subtle",
    "--iris-info-subtle",
}

def _load_token_map() -> dict:
    out = {}
    for path in (TOKENS_LIGHT, TOKENS_DARK):
        if path.exists():
            for m in _TOKEN_RE.finditer(path.read_text(encoding="utf-8")):
                key = m.group(0).split("'")[1]
                out.setdefault(key, m.group(1))
    return out

TOKEN_MAP = _load_token_map()

def _norm_color(v: str) -> str:
    """#fff -> #ffffff（3/4/6/8 位 hex 归一化，用于 fallback 对比）"""
    v = v.strip().lower()
    if not v.startswith("#") or len(v) not in (4, 5, 7, 9):
        return v
    hexpart = v[1:]
    if len(hexpart) in (3, 4):
        hexpart = "".join(c * 2 for c in hexpart)
    return "#" + hexpart

# ── 正则 ───────────────────────────────────────────────────────────
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_VAR_FALLBACK_RE = re.compile(r"var\(\s*(--iris-[\w-]+)\s*,\s*([^)]+?)\s*\)")

# 间距：style 对象字符串值（可含多值简写与 var() 混合）
_SPACING_VALUE_RE = re.compile(
    r"['\"]?(?:padding|margin|gap|rowGap|columnGap|paddingLeft|paddingRight|"
    r"paddingTop|paddingBottom|marginLeft|marginRight|marginTop|marginBottom)"
    r"['\"]?\s*[:=]\s*(['\"])([^'\"]*)\1"
)
# 间距：数字型（gap: 6, padding: 12）
_SPACING_NUM_RE = re.compile(
    r"['\"]?(?:gap|rowGap|columnGap|padding|margin)['\"]?\s*[:=]\s*(\d+)(?=\s*[,)}])"
)
# CSS 块（svelte/vue <style>）间距，支持多值
_SPACING_CSS_RE = re.compile(
    r"\b(?:padding|margin|gap|row-gap|column-gap|padding-left|padding-right|"
    r"padding-top|padding-bottom|margin-left|margin-right|margin-top|margin-bottom)"
    r"\s*:\s*([^;{}]+);"
)


def _spacing_tokens(value: str) -> list:
    """把间距值拆成原子 token（'8px 10px' -> ['8px','10px']），
    var() 整体作为一个 token，rem/em 换算 px。"""
    out = []
    for part in value.split():
        part = part.strip()
        if not part:
            continue
        if part.startswith("var(") or part in ("auto", "0", "0px", "inherit",
                                                 "unset", "initial", "transparent"):
            continue
        m = re.match(r"^(-?\d+(?:\.\d+)?)(px|rem|em)?$", part)
        if m:
            num = float(m.group(1))
            unit = m.group(2) or "px"
            px = num * 16 if unit in ("rem", "em") else num
            out.append(px)
    return out
_SHADOW_RE = re.compile(
    r"boxShadow\s*[:=]\s*['\"](?!var\()([^'\"]+)['\"]"
)
_SHADOW_CSS_RE = re.compile(
    r"box-shadow\s*:\s*(?!var\(--iris-shadow)[^;{}]+;"
)
_FONT_STYLE_RE = re.compile(
    r"fontSize\s*[:=]\s*['\"](\d+)px['\"]"
)
_FONT_CSS_RE = re.compile(
    r"font-size\s*:\s*(\d+)px\b"
)


def _classify_hex(text: str, path: Path, line_no: int) -> list:
    """hex 维度：裸 hex（非 var fallback）与 fallback 漂移。"""
    out = []
    for m in _VAR_FALLBACK_RE.finditer(text):
        css_var, fb = m.group(1), m.group(2).strip().strip("'\"")
        # --iris-primary-foreground -> iris.primary.foreground（点形式 token 键）
        token_key = css_var[2:].replace("-", ".")
        if css_var in DERIVED_TOKENS:
            continue  # 派生 token（ADR-006），运行时生成
        if fb.startswith("#") and fb not in ("transparent",):
            canonical = TOKEN_MAP.get(token_key)
            if canonical and _norm_color(canonical) != _norm_color(fb):
                out.append(
                    f"{path}:{line_no + text[:m.start()].count(chr(10)) + 1}: "
                    f"fallback 漂移 var({css_var}, {fb}) != token 定义 {canonical}"
                )
            elif not canonical:
                out.append(
                    f"{path}:{line_no + text[:m.start()].count(chr(10)) + 1}: "
                    f"未知 token {css_var} 的 fallback {fb}（token 定义不存在）"
                )
    # 裸 hex：排除已被 var() 覆盖的
    protected = [(m.start(), m.end()) for m in _VAR_FALLBACK_RE.finditer(text)]
    for m in _HEX_RE.finditer(text):
        if any(s <= m.start() < e for s, e in protected):
            continue
        # 排除注释行
        prefix = text[: m.start()]
        if "//" in prefix.split("\n")[-1] or "/*" in prefix:
            continue
        out.append(
            f"{path}:{line_no + text[:m.start()].count(chr(10)) + 1}: "
            f"裸 hex 颜色 {m.group(0)}（应消费 --iris-* token）"
        )
    return out


def _check_file(path: Path, mode: str) -> list:
    text = path.read_text(encoding="utf-8", errors="replace")
    out = []
    if mode in ("hex", "all"):
        out += _classify_hex(text, path, 1)
    if mode in ("spacing", "all"):
        for m in _SPACING_VALUE_RE.finditer(text):
            for px in _spacing_tokens(m.group(2)):
                if px not in SPACING_SCALE:
                    out.append(
                        f"{path}:{text[:m.start()].count(chr(10)) + 1}: "
                        f"魔法间距 {m.group(0)[:60]}（token 刻度 {sorted(SPACING_SCALE - {0})}）"
                    )
                    break
        for m in _SPACING_NUM_RE.finditer(text):
            v = int(m.group(1))
            if v not in SPACING_SCALE:
                out.append(
                    f"{path}:{text[:m.start()].count(chr(10)) + 1}: "
                    f"魔法间距 {m.group(0)[:40]}（数字型，token 刻度 {sorted(SPACING_SCALE - {0})}）"
                )
        for m in _SPACING_CSS_RE.finditer(text):
            for px in _spacing_tokens(m.group(1)):
                if px not in SPACING_SCALE:
                    out.append(
                        f"{path}:{text[:m.start()].count(chr(10)) + 1}: "
                        f"魔法间距 {m.group(0)[:60]}（CSS）"
                    )
                    break
    if mode in ("shadow", "all"):
        for m in _SHADOW_RE.finditer(text):
            if "var(" in m.group(1):
                continue  # 复合阴影含 token 引用（focus ring 等），豁免
            if "9999px" in m.group(1):
                continue  # 全屏遮罩模式（Tour/Modal mask），功能性写法非阴影家族
            out.append(
                f"{path}:{text[:m.start()].count(chr(10)) + 1}: "
                f"硬编码阴影 {m.group(1)[:60]}（应消费 --iris-shadow-*）"
            )
        for m in _SHADOW_CSS_RE.finditer(text):
            if "var(" in m.group(0) or "9999px" in m.group(0):
                continue
            out.append(
                f"{path}:{text[:m.start()].count(chr(10)) + 1}: "
                f"硬编码阴影 {m.group(0)[:60]}（CSS，应消费 --iris-shadow-*）"
            )
    if mode in ("font", "all"):
        for m in _FONT_STYLE_RE.finditer(text):
            out.append(
                f"{path}:{text[:m.start()].count(chr(10)) + 1}: "
                f"裸字号 {m.group(0)}（应消费 --iris-font-size-*）"
            )
        for m in _FONT_CSS_RE.finditer(text):
            out.append(
                f"{path}:{text[:m.start()].count(chr(10)) + 1}: "
                f"裸字号 {m.group(0)}（CSS，应消费 --iris-font-size-*）"
            )
    return out


def scan(root: Path, mode: str, exclude: set) -> tuple:
    """返回 (files_scanned, violations)"""
    files, violations = 0, []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in EXTS:
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if rel.name.endswith(SKIP_FILE_SUFFIX) or any(s in rel.name for s in SKIP_FILE_SUFFIX):
            continue
        files += 1
        violations += _check_file(path, mode)
    return files, violations


def _parse_argv(argv):
    mode, directory, out_file, want_json, pkg = "all", None, None, False, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--mode" and i + 1 < len(argv):
            mode = argv[i + 1]; i += 2
        elif a == "--dir" and i + 1 < len(argv):
            directory = argv[i + 1]; i += 2
        elif a == "-o" and i + 1 < len(argv):
            out_file = argv[i + 1]; i += 2
        elif a == "--pkg" and i + 1 < len(argv):
            pkg = argv[i + 1]; i += 2
        elif a == "--json":
            want_json = True; i += 1
        else:
            i += 1
    return mode, directory, out_file, want_json, pkg


def main(argv):
    mode, directory, out_file, want_json, pkg = _parse_argv(argv)
    if mode not in ("hex", "spacing", "shadow", "font", "all"):
        print(f"IRIS-UI-SPEC: unknown mode '{mode}'", file=sys.stderr)
        return 2

    if directory:
        roots = [Path(directory)]
    elif pkg:
        root = Path("/home/u1/iris-ui")
        if pkg in ("react", "vue", "solid", "svelte"):
            roots = [root / f"packages/{pkg}/src"]
        elif pkg == "plugins":
            roots = sorted((root / "packages").glob("plugin-*/src"))
        else:
            print(f"IRIS-UI-SPEC: unknown --pkg '{pkg}'", file=sys.stderr)
            return 2
    else:
        root = Path("/home/u1/iris-ui")
        roots = [root / p for p in
                 ("packages/react/src", "packages/vue/src", "packages/solid/src",
                  "packages/svelte/src")]
        roots += sorted((root / "packages").glob("plugin-*/src"))

    total_files, total_violations = 0, []
    for r in roots:
        if not r.exists():
            continue
        f, v = scan(r, mode, set())
        total_files += f
        total_violations += v

    report = {
        "mode": mode,
        "files": total_files,
        "violations": total_violations,
        "count": len(total_violations),
        "token_scale": sorted(SPACING_SCALE),
    }
    if want_json:
        payload = json.dumps(report, ensure_ascii=False, indent=1)
        if out_file:
            Path(out_file).parent.mkdir(parents=True, exist_ok=True)
            Path(out_file).write_text(payload, encoding="utf-8")
        print(payload)
    else:
        for v in total_violations:
            print(v, file=sys.stderr)
        print(f"IRIS-UI-SPEC [{mode}]: {len(total_violations)} violations "
              f"in {total_files} files")
    return 1 if total_violations else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
