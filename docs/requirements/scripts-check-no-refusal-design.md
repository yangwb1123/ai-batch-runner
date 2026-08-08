# Design — check-no-refusal: Regression Coverage + Duplicate Elimination

- **Implements**: `docs/requirements/scripts-check-no-refusal-regression.md` (FR-1…FR-4, DOC-1)
- **Status**: Design — ready for review; every acceptance mapped to a runnable check (§8)
- **Verification date**: 2026-08-08 (all evidence claims independently re-verified against the tree)

---

## 1. Evidence Verification Ledger

All claims in the source evidence were treated as **untrusted** and re-verified. Result: **every claim confirmed**, plus two findings the evidence missed.

| # | Claim | Verdict | Verification |
|---|-------|---------|--------------|
| E1 | `scripts/check-no-refusal.py` fail-closed gate: exit 0/1/2, 10 markers, `REFUSAL-CHECK` diagnostics | ✅ | Read source (44 lines): exit 0 only for non-empty marker-free artifact; 1 for missing/empty/markers; 2 for no-arg usage. Markers = 10 (incl. `"FAILED — 输入"` ⊂ `"FAILED — 输入缺失"` — harmless double-hit) |
| E2 | Byte-identical duplicate at `examples/snaplink-platform/scripts/check-no-refusal.py` | ✅ | `diff` IDENTICAL; both 1414 bytes; both git-tracked |
| E3 | Zero test coverage in `tests/`, `checks/` | ✅ | `grep -rn "no_refusal\|no-refusal" tests/ checks/` → exit 1, no matches |
| E4 | Not in `pi-batch.yaml` validators | ✅ | `yaml.safe_load` → exactly 23 entries; no refusal entry (`pi-batch.yaml:134-180`) |
| E5 | Load-bearing: 8 `validate_cmd` sites, 4 pipelines | ✅ | audit-revision:37,56,72,93,112 (5) + audit-fix:38 + requirement:44 + audit-v21:41 = 8, all `{cwd}/scripts/check-no-refusal.py {output}` |
| E6 | `{cwd}` resolves to task workdir → repo root → canonical copy | ✅ | `pbatch/runner.py:487`: `{cwd}` → `shlex.quote(task.workdir())`; `pbatch/pipeline.py:604`: no `cwd:` → `os.getcwd()`; example README documents repo-root invocation |
| F1 | `pi-batch check` does not enumerate validator registry | ✅ | `pbatch/selfcheck.py:36-53`: steps = quality, registry, org, capabilities, graph, tools, knowledge (+eval unless `--skip-eval`) |
| F2 | `pbatch.config.VALIDATORS` importable for tests | ✅ | `pbatch/config.py:419-430`; lookup order `PBATCH_SCRIPT_DIR` → package dir → cwd; resolves under `python -m pytest` from repo root |
| F3 | Marker list content | ✅ | Exactly the 10 listed; `runpy.run_path` extraction works and does **not** execute `main()` (`__name__` = `<run_path>`) |
| F4 | `requirement-pipeline.yaml:44` uses `python`, others `python3` | ✅ | Interpreter inconsistency confirmed |
| F5 | Example copy has no documented consumer | ✅ | Grep over py/yaml/md/sh: only the requirement/run artifacts mention it; no invocation |
| F6 | Suite conventions | ✅ | `tests/test_entrypoints.py` subprocess style, `timeout=120`; pyproject testpaths `["tests","checks"]`; `make test` = `python -m pytest tests/`; `make ci` = check self-test test quality org |

### Additional findings (beyond the evidence)

