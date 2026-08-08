"""Regression tests for the eval-suite expect-fallback machinery
(direction 85368cf1: execute expect-only eval cases instead of passing
them vacuously).

All crafted suites live in a monkeypatched EVAL_DIR (tmp_path) — the real
evals/ directory is only read (never written) by the subprocess-based
filter test, which implements acceptance A1.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _write_suite(tmp_path, name: str, body: str) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def patched_eval_dir(tmp_path, monkeypatch):
    """Point pbatch.eval.EVAL_DIR at a temp dir holding crafted suites."""
    monkeypatch.setattr("pbatch.eval.EVAL_DIR", tmp_path)
    return tmp_path


def test_system_type_key_dispatches_to_classify_text(patched_eval_dir, capsys):
    """expect.system_type executes against classify_text (R2, A1)."""
    _write_suite(patched_eval_dir, "tmp", """\
name: tmp-suite
cases:
  - id: system-type-check
    input: "订单审批流，状态流转，审核流程"
    expect:
      system_type: state-machine
""")
    from pbatch import eval as eval_mod
    eval_mod.eval_main(["--filter", "system-type-check", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload == {"total": 1, "failed": 0, "failures": []}

    # Same input with a wrong expectation → assertion FAIL → exit 1.
    _write_suite(patched_eval_dir, "tmp", """\
name: tmp-suite
cases:
  - id: system-type-check
    input: "订单审批流，状态流转，审核流程"
    expect:
      system_type: knowledge
""")
    with pytest.raises(SystemExit) as exc:
        eval_mod.eval_main(["--filter", "system-type-check", "--json"])
    assert exc.value.code == 1


def test_includes_excludes_dispatch_to_match_rules(patched_eval_dir, capsys):
    """expect.includes/excludes share has_rule/lacks_rule rule-id semantics
    (R2, A2)."""
    _write_suite(patched_eval_dir, "tmp", """\
name: tmp-suite
cases:
  - id: di-login
    input: "生成一个登录页 demo"
    expect:
      includes: [design-intelligence-product, design-intelligence-priority]
      excludes: [design-intelligence-emotional]
""")
    from pbatch import eval as eval_mod
    eval_mod.eval_main(["--filter", "di-login", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload == {"total": 1, "failed": 0, "failures": []}

    # A backend rule id that the demo input never matches → FAIL → exit 1.
    _write_suite(patched_eval_dir, "tmp", """\
name: tmp-suite
cases:
  - id: di-login
    input: "生成一个登录页 demo"
    expect:
      includes: [design-intelligence-api]
""")
    with pytest.raises(SystemExit) as exc:
        eval_mod.eval_main(["--filter", "di-login", "--json"])
    assert exc.value.code == 1


def test_vacuous_case_exits_2(patched_eval_dir, capsys, caplog):
    """A case with only display-only expect keys fails closed with exit 2,
    naming suite and case (R3, A3)."""
    _write_suite(patched_eval_dir, "tmp", """\
name: tmp-suite
cases:
  - id: bare-expect
    input: "写一个订单创建接口"
    expect:
      task_type: backend
""")
    from pbatch import eval as eval_mod
    from pbatch.eval import VacuousCaseError, run_suite

    # run_suite raises a typed error naming suite + case.
    suite = {"name": "tmp-suite", "path": "tmp.yaml",
             "cases": [{"id": "bare-expect", "input": "x",
                        "expect": {"task_type": "backend"}}]}
    with pytest.raises(VacuousCaseError) as exc:
        run_suite(suite)
    assert "tmp-suite" in str(exc.value) and "bare-expect" in str(exc.value)

    # eval_main maps it to exit 2 (schema error, distinct from exit 1).
    with pytest.raises(SystemExit) as exc:
        eval_mod.eval_main(["--json"])
    assert exc.value.code == 2
    assert any("EVAL schema error" in r.message for r in caplog.records)
    # F-7: exit 2 never emits a JSON payload (schema errors are not a run
    # result — exit 1 does print {total, failed, failures}).
    assert "{" not in capsys.readouterr().out


def test_filter_no_match_exits_2(patched_eval_dir, capsys, caplog):
    """AC1a (fail-closed direction): --filter matching zero cases used to
    report {"total": 0, "failed": 0} with exit 0 (silent pass in CI); it
    now exits 2, names the filter on stderr, and leaves stdout clean
    (F-7: exit 2 never emits a JSON payload)."""
    _write_suite(patched_eval_dir, "tmp", """\
