"""Tests for the backend spec domain: registry selection, backend rule
matching, backend assessment, the backend quality gate, and the review
pipeline."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from pbatch.rule_matcher import domain_for, load_registry, match_rules

_TESTS_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = str(_TESTS_ROOT / "scripts" / "check-backend-quality.py")


@pytest.fixture
def clean_repo():
    """Scratch dir whose ancestor chain is manifest-free. pytest tmp_path
    lives under /tmp, which on dev boxes may carry stray manifests
    (e.g. /tmp/go.mod) that would false-close the 'no manifest -> skip'
    tests; the repo root's ancestors were verified clean (design stage), so
    a scratch dir inside the repo is a reliable no-manifest environment."""
    path = Path(tempfile.mkdtemp(prefix=".beq-clean-", dir=Path.cwd()))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _run(*args, cwd=None):
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True, timeout=120, cwd=cwd)


def test_backend_domain_selection():
    assert domain_for("写一个 go 的订单接口") == "backend"
    assert domain_for("做一个登录页面") == "frontend_ui"


def test_backend_registry_loads_backend_rules():
    registry = load_registry(domain="backend")
    ids = set(registry["rules"])
    assert {"architecture", "design-patterns", "domain-modeling",
            "evolution"} <= ids
    # frontend rules never leak into the backend registry
    assert "visual-core" not in ids


def test_match_rules_uses_backend_registry_for_backend_tasks():
    matched = match_rules("写一个订单创建接口，含支付和审批，生产环境，go 实现")
    ids = [item["id"] for item in matched["rules"]]
    assert "architecture" in ids
    assert "domain-modeling" in ids  # risk-mandated on high-risk
    assert "visual-core" not in ids


def test_backend_risk_mandates_domain_modeling():
    matched = match_rules("写一个订单创建接口")
    assert matched["risk"] == "high"
    required = {item["id"] for item in matched["rules"] if item["required"]}
    assert "domain-modeling" in required


def test_backend_assessment_prescribes_backend_rules():
    from pbatch.assessor import prescription
    assessment = prescription("写一个订单创建接口，含支付和审批，生产环境，go 实现")
    ids = [item["id"] for item in assessment["prescription"]]
    assert "architecture" in ids
    assert "visual-core" not in ids
    assert "evolution" not in ids or assessment["effective_tier"] == "production"


def test_backend_checker_errors(tmp_path):
    bad = tmp_path / "order.ts"
    bad.write_text("this.status = 'paid';\nconsole.log('x');\n", encoding="utf-8")
    result = _run(str(bad))
    assert result.returncode == 1
    assert "direct status assignment" in result.stderr
    assert "console.log" in result.stderr


def test_backend_checker_domain_import_direction(tmp_path):
    domain = tmp_path / "domain" / "order.ts"
    domain.parent.mkdir(parents=True)
    domain.write_text("import { prisma } from '../../infrastructure/db';\n",
                      encoding="utf-8")
    result = _run(str(domain))
    assert result.returncode == 1
    assert "dependency direction" in result.stderr


def test_backend_checker_over_abstraction_warning(tmp_path):
    repo = tmp_path / "repo.ts"
    repo.write_text("export interface OrderRepo { find(): Promise<any>; }\n"
                    "export class OrderRepoImpl implements OrderRepo {\n"
                    "  find() { return Promise.resolve(null); }\n}\n",
                    encoding="utf-8")
    loose = _run(str(repo))
    assert loose.returncode == 0  # warnings only under --strict
    strict = _run(str(repo), "--strict")
    assert strict.returncode == 1
    assert "single-implementation interface" in strict.stderr


def test_backend_checker_empty_dir_skips_with_signal(clean_repo):
    """AC-5: an existing dir without any backend manifest passes with an
    explicit 'no backend detected' signal — never a silent OK."""
    result = _run("--dir", str(clean_repo), "--strict")
    assert result.returncode == 0
    assert "no backend manifest detected; gate skipped" in result.stdout


def test_backend_checker_missing_dir_fails_closed(tmp_path):
    """AC-1: --dir on a missing directory exits non-zero with a scan error
    instead of the vacuous 'OK (0 file(s) scanned)'."""
    result = _run("--dir", str(tmp_path / "does-not-exist"), "--strict")
    assert result.returncode == 1
    assert "scan error: directory not found" in result.stderr


def test_backend_checker_manifest_zero_files_fails_closed(tmp_path):
    """AC-2: a backend manifest with 0 scanned files fails the gate."""
    repo = tmp_path / "go-repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")
    result = _run("--dir", str(repo), "--strict")
    assert result.returncode == 1
    assert "manifest go.mod found but 0 backend files scanned" in result.stderr


def test_backend_checker_no_manifest_dir_skipped_not_scanned(clean_repo):
    """Auto mode without a manifest never falls back to the union scan
    (a root union scan would false-close on this repo's own TS/Python
    files — the repo root must skip cleanly)."""
    (clean_repo / "bad.ts").write_text("this.status = 'paid';\n",
                                       encoding="utf-8")
    result = _run("--dir", str(clean_repo), "--strict")
    assert result.returncode == 0
    assert "no backend manifest detected; gate skipped" in result.stdout
    assert "bad.ts" not in result.stderr


def test_backend_checker_go_manifest_scans_only_go(tmp_path):
    """Manifest detection pins the language: .ts violations are not scanned
    in a Go repo (union scan would false-close hybrid repos)."""
    repo = tmp_path / "go-repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")
    (repo / "bad.go").write_text("this.status = 'paid';\n", encoding="utf-8")
    (repo / "bad.ts").write_text("this.status = 'paid';\n", encoding="utf-8")
    result = _run("--dir", str(repo), "--strict")
    assert result.returncode == 1
    assert "bad.go" in result.stderr
    assert "bad.ts" not in result.stderr


def test_backend_checker_rust_manifest(tmp_path):
    repo = tmp_path / "rust-repo"
    repo.mkdir()
    (repo / "Cargo.toml").write_text("[package]\nname = 'x'\n",
                                      encoding="utf-8")
    (repo / "bad.rs").write_text("console.log('x');\n", encoding="utf-8")
    (repo / "bad.go").write_text("this.status = 'paid';\n", encoding="utf-8")
    result = _run("--dir", str(repo), "--strict")
    assert result.returncode == 1
    assert "bad.rs" in result.stderr
    assert "bad.go" not in result.stderr


def test_backend_checker_java_manifest(tmp_path):
    repo = tmp_path / "java-repo"
    repo.mkdir()
    (repo / "pom.xml").write_text("<project/>", encoding="utf-8")
    (repo / "App.java").write_text("console.log('x');\n", encoding="utf-8")
    (repo / "App.kt").write_text("console.log('x');\n", encoding="utf-8")
    result = _run("--dir", str(repo), "--strict")
    assert result.returncode == 1
    assert "App.java" in result.stderr
    assert "App.kt" in result.stderr


def test_backend_checker_node_manifest(tmp_path):
    repo = tmp_path / "node-repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    (repo / "api.ts").write_text("this.status = 'paid';\n", encoding="utf-8")
    (repo / "api.go").write_text("this.status = 'paid';\n", encoding="utf-8")
    result = _run("--dir", str(repo), "--strict")
    assert result.returncode == 1
    assert "api.ts" in result.stderr
    assert "api.go" not in result.stderr


def test_backend_checker_relative_dir_resolves_for_walkup(tmp_path):
    """A relative --dir must behave like its absolute form: the manifest
    walk-up applies to the resolved path, not the lexical '.' root."""
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "go.mod").write_text("module x\n", encoding="utf-8")
    sub = root / "services"
    sub.mkdir()
    (sub / "bad.go").write_text("this.status = 'paid';\n", encoding="utf-8")
    result = _run("--dir", ".", "--strict", cwd=str(sub))
    assert result.returncode == 1
    assert "bad.go" in result.stderr


def test_backend_checker_manifest_walkup(tmp_path):
    """A manifest above the scanned dir (e.g. --dir services/orders in a Go
    repo) is found within the bounded walk-up."""
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / "go.mod").write_text("module x\n", encoding="utf-8")
    sub = root / "services" / "orders"
    sub.mkdir(parents=True)
    (sub / "bad.go").write_text("this.status = 'paid';\n", encoding="utf-8")
    result = _run("--dir", str(sub), "--strict")
    assert result.returncode == 1
    assert "bad.go" in result.stderr


def test_backend_checker_manifest_walkup_stops_at_git_boundary(tmp_path):
    """An unrelated ancestor manifest must not classify a nested checkout."""
    outer = tmp_path / "outer"
    project = outer / "project"
    source = project / "nested"
    source.mkdir(parents=True)
    (outer / "package.json").write_text('{"name": "outer"}', encoding="utf-8")
    (project / ".git").mkdir()
    (source / "bad.ts").write_text("this.status = 'paid';\n", encoding="utf-8")

    result = _run("--dir", str(source), "--strict", "--json")

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "skipped"
    assert payload["manifest"] == ""
    assert payload["files_scanned"] == 0


def test_backend_checker_manifest_precedence(tmp_path):
    """Same-level go.mod + package.json: go.mod wins (fixed precedence)."""
    repo = tmp_path / "mono"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")
    (repo / "package.json").write_text('{"name": "x"}', encoding="utf-8")
    (repo / "bad.go").write_text("this.status = 'paid';\n", encoding="utf-8")
    (repo / "bad.ts").write_text("this.status = 'paid';\n", encoding="utf-8")
    result = _run("--dir", str(repo), "--strict")
    assert result.returncode == 1
    assert "bad.go" in result.stderr
    assert "bad.ts" not in result.stderr


def test_backend_checker_manifest_flag_known(tmp_path):
    """--manifest go asserts the language even without a go.mod file;
    0 files under a forced manifest still fails closed."""
    repo = tmp_path / "no-manifest"
    repo.mkdir()
    (repo / "bad.go").write_text("this.status = 'paid';\n", encoding="utf-8")
    result = _run("--dir", str(repo), "--strict", "--manifest", "go")
    assert result.returncode == 1
    assert "bad.go" in result.stderr
    empty = tmp_path / "empty-forced"
    empty.mkdir()
    result = _run("--dir", str(empty), "--strict", "--manifest", "go")
    assert result.returncode == 1
    assert "manifest go.mod (--manifest) found but 0 backend files scanned" \
        in result.stderr


def test_backend_checker_manifest_flag_unknown_union(tmp_path):
    """Unknown --manifest names (e.g. 'python') fall back to the union
    extension set but still fail closed on 0 files."""
    repo = tmp_path / "py-repo"
    repo.mkdir()
    (repo / "app.py").write_text("this.status = 'paid';\n", encoding="utf-8")
    result = _run("--dir", str(repo), "--strict", "--manifest", "python")
    assert result.returncode == 1
    assert "app.py" in result.stderr
    empty = tmp_path / "py-empty"
    empty.mkdir()
    result = _run("--dir", str(empty), "--strict", "--manifest", "python")
    assert result.returncode == 1
    assert "--manifest python: 0 backend files scanned" in result.stderr


def test_backend_checker_dir_json_additive_fields(tmp_path):
    """JSON stays byte-compatible: existing fields unchanged, only
    manifest/scan_error/status are added."""
    repo = tmp_path / "go-repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")
    (repo / "bad.go").write_text("this.status = 'paid';\n", encoding="utf-8")
    result = _run("--dir", str(repo), "--strict", "--json")
    start = result.stdout.index("{")
    decoded, _ = json.JSONDecoder().raw_decode(result.stdout[start:])
    assert decoded["strict"] is True
    assert decoded["files_scanned"] == 1
    assert decoded["total"] == 1
    assert decoded["manifest"] == "go.mod"
    assert decoded["status"] == "ok"
    assert "scan_error" not in decoded
    for v in decoded["violations"]:
        assert "file" in v and "detail" in v


def test_backend_checker_dir_skipped_json_status(clean_repo):
    result = _run("--dir", str(clean_repo), "--strict", "--json")
    start = result.stdout.index("{")
    decoded, _ = json.JSONDecoder().raw_decode(result.stdout[start:])
    assert decoded["status"] == "skipped"
    assert decoded["manifest"] == ""
    assert decoded["files_scanned"] == 0
    assert decoded["total"] == 0


def test_backend_checker_scan_error_json(tmp_path):
    """F4: scan_error path also keeps the --json stdout contract — exactly
    one JSON document, strict-parseable (a summary-line regression would
    make json.loads raise)."""
    missing = tmp_path / "nope"
    result = _run("--dir", str(missing), "--strict", "--json")
    assert result.returncode == 1
    decoded = json.loads(result.stdout)
    assert decoded["status"] == "scan_error"
    assert "directory not found" in decoded["scan_error"]
    assert decoded["total"] == 0


def test_backend_json_stdout_contract(tmp_path, clean_repo):
    """AC1: under --json, stdout is exactly one JSON document on every
    status path (ok / skipped / violation) — no trailing summary lines."""
    def run(*args):
        return _run("--dir", str(args[0]), "--json", *args[1:])

    # (a) ok: manifest + clean source -> exit 0, pure JSON on stdout
    ok = tmp_path / "ok"
    ok.mkdir()
    (ok / "go.mod").write_text("module x\n", encoding="utf-8")
    (ok / "src" / "main.go").parent.mkdir(parents=True)
    (ok / "src" / "main.go").write_text("package main\n\nfunc main() {}\n",
                                          encoding="utf-8")
    result = run(ok)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["total"] == 0
    assert "BACKEND-QUALITY" not in result.stdout
    assert "BACKEND-QUALITY: OK" in result.stderr

    # (b) skipped: no manifest anywhere in the walk-up
    skipped = clean_repo / "empty"
    skipped.mkdir()
    result = run(skipped)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "skipped"
    assert "gate skipped" in result.stderr
    assert "gate skipped" not in result.stdout

    # (c) violation: manifest + offending source -> exit 1, stdout still pure
    viol = tmp_path / "viol"
    viol.mkdir()
    (viol / "go.mod").write_text("module x\n", encoding="utf-8")
    (viol / "src" / "a.go").parent.mkdir(parents=True)
    (viol / "src" / "a.go").write_text("console.log('x');\n", encoding="utf-8")
    result = run(viol)
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["total"] >= 1
    assert "console.log" in result.stderr
    assert "BACKEND-QUALITY" not in result.stdout


def test_backend_skip_flag(clean_repo):
    """AC2: --skip unions with DEFAULT_EXCLUDES (never replaces), matches
    directory segments, and --skip "" is a no-op.

    Negative control: tests/ is a default-excluded segment. A replace
    implementation would drop the defaults and scan tests/x.go -> 2 under
    --skip scratch; the union keeps it excluded -> 1 (asserted below)."""
    repo = clean_repo / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")
    for name in ("src/a.go", "scratch/b.go", "tests/x.go"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("package main\n\nfunc main() {}\n", encoding="utf-8")

    def scanned(*extra):
        result = _run("--dir", str(repo), "--json", *extra)
        assert result.returncode == 0
        return json.loads(result.stdout)["files_scanned"]

    assert scanned() == 2
    assert scanned("--skip", "scratch") == 1
    # both names already in DEFAULT_EXCLUDES: the union keeps them excluded
    assert scanned("--skip", "generated,migrations") == 2
    assert scanned("--skip", "") == 2  # empty value -> no-op


def test_backend_trailing_skip_diagnostic(clean_repo):
    """F1: trailing --skip (no value) is a visible missing-target violation
    (exit 1), never a silent accept; stdout stays strict-parseable JSON."""
    repo = clean_repo / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")
    (repo / "src" / "main.go").parent.mkdir(parents=True)
    (repo / "src" / "main.go").write_text(
        "package main\n\nfunc main() {}\n", encoding="utf-8")
    result = _run("--dir", str(repo), "--json", "--skip")
    assert result.returncode == 1
    payload = json.loads(result.stdout)  # stdout stays one JSON doc
    files = [v["file"] for v in payload["violations"]]
    assert "--skip" in files
    assert "missing target" in result.stderr


def test_backend_skip_explicit_file_immune(tmp_path, clean_repo):
    """F2/C5: --skip filters --dir scans only. Explicit file targets are
    scanned regardless; positional mode (no --dir) is a --skip no-op."""
    repo = clean_repo / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")
    (repo / "src" / "a.go").parent.mkdir(parents=True)
    (repo / "src" / "a.go").write_text(
        "package main\n\nfunc main() {}\n", encoding="utf-8")
    viol = repo / "scratch" / "b.go"
    viol.parent.mkdir(parents=True)
    viol.write_text("package main\n\nfunc main() { console.log('x'); }\n",
                    encoding="utf-8")

    # explicit target under a --skip'd dir: still scanned, still reported
    result = _run("--dir", str(repo), "--skip", "scratch", "--json",
                  str(viol))
    payload = json.loads(result.stdout)
    assert payload["files_scanned"] == 2  # src/a.go (dir) + explicit viol
    assert payload["total"] == 1
    assert any(v["file"] == str(viol) for v in payload["violations"])

    # positional mode: --skip is a no-op, output byte-identical
    with_skip = _run(str(viol), "--skip", "scratch")
    without = _run(str(viol))
    assert with_skip.returncode == without.returncode == 1
    assert with_skip.stdout == without.stdout
    assert with_skip.stderr == without.stderr


def test_backend_skip_all_excluded_fails_closed(clean_repo):
    """F3: --skip cannot manufacture a hollow pass — excluding every source
    under a manifest trips the same empty-scan fail-closed as an empty repo."""
    repo = clean_repo / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")
    (repo / "src" / "a.go").parent.mkdir(parents=True)
    (repo / "src" / "a.go").write_text(
        "package main\n\nfunc main() {}\n", encoding="utf-8")
    (repo / "scratch" / "b.go").parent.mkdir(parents=True)
    (repo / "scratch" / "b.go").write_text(
        "package main\n\nfunc main() {}\n", encoding="utf-8")

    result = _run("--dir", str(repo), "--json", "--skip",
                  "src,scratch,extra")
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "empty"
    assert payload["files_scanned"] == 0
    assert "0 backend files scanned" in result.stderr


def test_backend_flaglike_value_rejected(tmp_path):
    """F1: a flag following a value flag is a usage error (exit 2), never
    silently swallowed. --skip --json must NOT degrade to human mode with
    exit 0; --dir --strict is the same shared-branch guard (was a visible
    scan error, now argparse-family exit 2)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")
    (repo / "src" / "main.go").parent.mkdir(parents=True)
    (repo / "src" / "main.go").write_text("package main\n\nfunc main() {}\n",
                                            encoding="utf-8")

    for args in (("--skip", "--json"), ("--skip", "--strict"),
                 ("--dir", "--strict"), ("--manifest", "--json")):
        result = _run("--dir", str(repo), *args)
        assert result.returncode == 2, args
        assert "expected one argument" in result.stderr, args
        assert "{" not in result.stdout, args  # no JSON, no false pass

    # bare dash is still a legal value (argparse-family convention)
    result = _run("--dir", str(repo), "--skip", "-")
    assert result.returncode == 0