- **FIND-1 (stale doc)**: `AGENTS.md:70` says "6 个验证器（含 `pyquality`）" but the registry has **23** — the count line is already stale by 17. DOC-1 must set the truthful post-change count (**24**), not "7".
- **FIND-2 (no-op trap)**: `pi-batch check --steps <unknown-name> --skip-eval` currently exits **0** with "CHECK: all 0 gates passed" — the `--steps` filter silently degrades to an empty run. A4.3 ("never a no-op print") is only *behaviorally* caught by A4.2's output assertion; the systemic defense is to reject unknown step names (exit 2). Small, safe change; included in §4.
- **FIND-3 (existing tests pin selfcheck)**: `tests/test_org.py:405-437` asserts step membership, calls `_run_step` directly, and runs `check --steps org,capabilities,tools --skip-eval` expecting "all 3 gates passed". The design must keep `_run_step`'s signature and the filter semantics for known names — it does (§4.2).
- **FIND-4 (no conftest)**: `tests/` has no conftest; `from pbatch import …` works only because `make test` runs `python -m pytest` (cwd = repo root on `sys.path`). New tests must follow the same convention (no path hacking).

---

## 2. Design Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | `refusal` step is an **in-process callable** step in `pbatch/selfcheck.py` (not a subprocess command) | Single source of logic, directly unit-testable for A4.3 (fail-closed), no duplicated inline `python -c` source, prints `CHECK [refusal] …` lines matching `_run_step`'s style |
| D2 | `--steps` unknown names → **exit 2** | Closes the FIND-2 no-op class systemically; A4.3's "never a no-op" becomes structural, not just behavioral |
| D3 | FR-2 **deletion branch** (spec §6.1 default); byte-identity fallback documented with its exact alternative test (§8 note) | Zero documented consumers (F5); `{cwd}` already targets the canonical copy (E6) |
| D4 | Markers derived via `runpy.run_path` (A1.1/A1.6) | Auto-syncs with the script; a purge shrinks parametrization *and* trips the ≥10 guard |
| D5 | Registry entry frozen to spec text: `python {cwd}/scripts/check-no-refusal.py {output}` | Matches the `completion` entry's `python` style; `python3` equally valid but the spec pins `python` |
| D6 | Gate contract (NG1) untouched: **zero edits** to `scripts/check-no-refusal.py` | The tests pin it; any future change to markers/exit codes fails loudly instead of drifting |

---

## 3. API Changes (exact surface)

### 3.1 `pi-batch.yaml` — one additive validator entry

```yaml
  # Fail-closed refusal gate: LLM "cannot proceed" artifacts are failures.
  refusalcheck: "python {cwd}/scripts/check-no-refusal.py {output}"
```

Insert at the end of the `validators:` block (after `completion`). Count: 23 → **24**.

### 3.2 `pbatch/selfcheck.py` — three changes

**(a) New function** (in-process step, unit-testable; ~45 lines, within quality budget):

```python
def _run_refusal_step(validators=None, script=None) -> bool:
    """`refusal` gate: registry entry + canonical script + 3 smoke runs.

    Fail closed: any missing/broken piece returns False — never a no-op
    print (A4.3). Parameters exist for unit tests; defaults read the live
    config so the step is always wired to reality.
    """
    from pbatch import config as pbconfig
    validators = pbconfig.VALIDATORS if validators is None else validators
    script = (Path(__file__).resolve().parents[1] / "scripts"
              / "check-no-refusal.py") if script is None else Path(script)
    entry = validators.get("refusalcheck")
    if not entry:
        print("CHECK [refusal] FAIL: 'refusalcheck' missing from pi-batch.yaml "
              "validator registry", file=sys.stderr)
        return False
    print(f"CHECK [refusal] registry entry: {entry}")
    if not script.is_file():
        print(f"CHECK [refusal] FAIL: script not found: {script}", file=sys.stderr)
        return False
    print(f"CHECK [refusal] script: {script}")
    smoke = [
        ("clean", "这是一份正常的阶段产物，内容完整。", 0),
        ("marker", "无法继续：上游输入缺失", 1),   # fixed marker, stable (script docstring)
        ("missing", None, 1),
    ]
    with tempfile.TemporaryDirectory() as td:
        for label, body, want in smoke:
            path = Path(td) / f"{label}.md"
            if body is not None:
                path.write_text(body, encoding="utf-8")
            try:
                r = subprocess.run([sys.executable, str(script), str(path)],
                                   capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"CHECK [refusal] FAIL: {label} smoke run: {exc}", file=sys.stderr)
                return False
            ok = r.returncode == want
            print(f"CHECK [refusal] {label}: exit {r.returncode} (want {want})"
                  + (" OK" if ok else " FAIL"))
            if not ok:
                return False
    print("CHECK [refusal] OK")
    return True
```

