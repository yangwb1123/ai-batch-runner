"""check-ui-geometry.py 回归自测（tests/ 下随 make ci 运行）。

构造违规/合规样例，断言检查脚本的拦截与豁免行为。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-ui-geometry.py"


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


def test_random_radius_rejected():
    code, _ = run_on(".a { border-radius: 8px; }\n")
    assert code != 0, "8px 圆角不在 {6,10,16} 家族必须被拒"


def test_radius_family_accepted():
    code, _ = run_on(".a { border-radius: 6px; }\n.b { border-radius: 999px; }\n")
    assert code == 0, "6px/999px 圆角应通过"


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