name: tmp-suite
cases:
  - id: di-login
    input: "生成一个登录页 demo"
    expect:
      includes: [design-intelligence-product, design-intelligence-priority]
""")
    from pbatch import eval as eval_mod
    with pytest.raises(SystemExit) as exc:
        eval_mod.eval_main(["--filter", "no-such", "--json"])
    assert exc.value.code == 2
    assert any("no-such" in r.message for r in caplog.records)
    assert "{" not in capsys.readouterr().out


def test_filter_no_match_subprocess_exits_2():
    """AC1b: real evals/ + `python -m pbatch eval --filter no-such-case
    --json` → exit 2, stdout carries no JSON payload, stderr names the
    filter (read-only subprocess, mirrors test_filter_targets_renamed_cases
    harness)."""
    result = subprocess.run(
        [sys.executable, "-m", "pbatch", "eval",
         "--filter", "no-such-case", "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 2, result.stderr
    assert "{" not in result.stdout
    assert "no-such-case" in result.stderr


def test_quick_filter_hitting_skipped_suite_exits_2(patched_eval_dir, caplog):
    """FR-4a: one guard closes the --quick variant of the same hole — a
    filter matching a case in a quick-skipped suite (e.g. assess) leaves
    the executed set empty and fails closed with exit 2."""
    _write_suite(patched_eval_dir, "assess", """\
name: assess
cases:
  - id: l3-prescription-no-billing-without-signal
    input: "做一个开源上传产品，断点续传，商业化"
    assert:
      product_level: L3_product
""")
    from pbatch import eval as eval_mod
    with pytest.raises(SystemExit) as exc:
        eval_mod.eval_main(["--quick", "--filter",
                            "l3-prescription-no-billing-without-signal"])
    assert exc.value.code == 2
    assert any("l3-prescription-no-billing-without-signal" in r.message
               for r in caplog.records)


def test_quick_filter_matching_core_case_still_passes(patched_eval_dir, capsys):
    """FR-4b: a valid --quick --filter run (case in a quick suite) is not
    touched by the guard — exit 0, total exactly 1."""
    _write_suite(patched_eval_dir, "classifier", """\
name: classifier
cases:
  - id: system-type-state-machine
    input: "订单审批流，状态流转，审核流程"
    expect:
      system_type: state-machine
""")
    from pbatch import eval as eval_mod
    eval_mod.eval_main(["--quick", "--filter", "system-type-state-machine",
                        "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload == {"total": 1, "failed": 0, "failures": []}


def test_empty_eval_dir_exits_2(patched_eval_dir, capsys, caplog):
    """Edge: an empty evals/ directory (no yaml files) used to report
    'all 0 passed' with exit 0 — same bug class as the filter typo; now
    fails closed with exit 2 and no stdout payload."""
    from pbatch import eval as eval_mod
    with pytest.raises(SystemExit) as exc:
        eval_mod.eval_main(["--json"])
    assert exc.value.code == 2
    assert any("executed 0" in r.message for r in caplog.records)
    assert "{" not in capsys.readouterr().out


def test_assert_empty_dict_uses_expect_fallback(patched_eval_dir):
    """`assert: {}` is treated as absent (module docstring contract):
    executable expect keys run — a deliberately wrong expect fails with
    exit 1 (fallback executed); display-only expect alone stays vacuous
    and exits 2. Pins F-3; the design doc's "assert: {} wins" claim is
    false vs shipped code."""
    from pbatch import eval as eval_mod
    _write_suite(patched_eval_dir, "tmp", """\
