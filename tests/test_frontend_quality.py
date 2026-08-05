"""Tests for the frontend engineering gate (scripts/check-frontend-quality.py)
and the engineering review pipeline that uses it."""

from __future__ import annotations

import json
import subprocess
import sys

SCRIPT = "scripts/check-frontend-quality.py"


def _run(*args, cwd=None):
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True, cwd=cwd, timeout=120)


def _write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_errors_always_fail_non_strict(tmp_path):
    _write(tmp_path, "src/a.tsx",
           "const data: any = x;\nconsole.log('debug', data);\n"
           "element.innerHTML = html;\n")
    result = _run("--dir", str(tmp_path))
    assert result.returncode == 1
    assert "TS any usage" in result.stderr
    assert "console.log" in result.stderr
    assert "unsafe innerHTML" in result.stderr


def test_warnings_pass_non_strict_fail_strict(tmp_path):
    big = "const [s%d, setS%d] = useState(0);\n" * 16
    _write(tmp_path, "src/God.tsx", big)
    loose = _run("--dir", str(tmp_path))
    assert loose.returncode == 0
    strict = _run("--dir", str(tmp_path), "--strict")
    assert strict.returncode == 1
    assert "state hooks" in strict.stderr


def test_clean_file_passes(tmp_path):
    _write(tmp_path, "src/ok.tsx",
           "export function ok() {\n  return <div />;\n}\n")
    result = _run("--dir", str(tmp_path), "--strict")
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_empty_dir_passes(tmp_path):
    result = _run("--dir", str(tmp_path), "--strict")
    assert result.returncode == 0
    assert "0 file(s) scanned" in result.stdout


def test_json_report_and_output_file(tmp_path):
    _write(tmp_path, "src/a.tsx", "const x: any = 1;\n")
    report = tmp_path / "out" / "quality.json"
    result = _run("--dir", str(tmp_path), "--json", "-o", str(report))
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["files_scanned"] == 1
    assert payload["total"] == 1
    assert "any" in payload["violations"][0]["detail"]
    assert report.exists()


def test_test_skips_and_eslint_disable_are_errors(tmp_path):
    _write(tmp_path, "src/b.test.ts",
           "it.skip('x', () => {});\n// eslint-disable-next-line\n")
    result = _run("--dir", str(tmp_path))
    assert result.returncode == 1
    assert ".skip" in result.stderr
    assert "eslint-disable" in result.stderr


def test_single_file_target(tmp_path):
    bad = tmp_path / "bad.ts"
    bad.write_text("console.log(1)\n", encoding="utf-8")
    result = _run(str(bad))
    assert result.returncode == 1
    assert "console.log" in result.stderr


def test_engineering_review_pipeline_schema_and_gate():
    from pbatch.config import _resolve_validators
    from pbatch.pipeline import load_pipeline
    pipeline = load_pipeline("examples/frontend-engineering-review.yaml")
    assert [s.name for s in pipeline.stages] == [
        "inventory", "engineering_review", "gate", "verdict"]
    assert pipeline.stages[1].meta is True
    assert pipeline.stages[2].validate_cmd == "uiquality"
    assert pipeline.stages[3].gate is True
    commands = _resolve_validators("uiquality")
    assert commands and "check-frontend-quality.py --dir" in commands[0]
    assert "--strict" in commands[0]


def test_engineering_role_keywords_registered():
    from pbatch.relevance import load_role_keywords
    keywords = load_role_keywords()
    for role in ("frontend_engineer", "async_reviewer", "code_architecture_reviewer"):
        assert role in keywords
        assert keywords[role]


def test_effect_leak_warning(tmp_path):
    leaky = tmp_path / "leak.tsx"
    leaky.write_text(
        'useEffect(() => {\n  window.addEventListener("resize", h);\n'
        '  setInterval(tick, 1000);\n}, []);\n', encoding="utf-8")
    strict = _run("--strict", str(leaky))
    assert strict.returncode == 1
    assert "cleanup" in strict.stderr
    clean = tmp_path / "clean.tsx"
    clean.write_text(
        'useEffect(() => {\n  const l = () => {};\n'
        '  window.addEventListener("resize", l);\n'
        '  return () => window.removeEventListener("resize", l);\n}, []);\n',
        encoding="utf-8")
    assert _run("--strict", str(clean)).returncode == 0


def test_swallowed_exception_is_error(tmp_path):
    bad = tmp_path / "swallow.tsx"
    bad.write_text("try { save(); } catch (e) {}\n", encoding="utf-8")
    result = _run(str(bad))
    assert result.returncode == 1
    assert "swallowed exception" in result.stderr


def test_loop_await_n_plus_one_warning(tmp_path):
    bad = tmp_path / "loop.tsx"
    bad.write_text("for (const id of ids) {\n  await api.fetch(id);\n}\n",
                   encoding="utf-8")
    assert _run(str(bad)).returncode == 0  # warning only
    strict = _run("--strict", str(bad))
    assert strict.returncode == 1
    assert "N+1 request risk" in strict.stderr


def test_assertion_less_test_warning(tmp_path):
    bad = tmp_path / "x.test.tsx"
    bad.write_text("it('renders', () => { render(<App />); });\n", encoding="utf-8")
    strict = _run("--strict", str(bad))
    assert strict.returncode == 1
    assert "without assertions" in strict.stderr
    good = tmp_path / "y.test.tsx"
    good.write_text("it('adds', () => { expect(add(1,2)).toBe(3); });\n",
                    encoding="utf-8")
    assert _run("--strict", str(good)).returncode == 0