def test_backend_non_json_unchanged(tmp_path):
    """C1: without --json the human summary stays on stdout — the --json
    fix must not leak into non-json runs (byte-identical human mode)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")
    (repo / "src" / "main.go").parent.mkdir(parents=True)
    (repo / "src" / "main.go").write_text("package main\n\nfunc main() {}\n",
                                            encoding="utf-8")
    result = _run("--dir", str(repo))
    assert result.returncode == 0
    assert "BACKEND-QUALITY: OK (1 file(s) scanned)" in result.stdout
    assert "{" not in result.stdout


def test_backend_o_flag_summary_on_stdout(tmp_path):
    """C2: -o without --json keeps the human summary on stdout; the report
    file bytes are the unchanged JSON payload."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")
    (repo / "src" / "main.go").parent.mkdir(parents=True)
    (repo / "src" / "main.go").write_text("package main\n\nfunc main() {}\n",
                                            encoding="utf-8")
    report = tmp_path / "report.json"
    result = _run("--dir", str(repo), "-o", str(report))
    assert result.returncode == 0
    assert "BACKEND-QUALITY: OK" in result.stdout
    assert json.loads(report.read_text(encoding="utf-8"))["total"] == 0


def test_backend_quality_validator_resolves():
    from pbatch.config import _resolve_validators
    commands = _resolve_validators("backendquality")
    assert commands and "check-backend-quality.py --dir" in commands[0]
    assert "--strict" in commands[0]