name: tmp-suite
cases:
  - id: empty-assert-expect
    input: "订单审批流，状态流转，审核流程"
    assert: {}
    expect:
      system_type: knowledge
""")
    with pytest.raises(SystemExit) as exc:
        eval_mod.eval_main(["--filter", "empty-assert-expect", "--json"])
    assert exc.value.code == 1  # fallback executed and failed loudly

    _write_suite(patched_eval_dir, "tmp", """\
name: tmp-suite
cases:
  - id: empty-assert-display
    input: "写一个订单创建接口"
    assert: {}
    expect:
      task_type: backend
""")
    with pytest.raises(SystemExit) as exc:
        eval_mod.eval_main(["--filter", "empty-assert-display", "--json"])
    assert exc.value.code == 2  # no executable keys left → vacuous


RENAMED_IDS = [
    # 6 classifier system_type cases (R4).
    "system-type-state-machine", "system-type-event-driven",
    "system-type-search", "system-type-optimization",
    "system-type-knowledge", "system-type-default",
    # 4 rules includes/excludes cases (R5).
    "design-intelligence-production-workbench",
    "design-intelligence-demo-login",
    "design-intelligence-backend-production",
    "system-type-methodology-routing",
]


@pytest.mark.parametrize("case_id", RENAMED_IDS)
def test_filter_targets_renamed_cases(case_id):
    """A1/A2 acceptance (F-6 pin): --filter targets every renamed case in
    the real evals/ suite, total exactly 1 (read-only subprocess)."""
    result = subprocess.run(
        [sys.executable, "-m", "pbatch", "eval",
         "--filter", case_id, "--json"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert payload == {"total": 1, "failed": 0, "failures": []}


def test_load_eval_files_rejects_id_less_case(tmp_path, monkeypatch):
    """Schema guard (F-6): a case without a non-empty id is rejected at
    load with exit 2 — a case that cannot be --filter targetable would
    otherwise silently report as '?' and hide regressions."""
    _write_suite(tmp_path, "tmp", """\
name: tmp-suite
cases:
  - name: legacy-name-keyed
    input: "写一个订单创建接口"
    assert:
      task_type: backend
""")
    monkeypatch.setattr("pbatch.eval.EVAL_DIR", tmp_path)
    from pbatch import eval as eval_mod
    with pytest.raises(SystemExit) as exc:
        eval_mod.load_eval_files()
    assert exc.value.code == 2


def test_load_eval_files_rejects_malformed_assert(tmp_path, monkeypatch):
    """Schema guard (architecture review F2): a non-mapping assert used to
    leak an AttributeError as exit 1; it is now a loud exit-2 schema error."""
    _write_suite(tmp_path, "tmp", """\
name: tmp-suite
cases:
  - id: bad-assert
    input: "写一个订单创建接口"
    assert:
      - task_type
""")
    monkeypatch.setattr("pbatch.eval.EVAL_DIR", tmp_path)
    from pbatch import eval as eval_mod
    with pytest.raises(SystemExit) as exc:
        eval_mod.load_eval_files()
    assert exc.value.code == 2


def test_expect_inert_when_assert_present(patched_eval_dir, capsys):
    """assert wins; expect stays inert even when deliberately wrong
    (R1 precedence)."""
    _write_suite(patched_eval_dir, "tmp", """\
name: tmp-suite
cases:
  - id: assert-wins
    input: "写一个订单创建接口，含支付审批，生产环境，go"
    assert:
      task_type: backend
    expect:
      system_type: knowledge
""")
    from pbatch import eval as eval_mod
    eval_mod.eval_main(["--filter", "assert-wins", "--json"])
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{"):])
    assert payload == {"total": 1, "failed": 0, "failures": []}