**(b) Register the step** — in `_default_steps`, before the `eval` block:

```python
    steps.append(("refusal", _run_refusal_step))
```

(Add after `knowledge`, before `if not args.skip_eval:`.) The step then runs in the default `pi-batch check` set (FR-4) and is filterable via `--steps refusal`. Note: `make ci` does **not** run `pi-batch check` (Makefile: `ci: check self-test test quality org`), so the three ~10 ms smoke runs add zero CI wall-time.

**(c) check_main loop + unknown-name rejection**:

```python
    steps = _default_steps(args)
    if args.steps:
        wanted = {name.strip() for name in args.steps.split(",") if name.strip()}
        known = {item[0] for item in _default_steps(args)} | {"eval"}
        unknown = wanted - known
        if unknown:
            print(f"CHECK: unknown step(s): {', '.join(sorted(unknown))}",
                  file=sys.stderr)
            sys.exit(2)
        steps = [item for item in steps if item[0] in wanted]
    failed = []
    for name, step in steps:
        ok = step() if callable(step) else _run_step(name, step)
        if not ok:
            failed.append(name)
    if failed:
        print(f"CHECK: failed: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)
    print(f"CHECK: all {len(steps)} gates passed")
```

- `"eval"` is unioned into `known` so `--steps eval --skip-eval` keeps today's behavior (0 gates, exit 0).
- `test_check_cli_full` (`--steps org,capabilities,tools --skip-eval` → "all 3 gates passed") is preserved: all three are known names and remain command-list steps.
- `_run_step(name, command)` signature untouched (existing direct-call test `test_check_run_step_ok` keeps passing).

### 3.3 `tests/test_no_refusal.py` — new file (pytest; ~180 lines; within quality budget)

Structure (imports: `runpy`, `re`, `subprocess`, `sys`, `argparse`, `Path`, `pytest`; helpers `_gate(*paths)`, `_markers()`):

| Test | Acceptance | Key assertions |
|---|---|---|
| `test_clean_artifact_passes` | A1.4, A1.7 | rc 0; `REFUSAL-CHECK: OK` in stdout |
| `test_marker_refuses[marker]` — `@pytest.mark.parametrize("marker", _markers(), ids=lambda m: m[:12])` | A1.1, A1.7 | rc 1; `REFUSAL-CHECK` and `failing closed` in stdout; fixture = prose + exactly one marker |
| `test_marker_list_not_purged` | A1.6 | `len(_markers()) >= 10` |
| `test_empty_output_refuses` | A1.2 | rc 1; `empty output` |
| `test_whitespace_only_refuses` (`"\n\t  \n"`) | A1.2 | rc 1; `empty output` |
| `test_missing_file_refuses` | A1.3 | rc 1; `artifact missing` |
| `test_usage_without_args` (`_gate()` with no path) | A1.5 | rc 2; `usage:` |
| `test_no_duplicate_copy` | A2.1 | `not EXAMPLE_COPY.exists()` |
| `test_pipelines_reference_canonical_copy` | A2.2 | Exactly **8** lines containing `check-no-refusal` across the 4 pipeline YAMLs, each matching `^\s*validate_cmd:\s*"python3? \{cwd\}/scripts/check-no-refusal\.py \{output\}"\s*$` |
| `test_examples_tree_only_validate_cmd_refs` | A2.3 | Walk `examples/` (yaml/yml/md/py/sh); every line containing `check-no-refusal` matches the same regex (only the 8 validate_cmd strings remain) |
| `test_registered_in_validators` | A3.1, A3.2 | `"refusalcheck" in pbatch.config.VALIDATORS`; cmd contains `scripts/check-no-refusal.py`; `"examples/" not in cmd` |
| `test_refusal_step_in_default_steps` | FR-4 (structural) | `"refusal" in [n for n,_ in selfcheck._default_steps(Namespace(skip_eval=True, quick_eval=False, steps=""))]` |
| `test_refusal_step_pass_with_live_config` | FR-4 | `selfcheck._run_refusal_step() is True` |
| `test_refusal_step_fails_closed_without_registry_entry` | A4.3 | `_run_refusal_step(validators={}, script=SCRIPT) is False` |
| `test_refusal_step_fails_closed_without_script` | A4.3 | `_run_refusal_step(validators={"refusalcheck": "…"}, script=ROOT/"scripts"/"nope.py") is False` |
| `test_pi_batch_check_lists_gate` | A4.2 | `subprocess.run([sys.executable, "pi-batch.py", "check", "--steps", "refusal", "--skip-eval"], cwd=ROOT, timeout=120)` → rc 0 **and** `CHECK [refusal]` in stdout |
| `test_check_rejects_unknown_step` | FIND-2/D2 | same invocation with `--steps not_a_real_step` → rc **2** |