def test_backend_review_pipeline_schema():
    from pbatch.pipeline import load_pipeline
    pipeline = load_pipeline("examples/backend-engineering-review.yaml")
    assert [s.name for s in pipeline.stages] == [
        "inventory", "engineering_review", "gate", "verdict"]
    assert pipeline.stages[2].validate_cmd == "backendquality"
    assert pipeline.stages[3].gate is True


def test_backend_role_keywords_registered():
    from pbatch.relevance import load_role_keywords
    keywords = load_role_keywords()
    for role in ("backend_engineer", "design_pattern_reviewer"):
        assert role in keywords
        assert keywords[role]


def test_backend_spec_files_exist():
    for file in ("architecture.md", "design-patterns.md",
                 "domain-modeling.md", "evolution.md", "rules.yaml"):
        assert (__import__("pathlib").Path("backend-specs") / file).exists()


def test_algorithms_rule_triggered_by_scenario():
    matched = match_rules("写一个商品搜索接口，Top K 排行，用 go 实现")
    ids = [item["id"] for item in matched["rules"]]
    assert "algorithms" in ids


def test_algorithms_rule_not_triggered_by_plain_crud():
    matched = match_rules("做一个客户管理的 CRUD 接口")
    ids = [item["id"] for item in matched["rules"]]
    assert "algorithms" not in ids


