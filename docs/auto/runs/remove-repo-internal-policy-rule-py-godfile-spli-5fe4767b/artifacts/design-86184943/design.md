# Design — Remove repo-internal rule PY-GODFILE-SPLIT & fail closed on self-referential rule files

| Field | Value |
|---|---|
| Module | `backend-specs` + `pbatch/rule_cli.py` + `evals/rules.yaml` |
| Direction | Remove repo-internal policy rule PY-GODFILE-SPLIT from the backend domain registry and fail closed on self-referential rule files |
| Requirements | `requirements-spec.md` (same run dir), R1/R2/R3, acceptance A1–A4 |
| Status | Design — all cited evidence independently re-verified against HEAD |

---

## 1. Evidence verification register (untrusted → verified)

All claims from the source evidence were treated as untrusted and re-checked
against the repository, not taken from the spec's word:

| # | Claim | Verification (independent) | Verdict |
|---|---|---|---|
| E1 | `backend-specs/rules.yaml:164` contains the `PY-GODFILE-SPLIT` block | `sed -n '150,185p'` — block at 164–170: `files: ['backend-specs/rules.yaml']`, `min_tier: standard`, `level: policy`, `required: true`, `# promoted from draft PY-GODFILE-SPLIT（advice，…）` | ✅ confirmed |
| E2 | `docs/rules/promoted/PY-GODFILE-SPLIT-20260807.yaml` exists (canonical archive) | File present; full RCA record (`id`, `category: backend`, `severity: advice`, rationale, requirements, forbidden); `pbatch/learn.py:113` `PROMOTED_DIR = Path("docs/rules/promoted")` | ✅ confirmed |
| E3 | `pbatch/rule_cli.py:52` `_check_rule` checks file existence only | Read lines 52–80: field presence, `min_tier`/`level` enums, `suppress_on` signals, `Path(str(file)).exists()` for `files` and profile-expanded `files_template`. No self-reference comparison | ✅ confirmed |
| E4 | `pbatch/rule_matcher.py:186` required rules never suppressed | Read `_rule_skip_reason`: `if rule.get("required"): return ""` — leak is unconditional for any tier ≥ `min_tier` | ✅ confirmed |
| E5 | `pbatch/rule_matcher.py:42` `DOMAIN_REGISTRIES["backend"]` | `{"backend": ["backend-specs/rules.yaml"], "frontend_ui": ["ui-specs/rules.yaml"], "generic": []}` | ✅ confirmed |
| E6 | Bug reproduced: both acceptance inputs emit the rule; `rules --check` exits 0 | Ran `python pi-batch.py rules --check` → `exit=0`, both registries "OK" with the self-referential rule live | ✅ confirmed |
| E7 | `rules --check` ignores `--registry` (A3 CLI caveat) | `rules_main` (rule_cli.py:124–126): `if args.check: _check_registries_cli(); return` — returns **before** parsing `--registry` | ✅ confirmed |
| E8 | `run_suite` only reads `assert:`; `expect:`/`includes` inert | eval.py:89 `asserts = case.get("assert", {})`; `expect` read only in `_suite_domains` (coverage display). `has_rule`/`lacks_rule` handled at eval.py:70, 97–106 | ✅ confirmed |
| E9 | Baseline `pi-batch eval` = 40 cases / 0 failed | Ran `python pi-batch.py eval --json` → `{"total": 40, "failed": 0}` | ✅ confirmed |
| E10 | Zero blast radius (no test/eval/script references the id) | `grep -rn "PY-GODFILE-SPLIT"` (excluding archive/registry/run dirs) → no hits; `evals/` has no reference | ✅ confirmed |
| E11 | `check_registry` has a single production caller | grep: only `_check_registries_cli` (rule_cli.py:90); **plus** `tests/test_agent_engineering.py:158` calls `check_registry(load_registry(str(broken)))` with one positional arg → optional-param compatibility constraint | ✅ confirmed (+1 new finding) |
| E12 | `_default_registry_paths` exists and is importable | rule_matcher.py:89–93 returns repo-root-anchored + CWD-anchored candidates | ✅ confirmed |
| E13 | Gate propagation: `pi-batch check` runs `rules --check` | selfcheck.py:37 `("registry", [sys.executable, "-m", "pbatch", "rules", "--check"])`; `make ci` runs `pytest tests/` which includes `test_rules_check_passes` (asserts exit 0) and `test_entrypoints.py::test_eval_quick_mode_runs_core_domains` | ✅ confirmed (+1 new finding) |
| E14 | No other self-referential rule exists in either registry | Independent scan of both `rules.yaml` (files + files_template, resolve-normalized): only `PY-GODFILE-SPLIT` matches → the new gate ships green | ✅ confirmed (+1 new finding) |
| E15 | `learn promote` writes `files:` verbatim into the registry | learn.py:143–153: `--files` list is written unvalidated into the registry block (only existence checked, learn.py:139–141); post-promote `rules --check` is only a log line (learn.py:333), not an executed gate → re-pollution vector is real; R1.2 (documented guardrail) + R2 (structural gate) both justified | ✅ confirmed (+1 new finding) |