All subprocess calls `timeout=120`, cwd = repo root, `capture_output=True, text=True` (mirrors `tests/test_entrypoints.py`). `_markers()` = `list(runpy.run_path(str(SCRIPT))["REFUSAL_MARKERS"])` — safe: `main()` does not execute under `run_path` (verified).

**Optional DOC-1 hardening** (spec says reviewer read-through suffices; if desired, one test): `test_agents_md_validator_count_matches` — parse `pi-batch.yaml` with `yaml.safe_load` (PyYAML is a hard dep, pyproject line 11), `len(validators) == 24`, regex `(\d+) 个验证器` in `AGENTS.md` equals the count. This also fixes FIND-1 permanently instead of once.

### 3.4 Deletion — one `git rm`

```
git rm examples/snaplink-platform/scripts/check-no-refusal.py
```

### 3.5 `AGENTS.md:70` — count line

```
| `pi-batch.yaml` | 声明式配置：agent 二进制、会话标志、6 个验证器（含 `pyquality`） |
```
→
```
| `pi-batch.yaml` | 声明式配置：agent 二进制、会话标志、24 个验证器（含 `pyquality`、`refusalcheck`） |
```

### 3.6 Optional (spec §6.2) — one-token normalization

`examples/snaplink-platform/requirement-pipeline.yaml:44`: `python {cwd}/…` → `python3 {cwd}/…`. Does not affect any acceptance (A2.2 regex accepts both).

---

## 4. Compatibility Constraints

1. **Gate contract frozen (NG1)**: `scripts/check-no-refusal.py` gets **zero** edits. Exit codes 0/1/2, the 10 markers, and the `REFUSAL-CHECK:` diagnostic strings are pinned by A1.x. Any future behavior change fails the suite by design.
2. **Validator registry**: purely additive (`refusalcheck`). No renames, no reordering, no edits to the 23 existing entries. `{cwd}`/`{output}` placeholders are already substituted by `pbatch/runner.py:487`. The stale `config` entry is explicitly out of scope (NG2).
3. **selfcheck**: `_run_step(name, command)` signature unchanged; the 8 existing command-list steps behave identically; `--skip-eval`/`--quick-eval` semantics unchanged; `--steps` filtering for **known** names unchanged (including `--steps eval --skip-eval` → 0 gates, exit 0). Only new behavior: unknown step names now exit 2 (was silent exit 0 — deliberate tightening, D2).
4. **Pipeline files (NG3)**: no stage logic/prompt/output changes; `validate_cmd` strings untouched except the optional `python`→`python3` token (3.6). The 8 references continue to resolve to `scripts/check-no-refusal.py` via `{cwd}`.
5. **Deletion blast radius**: only the documented-never invocation breaks — running an example pipeline with cwd inside `examples/snaplink-platform/` (F5). Fallback branch (§8 note) preserves both copies with a byte-identity test if reviewers reject deletion; exactly one branch (deletion XOR identity) is live.
6. **Test environment**: `python -m pytest tests/` from repo root (the Makefile invocation); no conftest path tricks (FIND-4); PyYAML available for the optional A5.1 assertion.
7. **Quality budget**: all new code must pass `quality.py --strict .` (function ≤50 lines, complexity ≤15, file ≤1000 lines) — `_run_refusal_step` is ~45 lines, the test file ~180.
8. **Interpreter note**: the registry entry uses `python` per spec (same exposure as the existing `completion` entry); the repo's own docs and Makefile use `python` throughout — no new class of portability risk.