def test_ddd_and_complexity_only_at_production_tier():
    matched = match_rules("企业订单域：多实体复杂流程，微服务生产环境，含支付审批")
    ids = [item["id"] for item in matched["rules"]]
    assert matched["tier"] == "production"
    assert {"ddd", "complexity-scale", "evolution"} <= set(ids)
    small = match_rules("写一个订单接口")
    small_ids = [item["id"] for item in small["rules"]]
    assert "ddd" not in small_ids


def test_oop_di_rule_at_standard_tier():
    matched = match_rules("写一个订单创建接口")
    ids = [item["id"] for item in matched["rules"]]
    assert "oop-di" in ids


def test_backend_classifier_business_terms():
    from pbatch.classifier import classify_text
    assert classify_text("企业订单域：多实体复杂流程，微服务生产环境").task_type == "backend"
    assert classify_text("做一个订单支付页面").task_type == "frontend_ui"


def test_checker_n_plus_one_warning(tmp_path):
    bad = tmp_path / "svc.ts"
    bad.write_text(
        "for (const order of orders) {\n"
        "  const customer = await customerRepo.findById(order.customerId);\n"
        "}\n", encoding="utf-8")
    assert _run(str(bad)).returncode == 0  # warnings under strict only
    strict = _run(str(bad), "--strict")
    assert strict.returncode == 1
    assert "N+1 I/O risk" in strict.stderr


