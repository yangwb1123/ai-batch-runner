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


def test_dart_edgeinsets_only_not_test_only(tmp_path):
    dart = tmp_path / "x.dart"
    dart.write_text("padding: EdgeInsets.only(bottom: 12),\n", encoding="utf-8")
    assert _run("--strict", str(dart)).returncode == 0
    ts = tmp_path / "y.test.ts"
    ts.write_text("it.only('x', () => {});\n", encoding="utf-8")
    assert _run(str(ts)).returncode == 1


def test_dart_html_named_param_not_unsafe(tmp_path):
    dart = tmp_path / "api.dart"
    dart.write_text("html: resp.body,\n", encoding="utf-8")
    assert _run("--strict", str(dart)).returncode == 0


def test_flutter_nesting_threshold_relaxed(tmp_path):
    deep = tmp_path / "widget.dart"
    deep.write_text("if (a) {\n  if (b) {\n    if (c) {\n      if (d) {\n"
                    "        if (e) {\n          return x;\n        }\n"
                    "      }\n    }\n  }\n}\n", encoding="utf-8")
    assert _run("--strict", str(deep)).returncode == 0  # depth 5 < dart limit 8


def test_dart_list_skip_not_test_skip(tmp_path):
    dart = tmp_path / "page.dart"
    dart.write_text("final page = rows.skip(1).toList();\n", encoding="utf-8")
    assert _run("--strict", str(dart)).returncode == 0
    ts = tmp_path / "t.test.ts"
    ts.write_text("test.skip('x', () => {});\n", encoding="utf-8")
    assert _run(str(ts)).returncode == 1


def test_dart_const_static_access_ok(tmp_path):
    # regression: `const AppColors.x` was produced by a refactor tool — the
    # checker itself must not flag legitimate Dart patterns
    dart = tmp_path / "w.dart"
    dart.write_text("const x = Color(0xFF6366F1);\n", encoding="utf-8")
    assert _run("--strict", str(dart)).returncode == 0


def test_frontend_skip_flag(tmp_path):
    """AC3: --skip unions with DEFAULT_EXCLUDES. --skip e2e keeps 2 (e2e is
    already a default exclude — union AND replace agree here, so this line
    pins idempotence, not the trap); --skip scratch -> 1 is the line that
    catches a replace implementation (it would drop the defaults and re-scan
    e2e/, yielding 2)."""
    _write(tmp_path, "src/a.tsx", "const x: any = 1;\n")
    _write(tmp_path, "scratch/b.tsx", "const x: any = 1;\n")
    _write(tmp_path, "e2e/c.tsx", "const x: any = 1;\n")

    def scanned(*extra):
        result = _run("--dir", str(tmp_path), "--json", *extra)
        return json.loads(result.stdout)["files_scanned"]

    assert scanned() == 2                  # e2e excluded by default
    assert scanned("--skip", "scratch") == 1  # replace-impl would yield 2
    assert scanned("--skip", "e2e") == 2  # idempotence pin (both semantics 2)
    assert scanned("--skip", "scratch,e2e") == 1
    assert scanned("--skip", "") == 2     # empty value -> no-op


def test_frontend_flaglike_value_rejected(tmp_path):
    """F1: --skip followed by a flag is a usage error (exit 2), never
    silently swallowed — --skip --json must not degrade to human mode with
    exit 0 on a clean tree."""
    _write(tmp_path, "src/a.tsx", "const x: any = 1;\n")

    for args in (("--skip", "--json"), ("--skip", "--strict")):
        result = _run("--dir", str(tmp_path), *args)
        assert result.returncode == 2, args
        assert "expected one argument" in result.stderr, args
        assert "{" not in result.stdout, args  # no JSON, no false pass

    # bare dash is still a legal value (argparse-family convention)
    _write(tmp_path, "src/a.tsx", "export const ok = 1;\n")
    result = _run("--dir", str(tmp_path), "--skip", "-")
    assert result.returncode == 0