**Net**: every cited claim holds; 3 additional findings (E11, E13, E15) sharpen
compatibility constraints and the migration ordering.

---

## 2. Design overview

Three coordinated, small changes — one data removal, one gate hardening, one
regression lock. The gate is the durable protection (pins the *class* of
defect); the eval case pins this *specific* rule; the removal is the payload.

```
backend-specs/rules.yaml   R1: delete PY-GODFILE-SPLIT block (lines 164–170)
pbatch/rule_cli.py         R2: check_registry(+registry_paths) fail-closed self-reference check
evals/rules.yaml           R3: assert: lacks_rule [PY-GODFILE-SPLIT] backend case
tests/test_rule_matcher.py R2.4: unit tests (gate fires / shipped registries clean)
docs/SPEC_SYSTEM.md        (optional) one-line registry hygiene note
```

No behavioral change to `pbatch/rule_matcher.py` matching logic — line 186's
required-rule fail-closed semantics are correct and stay. No change to
`pbatch/learn.py` (R1.2 is a documented guardrail per requirements spec).

---

## 3. API changes

### 3.1 `pbatch/rule_cli.py` — `check_registry` gains optional `registry_paths`

```python
def check_registry(registry: dict,
                   registry_paths: Optional[list] = None) -> list:
    """All registry rules: schema + file references; [] = clean.
    registry_paths: resolved paths of the registry file(s) the rules were
    loaded from; when given, a rule whose files/files_template resolves to
    any of them is a violation (self-referential rule, fail closed)."""
    resolved = None
    if registry_paths:
        resolved = {str(Path(p).resolve()) for p in registry_paths}
    violations = []
    for rule_id, rule in (registry.get("rules") or {}).items():
        _check_rule(rule_id, rule, violations, resolved)
    return violations
```

- `None` (default) → exactly current behavior for existing callers (E11:
  `tests/test_agent_engineering.py:158` keeps compiling and passing).
- `_check_rule(rule_id, rule, violations, registry_paths: Optional[set] = None)`
  keeps its single-rule shape; add after the existing file-existence loop:

```python
    if registry_paths:
        for file in files:
            if str(Path(str(file)).resolve()) in registry_paths:
                violations.append(
                    f"rule '{rule_id}': files entry '{file}' resolves to the "
                    "registry file itself (self-referential rule)")
        if template:
            for profile in rule.get("profiles", []):
                target = str(template).replace("{profile}", str(profile))
                if str(Path(target).resolve()) in registry_paths:
                    violations.append(
                        f"rule '{rule_id}': files_template resolves to the "
                        "registry file itself (self-referential rule)")
```

  Comparison is **resolve-normalized** (`Path.resolve()`: absolute, symlinks
  resolved, `..` collapsed) — catches `backend-specs/rules.yaml`,
  `./backend-specs/rules.yaml`, and absolute forms alike (R2.5).

### 3.2 `_check_registries_cli` — pass the actual registry paths

```python
def _check_registries_cli() -> None:
    from .rule_matcher import _default_registry_paths   # already importable
    domains = ("frontend_ui", "backend")
    for domain in domains:
        registry = load_registry(domain=domain)
        paths = sorted({str(p.resolve())
                        for p in _default_registry_paths(domain)})
        violations = check_registry(registry, paths)
        ...
```