def test_checker_full_sort_topk_warning(tmp_path):
    bad = tmp_path / "rank.ts"
    bad.write_text("const top = rows.sort((a, b) => b.score - a.score).slice(0, 10);\n",
                   encoding="utf-8")
    strict = _run(str(bad), "--strict")
    assert strict.returncode == 1
    assert "Top K should use a heap" in strict.stderr


def test_backend_spec_registry_integrity():
    import yaml
    data = yaml.safe_load(Path("backend-specs/rules.yaml").read_text(encoding="utf-8"))
    missing = []
    for rule_id, rule in data["rules"].items():
        for file in rule.get("files", []):
            if not Path(file).exists():
                missing.append(f"{rule_id}: {file}")
    assert not missing, missing


def test_persistence_rule_risk_mandated():
    matched = match_rules("写一个订单创建接口，含支付")
    ids = [item["id"] for item in matched["rules"]]
    assert "persistence-modeling" in ids
    required = {item["id"] for item in matched["rules"] if item["required"]}
    assert "persistence-modeling" in required


def test_persistence_rule_not_at_demo_tier():
    matched = match_rules("做一个客户管理的 CRUD 接口")
    ids = [item["id"] for item in matched["rules"]]
    assert "persistence-modeling" not in ids


def test_production_rules_at_production_tier():
    matched = match_rules("企业订单域：多实体复杂流程，微服务生产环境，含支付审批")
    ids = [item["id"] for item in matched["rules"]]
    assert {"production-readiness", "persistence-modeling",
            "agent-guardrails"} <= set(ids)