def test_frontend_json_ok_path_pure(tmp_path):
    """R2: under --json the OK summary goes to stderr; stdout is exactly
    one JSON document."""
    _write(tmp_path, "src/clean.tsx",
           "export function ok() {\n  return <div />;\n}\n")
    result = _run("--dir", str(tmp_path), "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["total"] == 0
    assert "UI-QUALITY" not in result.stdout
    assert "UI-QUALITY: OK" in result.stderr


def test_frontend_trailing_skip_diagnostic(tmp_path):
    """F1 (mirror): trailing --skip (no value) is a visible missing-target
    violation (exit 1), never a silent accept; stdout stays strict JSON."""
    _write(tmp_path, "src/a.tsx", "export const ok = 1;\n")
    result = _run("--dir", str(tmp_path), "--json", "--skip")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    files = [v["file"] for v in payload["violations"]]
    assert "--skip" in files
    assert "missing target" in result.stderr


def test_frontend_skip_explicit_file_immune(tmp_path):
    """F2 (mirror): --skip filters --dir scans only; explicit file targets
    are scanned regardless; positional mode is a --skip no-op."""
    _write(tmp_path, "src/a.tsx", "export const ok = 1;\n")
    viol = tmp_path / "scratch" / "b.tsx"
    _write(tmp_path, "scratch/b.tsx", "const x: any = 1;\n")

    result = _run("--dir", str(tmp_path), "--skip", "scratch", "--json",
                  str(viol))
    payload = json.loads(result.stdout)
    assert payload["files_scanned"] == 2  # src/a.tsx (dir) + explicit viol
    assert payload["total"] == 1
    assert any(v["file"] == str(viol) for v in payload["violations"])

    with_skip = _run(str(viol), "--skip", "scratch")
    without = _run(str(viol))
    assert with_skip.returncode == without.returncode == 1
    assert with_skip.stdout == without.stdout
    assert with_skip.stderr == without.stderr


def test_frontend_skip_all_excluded_empty_pass(tmp_path):
    """F3 (mirror): frontend convention — an empty scan after --skip is a
    pass (exit 0), symmetric to its empty-directory convention; backend's
    fail-closed behavior is pinned in test_backend_specs."""
    _write(tmp_path, "src/a.tsx", "const x: any = 1;\n")
    _write(tmp_path, "scratch/b.tsx", "const x: any = 1;\n")
    result = _run("--dir", str(tmp_path), "--json", "--skip",
                  "src,scratch")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["files_scanned"] == 0
    assert payload["total"] == 0
    assert "UI-QUALITY: OK (0 file(s) scanned)" in result.stderr


def test_frontend_o_flag_summary_on_stdout(tmp_path):
    """F6: -o without --json keeps the human summary on stdout; the report
    file is strict-parseable JSON (mirror of the backend C2 test)."""
    _write(tmp_path, "src/clean.tsx",
           "export function ok() {\n  return <div />;\n}\n")
    report = tmp_path / "report.json"
    result = _run("--dir", str(tmp_path), "-o", str(report))
    assert result.returncode == 0
    assert "UI-QUALITY: OK" in result.stdout
    assert json.loads(report.read_text(encoding="utf-8"))["total"] == 0


# --- api-call accounting: Map/Set lookups are not transport (D211 false red) ---
#
# `_API_RE`'s bare `.get|.post|.put|.delete` branch used to count in-memory
# `Map.prototype.get()` as a network call. Files holding six Map lookups were
# pinned at exactly MAX_API_CALLS with zero transport, so the gate was red for
# a non-violation and had no headroom. These tests pin both directions.


def test_map_lookups_do_not_count_as_api_calls(tmp_path):
    """Six in-memory lookups must NOT trip the api-call budget."""
    body = "".join(f"const v{i} = production.get(row{i}.id)\n" for i in range(6))
    _write(tmp_path, "src/rows.ts",
           "export const build = (production: ReadonlyMap<string, X>) => {\n"
           + body + "  return 0\n}\n")
    result = _run("--dir", str(tmp_path), "--strict")
    assert result.returncode == 0, result.stderr
    assert "api calls" not in result.stderr


def test_new_map_instance_lookups_do_not_count(tmp_path):
    """A locally constructed Map is provably a collection — masked too."""
    _write(tmp_path, "src/local.ts",
           "export const f = () => {\n"
           "  const byKey = new Map<string, number>()\n"
           "  byKey.get('a')\n  byKey.get('b')\n  byKey.delete('c')\n"
           "  return byKey.size\n}\n")
    result = _run("--dir", str(tmp_path), "--strict")
    assert result.returncode == 0, result.stderr
    assert "api calls" not in result.stderr


def test_map_prototype_and_ctor_receivers_do_not_count(tmp_path):
    _write(tmp_path, "src/proto.ts",
           "const a = Map.prototype.get(m, k)\n"
           "const b = new Map().get(k)\n"
           "const c = new URLSearchParams(u).get('x')\n")
    result = _run("--dir", str(tmp_path), "--strict")
    assert result.returncode == 0, result.stderr
    assert "api calls" not in result.stderr


def test_real_api_calls_still_trip_the_budget(tmp_path):
    """Negative control — the mask must NOT hide genuine transport calls.
    Without this the fix would be a one-way ratchet into false greens."""
    _write(tmp_path, "src/api.ts",
           "".join(f"const r{i} = api.get('/x/{i}')\n" for i in range(6)))
    result = _run("--dir", str(tmp_path), "--strict")
    assert result.returncode == 1
    assert "6 api calls > 5" in result.stderr


def test_real_fetch_and_axios_still_count(tmp_path):
    _write(tmp_path, "src/api2.ts",
           "".join(f"await axios.post('/x/{i}')\n" for i in range(4))
           + "await fetch('/y')\nawait fetch('/z')\n")
    result = _run("--dir", str(tmp_path), "--strict")
    assert result.returncode == 1
    assert "6 api calls > 5" in result.stderr


def test_unknown_receiver_still_counted_conservatively(tmp_path):
    """A receiver we cannot prove is a collection (here: a test helper) keeps
    being counted. The fix removes provable noise only."""
    _write(tmp_path, "src/wrapper.ts",
           "".join(f"wrapper.get({i})\n" for i in range(6)))
    result = _run("--dir", str(tmp_path), "--strict")
    assert result.returncode == 1
    assert "6 api calls > 5" in result.stderr


def test_mixed_map_and_real_calls_count_only_real_ones(tmp_path):
    _write(tmp_path, "src/mixed.ts",
           "const m = new Map<string, number>()\n"
           + "".join(f"m.get({i})\n" for i in range(9))
           + "const r = api.get('/only-one')\n")
    result = _run("--dir", str(tmp_path), "--strict")
    assert result.returncode == 0, result.stderr
    assert "api calls" not in result.stderr