- Uses the exact same candidate resolution `load_registry` uses (E12), so the
  comparison set is the registry file the rules were actually loaded from.
- Exit plumbing untouched: `sys.exit(1)` already exists (R2.4).
- `pi-batch check` registry gate (selfcheck.py:37) and `make ci`'s
  `test_rules_check_passes` inherit the check with **zero wiring changes** (E13).

### 3.3 `evals/rules.yaml` — one new case (R3, `assert:` form)

Anchored after `crud-backend-minimal` (line 18), same `task_type: backend` +
`lacks_rule` shape already used there:

```yaml
  - id: backend-manifest-excludes-repo-internal-policy
    input: "写一个订单创建接口，含支付审批，生产，go"
    assert:
      task_type: backend
      lacks_rule: [PY-GODFILE-SPLIT]
```

### 3.4 `tests/test_rule_matcher.py` — two new tests

```python
def test_registry_rejects_self_referential_files():
    """A rule listing the registry file itself is a schema violation."""
    from pbatch.rule_cli import check_registry
    reg = {"rules": {"bad": {"description": "x", "min_tier": "standard",
                             "files": ["backend-specs/rules.yaml"]}}}
    viol = check_registry(reg, registry_paths=["backend-specs/rules.yaml"])
    assert any("self-referential" in v for v in viol)
    assert not check_registry(reg)  # default: backward-compatible behavior

def test_shipped_registries_have_no_self_reference():
    """Both shipped registries pass the self-reference gate (A3 clean side)."""
    from pbatch.rule_cli import check_registry
    from pbatch.rule_matcher import _default_registry_paths, load_registry
    for domain in ("frontend_ui", "backend"):
        paths = [str(p.resolve()) for p in _default_registry_paths(domain)]
        assert not check_registry(load_registry(domain=domain), paths)
```

### 3.5 `backend-specs/rules.yaml` — data removal (R1)

Delete exactly the block at lines 164–170:

```yaml
  PY-GODFILE-SPLIT:
    description: god-file 967 行 cli.py：执行层未拆分导致可维护性下降
    files: ['backend-specs/rules.yaml']
    min_tier: standard
    level: policy
    required: true
    # promoted from draft PY-GODFILE-SPLIT（advice，影子/人工批准后并入）
```

`docs/rules/promoted/PY-GODFILE-SPLIT-20260807.yaml` stays untouched (R1.1).

---

## 4. Compatibility constraints

| # | Constraint | Consequence |
|---|---|---|
| C1 | `registry_paths` must be **optional, default `None`** | Existing callers (`tests/test_agent_engineering.py:158`, external users of `check_registry`) keep identical behavior; signature is additive |
| C2 | Violation messages must be **additive** (new text, no rewording of existing messages) | `test_rules_check_fails_on_broken_registry` asserts substring `"files"` — the new message contains "files entry …" so it cannot break that assertion; no test asserts exact violation counts |
| C3 | Clean registries must still pass: `rules --check` exit 0, `test_rules_check_passes` green | Verified zero self-references in both shipped registries today (E14) → gate ships green |
| C4 | **Ordering hazard**: R2 must not land while the self-referential rule is still in the registry | `test_rules_check_passes` (in `make ci`) asserts exit 0 → CI would go red. Land R1+R2 in the same change, or R1 first |
| C5 | `_default_registry_paths` is a private helper; importing it inside `_check_registries_cli` keeps it internal to `pbatch` | No new public API surface; no circular import (rule_cli already imports from rule_matcher) |
| C6 | Eval count changes 40 → 41 | `test_entrypoints.py::test_eval_quick_mode_runs_core_domains` asserts `total >= 20` — unaffected |
| C7 | `rules --check` continues to ignore `--registry` (pre-existing, E7) | A3's gate demonstration is via pytest unit test, not CLI override — documented in §7 |
| C8 | No `pbatch/rule_matcher.py` behavior change; `required` semantics intact | Manifest content for legit tasks unchanged except the removed rule; `format_manifest` untouched |
| C9 | `learn promote` unchanged (R1.2 = documentation) | The guardrail lives in: the structural gate (R2) + eval lock (R3) + PR description note; `learn.py:333` already instructs operators to run `rules --check` after promote |