def test_checker_float_money_error(tmp_path):
    sql = tmp_path / "schema.sql"
    sql.write_text("CREATE TABLE t (price DOUBLE, amount FLOAT);\n", encoding="utf-8")
    result = _run(str(sql), "--strict")
    assert result.returncode == 1
    assert "money stored as float/double" in result.stderr


def test_checker_select_star_and_tenant(tmp_path):
    sql = tmp_path / "q.sql"
    sql.write_text("SELECT * FROM orders WHERE id = ?;\n", encoding="utf-8")
    result = _run(str(sql), "--strict")
    assert result.returncode == 1
    assert "SELECT *" in result.stderr
    assert "tenant scoping" in result.stderr


def test_backend_engineer_prompt_requires_persistence_report():
    prompt = Path("prompts/backend_engineer.md").read_text(encoding="utf-8")
    assert "Persistence Design" in prompt
    assert "four models" in prompt.lower() or "API DTO" in prompt


def test_constitution_rule_production_tier_only():
    matched = match_rules("企业订单域：多实体复杂流程，微服务生产环境，含支付审批")
    ids = [item["id"] for item in matched["rules"]]
    assert "architecture-constitution" in ids
    crud = match_rules("做一个客户管理的 CRUD 接口")
    crud_ids = [item["id"] for item in crud["rules"]]
    assert "architecture-constitution" not in crud_ids