---

## 5. Failure Modes (with detection)

| # | Failure mode | Detected by | Fails as |
|---|--------------|-------------|---------|
| FM1 | Marker purged from script (list shrinks) | A1.6 (`≥10`) + A1.1 parametrization count | 1+ test failures |
| FM2 | Marker typo / rewritten marker | A1.1 — that marker's parametrized case exits 0 | 1 test failure |
| FM3 | Duplicate copy reintroduced (drift trap returns) | A2.1 | 1 test failure |
| FM4 | Pipeline `validate_cmd` rewritten (relative path, `examples/` path, wrong form) | A2.2 (8 refs, exact form) | 1 test failure |
| FM5 | `refusalcheck` removed from pi-batch.yaml | A3.1 + A4.2 + A4.3 unit + `pi-batch check` step itself (fail-closed) | suite + CLI failure |
| FM6 | `refusal` step removed/renamed in `_default_steps` | `test_refusal_step_in_default_steps`; `--steps refusal` now exits 2 (D2) instead of silently passing 0 gates (FIND-2) | 2 test failures |
| FM7 | Canonical script deleted/moved/renamed | A1.x subprocess failures + selfcheck smoke | entire suite |
| FM8 | Gate semantics changed (exit codes, markers, diagnostics) | A1.x pins each observable; suite turns red — forces a conscious spec amendment (NG1) | targeted failures |
| FM9 | AGENTS.md count drifts again | Optional A5.1 automated assertion (or reviewer) | 1 test failure |
| FM10 | Subprocess hang | `timeout=120` (tests) / `timeout=30` (selfcheck smoke) | TimeoutExpired → fail |
| FM11 | Non-UTF-8 artifact | Script's existing `errors="replace"` (unchanged); tests unaffected | — |
| FM12 | Stray file under `examples/` mentioning `check-no-refusal` | A2.3 tree walk | 1 test failure |
| FM13 | `python` interpreter absent on target host | Same exposure as existing `completion` entry; documented, not new | validator run fails loudly |

---

## 6. Migration Steps (each independently verifiable)

| # | Step | Verify |
|---|------|--------|
| 1 | Add `tests/test_no_refusal.py` (§3.3) | A1.x green; A2.1/A3.1/A4.2 red (expected: duplicate exists, no registry entry, no step) |
| 2 | Add `refusalcheck` to `pi-batch.yaml` (§3.1) | `test_registered_in_validators` green |
| 3 | Edit `pbatch/selfcheck.py` (§3.2 a–c) | `test_refusal_step_*`, `test_pi_batch_check_lists_gate`, `test_check_rejects_unknown_step` green; `tests/test_org.py::test_check_cli_full` still green |
| 4 | `git rm examples/snaplink-platform/scripts/check-no-refusal.py` | `test_no_duplicate_copy`, `test_examples_tree_only_validate_cmd_refs` green |
| 5 | (Optional) normalize `requirement-pipeline.yaml:44` to `python3` | tests still green |
| 6 | Update `AGENTS.md:70` (§3.5); (optional) add A5.1 assertion | optional test green |
| 7 | Full verification (§7) | all green |

Rollback: revert the `git rm` (the file is tracked), revert `pi-batch.yaml`/`selfcheck.py`/`AGENTS.md` hunks — each step is an independent commit-sized unit; no data migration involved.

---

## 7. Verification Plan

