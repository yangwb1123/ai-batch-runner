"""check-ui-geometry.py 回归自测（tests/ 下随 make ci 运行）。

构造违规/合规样例，断言检查脚本的拦截与豁免行为。圆角家族来自
ui-specs/tokens.json（radius {4,8,12}）——本文件同时承载验收映射的
pinned 回归测试（A1/A2/B1 与注册命令 e2e）。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-ui-geometry.py"
sys.path.insert(0, str(ROOT / "scripts"))
import ui_tokens  # noqa: E402


def run_on(text: str) -> tuple[int, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".css", delete=False) as f:
        f.write(text)
        path = f.name
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), path],
            capture_output=True,
            text=True,
        )
        return result.returncode, result.stderr + result.stdout
    finally:
        Path(path).unlink(missing_ok=True)


def test_bare_offset_rejected():
    code, _ = run_on(".a { margin-top: 3px; }\n")
    assert code != 0, "3px 裸偏移必须被拒"


def test_negative_offset_rejected():
    code, _ = run_on(".a { margin-left: -2px; }\n")
    assert code != 0, "-2px 裸偏移必须被拒"


def test_optical_variable_exempted():
    code, _ = run_on(".a { transform: translateY(var(--optical-offset-y, 0)); }\n")
    assert code == 0, "--optical-* 变量应豁免"


def test_old_radius_family_rejected():
    """旧家族 {6,10,16} 在 tokens.json 圆角家族 {4,8,12} 下必须被拒。"""
    code, _ = run_on(".a { border-radius: 6px; }\n.b { border-radius: 16px; }\n")
    assert code != 0, "6/16px 不在 tokens.json 圆角家族 {4,8,12} 必须被拒"


def test_radius_family_accepted():
    code, _ = run_on(
        ".a { border-radius: 4px; }\n.b { border-radius: 8px; }\n"
        ".c { border-radius: 12px; }\n.d { border-radius: 999px; }\n"
    )
    assert code == 0, "4/8/12px/999px 圆角应通过"


def test_random_stroke_rejected():
    code, _ = run_on(".a { border-top-width: 1.5px; }\n")
    assert code != 0, "1.5px 线宽不在 {1,2,3} 必须被拒"


def test_stroke_levels_accepted():
    code, _ = run_on(".a { border-width: 1px; }\n.b { border-top-width: 3px; }\n")
    assert code == 0, "1/3px 线宽应通过"


def test_sr_only_clip_block_exempted():
    code, _ = run_on(
        ".sr-only {\n  width: 1px;\n  height: 1px;\n  margin: -1px;\n"
        "  overflow: hidden;\n  clip: rect(0 0 0 0);\n}\n"
    )
    assert code == 0, "sr-only clip 块内 -1px 应豁免"


def test_tsx_inline_style_camel_case_rejected():
    """JSX inline style（camelCase，无分号）同样必须拦截。"""
    code, _ = run_on(
        "export function Page() {\n"
        '  return <section>\n'
        '    <header style={{ marginTop: "3px" }}>t</header>\n'
        '    <div style={{ borderRadius: "10px" }} />\n'
        '    <td style={{ borderBottomWidth: "1.5px" }} />\n'
        "  </section>\n"
        "}\n"
    )
    assert code != 0, "TSX inline style 违规必须被拒"


def test_tsx_inline_style_compliant_passes():
    code, _ = run_on(
        "export function Page() {\n"
        '  return <section>\n'
        '    <header style={{ marginTop: "var(--optical-content-shift, 0)" }}>t</header>\n'
        '    <div style={{ borderRadius: "8px" }} />\n'
        '    <td style={{ borderBottomWidth: "1px" }} />\n'
        "  </section>\n"
        "}\n"
    )
    assert code == 0, "合规 TSX inline style 应通过"


# ── 验收映射 A1/A2/B1 + 注册命令 e2e（tests/ 随 make ci 运行）─────────


def test_radius_family_matches_tokens_json():
    """A1：check-ui-geometry 的圆角家族必须等于 tokens.json radius 值。"""
    family = ui_tokens.radius_family()
    assert family == {4, 8, 12}, f"tokens.json radius 派生异常: {family}"
    # 与门禁实际行为一致：8px 通过、10px 拒绝（fixture 翻转证明执法反转）
    assert run_on(".a { border-radius: 8px; }\n")[0] == 0
    assert run_on(".a { border-radius: 10px; }\n")[0] != 0


def test_spacing_tokens_match_tokens_json():
    """A2：check-ui-spec.py 的间距 token 集 == tokens.json spacing == spacing.md §4。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ui_spec_under_test", ROOT / "scripts" / "check-ui-spec.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tokens_set = ui_tokens.spacing_tokens()
    assert module.SPACING_TOKENS == tokens_set
    assert tokens_set == {0, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64}
    spacing_md = (ROOT / "ui-specs" / "spacing.md").read_text(encoding="utf-8")
    doc_match = re.search(r"只允许 `\{([^}]+)\}`", spacing_md)
    assert doc_match, "spacing.md §4 token 集声明缺失"
    doc_set = {int(v) for v in doc_match.group(1).split(",")}
    assert tokens_set == doc_set, "spacing.md §4 与 tokens.json 漂移"