def test_checker_swallowed_exception(tmp_path):
    bad = tmp_path / "svc.ts"
    bad.write_text("try { risky(); } catch (e) {}\n", encoding="utf-8")
    result = _run(str(bad), "--strict")
    assert result.returncode == 1
    assert "swallowed exception" in result.stderr


def test_checker_http_client_reuse(tmp_path):
    bad = tmp_path / "api.ts"
    bad.write_text("const a = axios.create({});\nconst b = axios.create({});\n",
                   encoding="utf-8")
    result = _run(str(bad), "--strict")
    assert result.returncode == 1
    assert "HTTP client creations" in result.stderr


def test_checker_busy_polling(tmp_path):
    bad = tmp_path / "poll.ts"
    bad.write_text("while (true) { await sleep(1000); check(); }\n", encoding="utf-8")
    result = _run(str(bad), "--strict")
    assert result.returncode == 1
    assert "busy-polling" in result.stderr


def test_constitution_reviewer_keywords_registered():
    from pbatch.relevance import load_role_keywords
    keywords = load_role_keywords()
    assert "architecture_constitution_reviewer" in keywords
    assert keywords["architecture_constitution_reviewer"]


def test_constitution_prompt_has_15_questions():
    prompt = Path("prompts/architecture_constitution_reviewer.md").read_text(encoding="utf-8")
    assert "blast radius" in prompt.lower()
    assert "coupling octet" in prompt.lower()