---

## 5. Failure modes

| # | Failure mode | Detection | Mitigation |
|---|---|---|---|
| F1 | A repo-internal rule is re-promoted with `files: [<registry>.yaml]` (the E15 vector — `learn promote --files` is written verbatim) | `rules --check` exits 1 → `pi-batch check` registry gate red → `make ci` red via `test_rules_check_passes` | R2 gate is class-level (any self-reference, any domain); R3 pins this rule id; R1.2 documents the prohibition |
| F2 | Removal silently regresses (rule re-added without promotion) | Eval case `lacks_rule` fails (`pi-batch eval`, `make ci` eval path, `test_eval_quick_mode_runs_core_domains`) | R3 `assert:` form (not inert `expect:` — E8); eval count delta 40→41 visible in any run output |
| F3 | False positive: a legitimate spec file whose path resolves to a registry path | Gate fires on a registry that is actually clean → visible at `rules --check` | Comparison is exact resolve-normalized match against **only the currently-validated registry's own paths** (R2.6); scan shows zero current hits (E14); `test_shipped_registries_have_no_self_reference` locks the clean side |
| F4 | `files_template` with empty `profiles` never expanded → self-reference via template undetectable | Static gap, no current occurrence (templates point at `ui-specs/business-profiles/`, never the registries) | Mirrors the existing existence-check loop exactly (same coverage, same limitation); documented; zero false-negative risk for today's registries |
| F5 | Cross-domain reference (backend rule listing `ui-specs/rules.yaml`) escapes the gate | None today (E14 scan covered both registries' cross refs implicitly — no rule lists the other registry either) | Explicitly out of scope (R2.6); future hardening: compare against the union of all `DOMAIN_REGISTRIES` paths — noted, not built |
| F6 | Registry load silently degrades to empty (`load_registry` catches exceptions) | Pre-existing fail-closed design (empty backend rules = no leak) | Unchanged; out of scope — the gate validates the loaded dict, which is the same data `match_rules` consumes |
| F7 | Eval case accidentally written in inert `expect:` format (the `design-intelligence-*` precedent, E8) | Case passes forever regardless of behavior; total stays 40 | R3.1 mandates `assert:`; §7 step 6 verifies `eval --filter backend-manifest-excludes-repo-internal-policy` actively runs; review checklist item |
| F8 | Migration ordering slip (R2 before R1) | `make ci` red (C4) | §6 step order enforces R1 first / same change |

---

## 6. Migration steps (ordered, each with its own verification)

> No persisted state, no schema migration, no data backfill — this is a
> code+data edit within one repo. "Migration" = the safe landing sequence.

1. **R1** — Delete the block (lines 164–170) from `backend-specs/rules.yaml`.
   Verify: `python -c "import yaml; yaml.safe_load(open('backend-specs/rules.yaml'))"` parses; `grep -c PY-GODFILE-SPLIT backend-specs/rules.yaml` → 0; archive file still present.
2. **R2** — Apply §3.1/§3.2 edits to `pbatch/rule_cli.py` (import inside `_check_registries_cli`, optional param on `check_registry`, comparison block in `_check_rule`).
3. **R2.4** — Add the two tests to `tests/test_rule_matcher.py` (§3.4).
4. **R3** — Add the eval case to `evals/rules.yaml` (§3.3) **in `assert:` form**.
5. **R1.2 docs** — PR description note: repo-internal RCA rules belong in `docs/rules/promoted/`, never in shipped registries; (optional) one line in `docs/SPEC_SYSTEM.md` registry section: "rules must not reference the registry file itself; `rules --check` rejects self-referential `files`".
6. **Gate sequence** (in order; each must pass before the next):
   ```bash
   python pi-batch.py rules --check                          # exit 0
   python pi-batch.py eval --json                            # total 41, failed 0
   python pi-batch.py eval --filter backend-manifest-excludes-repo-internal-policy  # the new case actually runs (F7)
   python -m pytest tests/test_rule_matcher.py -k self_referential -q
   python -m pytest tests/test_agent_engineering.py -k rules -q
   python -m pytest tests/                                   # full regression (387+)
   make ci
   python quality.py --strict .                              # org gate unchanged
   ```
7. **Rollback** (if any gate fails after step 1): `git checkout backend-specs/rules.yaml pbatch/rule_cli.py evals/rules.yaml tests/test_rule_matcher.py` — the change is fully contained in 4 files (+ optional doc); no state to unwind.

---

## 7. Testable acceptance mapping (A1–A4 → concrete tests)

| Acceptance (from requirements spec) | Testable mechanism | Expected result |
|---|---|---|
| **A1** — `rules "写一个订单创建接口，含支付审批，生产，go" --json` has no `PY-GODFILE-SPLIT` | (a) Eval case `backend-manifest-excludes-repo-internal-policy` (`assert: lacks_rule`); (b) direct: `python pi-batch.py rules "写一个订单创建接口，含支付审批，生产，go" --json \| grep PY-GODFILE-SPLIT` | (a) eval PASS; (b) exit 1 from grep (no match), exit 0 from `rules` |
| **A2** — same for `"开发一个客户管理模块的列表查询与新增接口"` | Direct command + grep (same `match_rules` code path as A1; no separate eval case needed — R3.2 optional) | No `PY-GODFILE-SPLIT` in `rules[]` |
| **A3** — `rules --check` exits non-zero when a rule's `files` lists the registry itself; clean registries still exit 0 | (a) `test_registry_rejects_self_referential_files` — in-memory self-referential rule + `check_registry(reg, registry_paths=[...])` returns a violation (deterministic, non-mutating; CLI override path is closed by E7); (b) `test_shipped_registries_have_no_self_reference` + step-6 `rules --check` = clean side; (c) by construction: `_check_registries_cli` already `sys.exit(1)`s on any violation | (a) test passes; (b) exit 0; (c) a registry *containing* a self-reference makes `rules --check` exit 1 → `pi-batch check` registry gate and `test_rules_check_passes` (make ci) both red |
| **A4** — `pi-batch eval` and `make ci` pass | Step-6 gate sequence: eval total **41**/0 (baseline 40/0, E9), full pytest, `make ci` | 0 failures; total increments by exactly 1 (observable count delta is itself a regression signal) |

Implementation-order dependency (must hold): A3's clean side and A4 are only
simultaneously satisfiable if **R1 lands in the same change as R2** (C4/F8).
A1/A2 are satisfiable by R1 alone but would leave the gate open — not a
complete implementation.

---

## 8. Out of scope (scope discipline, per requirements §3)

- No generic-domain registry loading (`DOMAIN_REGISTRIES["generic"] = []`
  stays; the promoted archive is the repo-policy home, consumed by
  learn/health/reflection flows).
- No cross-field registry hardening (direction #2), no `design-intelligence-*`
  re-tiering (direction #3).
- No `learn.py` code change (R1.2 is documentation; the structural protection
  is R2+R3).
- No cleanup of the stale `pipeline.yaml` in the run dir (historical artifact).
- No change to `rules --check`'s `--registry` override behavior (pre-existing).

## 9. Verification evidence gathered for this design

| Check | Result |
|---|---|
| `python pi-batch.py rules --check` | exit 0 with the polluted rule live (bug reproduced, E6) |
| `python pi-batch.py eval --json` | baseline 40 total / 0 failed (E9) |
| Self-reference scan, both registries (files + files_template, resolve-normalized) | only `PY-GODFILE-SPLIT` (E14) |
| `grep -rn PY-GODFILE-SPLIT` outside archive/registry/run dirs | no test/eval/script dependents (E10) |
| `check_registry` callers | production: `_check_registries_cli` only; test: 1-arg call (E11) |
| `selfcheck.py` registry gate, `Makefile` ci targets, `tests/test_agent_engineering.py` rules tests | gate propagation and canaries confirmed (E13) |
| `learn.py promote` write path | `--files` written verbatim, post-promote check only logged (E15) |