def test_report_output_flag():
    """B1：-o 写 JSON 报告（与 --json 同格式）；clean 与 violation 都写。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        out = td / "sub" / "geometry.json"
        (td / "clean.css").write_text(".a { border-radius: 8px; }\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--dir", str(td), "--all", "-o", str(out)],
            capture_output=True, text=True)
        assert result.returncode == 0, result.stderr + result.stdout
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["files"] >= 1 and report["violations"] == {}
        # violation 时同样写文件且 exit 1
        out2 = td / "geometry2.json"
        (td / "bad.css").write_text(".b { border-radius: 10px; }\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--dir", str(td), "--all", "-o", str(out2)],
            capture_output=True, text=True)
        assert result.returncode == 1
        assert json.loads(out2.read_text(encoding="utf-8"))["violations"]


def test_registered_command_8px_fixture_passes():
    """A3 e2e：pi-batch.yaml uigeometry 注册命令（--dir --all --json -o）
    在 8px 圆角 fixture 上 exit 0 并写出 geometry.json（修复 B1 前 exit 2）。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "page.css").write_text(".card { border-radius: 8px; }\n", encoding="utf-8")
        out = td / "geometry.json"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--dir", str(td), "--all",
             "--json", "-o", str(out)],
            capture_output=True, text=True)
        assert result.returncode == 0, result.stderr + result.stdout
        assert out.is_file(), "注册命令必须写出 geometry.json"


def test_json_mode_fails_closed_on_violations():
    """B1 扩展：--json 模式违规同样 exit 1（修复 fail-open；与 check-ui-spec 对齐）。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        (td / "bad.css").write_text(".a { border-radius: 10px; }\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--dir", str(td), "--all", "--json"],
            capture_output=True, text=True)
        assert result.returncode == 1, "--json 模式违规必须 exit 1（fail closed）"
        assert "FAIL" in result.stderr


def test_missing_tokens_fail_closed():
    """失败模式：tokens.json 缺失 → exit 2 + 动态错误信息（fail closed）。"""
    with tempfile.NamedTemporaryFile("w", suffix=".css", delete=False) as f:
        f.write(".a { border-radius: 8px; }\n")
        path = f.name
    try:
        env = dict(os.environ, UI_TOKENS_PATH=str(ROOT / "no-such" / "tokens.json"))
        result = subprocess.run(
            [sys.executable, str(SCRIPT), path], capture_output=True, text=True, env=env)
        assert result.returncode == 2, "tokens.json 缺失必须 fail closed (exit 2)"
        assert "tokens.json" in result.stderr and "fail closed" in result.stderr
    finally:
        Path(path).unlink(missing_ok=True)


def test_invalid_tokens_fail_closed():
    """失败模式：tokens.json 不可解析 → exit 2 + 动态错误信息。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bad = td / "tokens.json"
        bad.write_text("{ not json", encoding="utf-8")
        fixture = td / "a.css"
        fixture.write_text(".a { border-radius: 8px; }\n", encoding="utf-8")
        env = dict(os.environ, UI_TOKENS_PATH=str(bad))
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(fixture)],
            capture_output=True, text=True, env=env)
        assert result.returncode == 2
        assert "不可解析" in result.stderr