def test_testing_rule_at_standard_tier():
    matched = match_rules("写一个订单创建接口")
    ids = [item["id"] for item in matched["rules"]]
    assert "testing" in ids


def test_system_and_network_rules_production_only():
    matched = match_rules("企业订单域：多实体复杂流程，微服务生产环境，含支付审批")
    ids = [item["id"] for item in matched["rules"]]
    assert {"system-engineering", "network-engineering"} <= set(ids)
    small = match_rules("做一个客户 CRUD 接口")
    small_ids = [item["id"] for item in small["rules"]]
    assert "network-engineering" not in small_ids


def test_checker_assertion_less_test_warning(tmp_path):
    bad = tmp_path / "x.test.ts"
    bad.write_text("it('renders', () => { render(<App />); });\n", encoding="utf-8")
    strict = _run(str(bad), "--strict")
    assert strict.returncode == 1
    assert "without assertions" in strict.stderr
    good = tmp_path / "y.test.ts"
    good.write_text("it('adds', () => { expect(add(1,2)).toBe(3); });\n",
                    encoding="utf-8")
    assert _run(str(good), "--strict").returncode == 0


def test_testing_reviewer_keywords_registered():
    from pbatch.relevance import load_role_keywords
    keywords = load_role_keywords()
    assert "testing_reviewer" in keywords
    assert keywords["testing_reviewer"]


def test_backend_implementation_pipeline_schema():
    from pbatch.pipeline import load_pipeline
    pipeline = load_pipeline("examples/backend-implementation-pipeline.yaml")
    assert [s.name for s in pipeline.stages] == ["plan", "implement", "review", "gate"]
    assert pipeline.stages[1].validate_cmd == "backendquality,completion"
    assert pipeline.stages[2].meta is True
    assert pipeline.stages[3].gate is True
    plan_prompt = pipeline.stages[0].from_prompt
    assert "Persistence Design" in plan_prompt
    assert "pi-batch context" in plan_prompt
    assert "pi-batch assess" in plan_prompt


def test_classifier_backend_pipeline_configured():
    from pbatch import config
    assert config.CLASSIFIER_BACKEND_PIPELINE
    assert config.CLASSIFIER_BACKEND_PIPELINE.endswith("backend-implementation-pipeline.yaml")


def test_backend_routing_pipeline_exists():
    from pathlib import Path
    assert Path("examples/backend-implementation-pipeline.yaml").exists()


def test_checker_except_pass_severity_split(tmp_path):
    """OSError cleanup pass = warning (strict only); non-OSError pass =
    error (always); commented pass = clean."""
    os_cleanup = tmp_path / "cleanup.py"
    os_cleanup.write_text("try:\n    os.unlink(f)\nexcept OSError:\n    pass\n",
                          encoding="utf-8")
    assert _run(str(os_cleanup)).returncode == 0          # warning only
    assert _run(str(os_cleanup), "--strict").returncode == 1
    business = tmp_path / "business.py"
    business.write_text("try:\n    parse()\nexcept ValueError:\n    pass\n",
                        encoding="utf-8")
    assert _run(str(business)).returncode == 1            # error always
    assert "swallowing non-OSError" in _run(str(business)).stderr
    commented = tmp_path / "commented.py"
    commented.write_text("except ValueError:\n    pass  # stream closed\n",
                         encoding="utf-8")
    assert _run(str(commented), "--strict").returncode == 0