```bash
# 1. Targeted suite
python -m pytest tests/test_no_refusal.py -v

# 2. Full CI (387 + ~19 new cases)
make ci

# 3. Self-check lists the gate (A4.1)
python pi-batch.py check --steps refusal --skip-eval      # exit 0, "CHECK [refusal]" lines

# 4. Unknown step rejected (D2)
python pi-batch.py check --steps not_a_real_step; echo $? # exit 2

# 5. Duplicate gone (A2.3)
grep -rn "check-no-refusal" examples/                     # exactly the 8 validate_cmd lines

# 6. Registry count truthful
python -c "import yaml; print(len(yaml.safe_load(open('pi-batch.yaml'))['validators']))"  # 24

# 7. Organization quality budget
python quality.py --strict .

# 8. Existing selfcheck pinning tests
python -m pytest tests/test_org.py -k check
```

**Definition of Done** = §8 mapping fully green; `pi-batch check` (default set) passes with the new step; gate script byte-identical to pre-change state (`git diff scripts/check-no-refusal.py` empty).

---

## 8. Testable Acceptance Mapping

| Acceptance | Test / command | Pass criterion |
|---|---|---|
| A1.1 (every marker → exit 1) | `test_marker_refuses` (parametrized, markers via `runpy`) | rc 1 + `REFUSAL-CHECK` + `failing closed` per marker |
| A1.2 (empty + whitespace → exit 1) | `test_empty_output_refuses`, `test_whitespace_only_refuses` | rc 1 + `empty output` |
| A1.3 (missing file → exit 1) | `test_missing_file_refuses` | rc 1 + `artifact missing` |
| A1.4 (clean → exit 0) | `test_clean_artifact_passes` | rc 0 + `REFUSAL-CHECK: OK` |
| A1.5 (no-arg usage → exit 2) | `test_usage_without_args` | rc 2 + `usage:` |
| A1.6 (marker list ≥ 10) | `test_marker_list_not_purged` | `len ≥ 10` |
| A1.7 (all refusal outputs carry `REFUSAL-CHECK`) | folded into A1.1/A1.2/A1.3 asserts | substring present |
| A2.1 (duplicate absent) | `test_no_duplicate_copy` | path not exists |
| A2.2 (8 refs → canonical form) | `test_pipelines_reference_canonical_copy` | exactly 8, regex match |
| A2.3 (examples/ tree clean) | `test_examples_tree_only_validate_cmd_refs` | only validate_cmd lines match |
| A3.1 (`refusalcheck` registered) | `test_registered_in_validators` | `in pbatch.config.VALIDATORS` |
| A3.2 (canonical path, not examples/) | same | path substring checks |
| A4.1 (CLI lists gate) | `python pi-batch.py check --steps refusal --skip-eval` | exit 0 + `CHECK [refusal]` |
| A4.2 (end-to-end in CI) | `test_pi_batch_check_lists_gate` | rc 0 + `CHECK [refusal]` in stdout |
| A4.3 (fail-closed, never no-op) | `test_refusal_step_fails_closed_*` + `test_check_rejects_unknown_step` | `is False`; rc 2 |
| A5.1 (AGENTS.md count truthful) | reviewer read-through; optional `test_agents_md_validator_count_matches` | 24 == 24 |
| FR-4 structural (step in default set) | `test_refusal_step_in_default_steps` | `"refusal"` in names |

**§6.1 fallback (identity branch)** — if reviewers reject deletion: replace `test_no_duplicate_copy` with `assert (ROOT/"scripts/check-no-refusal.py").read_bytes() == EXAMPLE_COPY.read_bytes()`, keep the copy, and update A2.3's expectation to "exactly one script file under `examples/`, byte-identical". Exactly one branch (deletion XOR identity) is live; the test file's docstring states which. This design implements the deletion branch (D3).

---

## 9. Scope Guardrails (reaffirmed from the spec)

- No change to the gate's markers, exit codes, or detection semantics (NG1).
- No edits to the 23 existing validator entries; stale `config` entry untouched (NG2).
- No pipeline stage logic/prompts/outputs rewired (NG3); the optional single-token interpreter normalization is the only pipeline-file touch.
- No new marker heuristics, no behavior changes to other checkers (NG4).
