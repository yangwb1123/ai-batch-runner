# Design — Execute expect-only eval cases instead of passing them vacuously

| Field | Value |
|---|---|
| Module | `evals` |
| Direction id | `85368cf1` |
| Requirements | `docs/auto/requirements/evals-85368cf1-requirements.md` (evidence-verified) |
| Status | Design (all evidence claims re-verified live against this repo) |
| Scope | R1–R7 in scope; no expansion |

---

## 0. Evidence verification (all claims re-checked live)

Every material claim in the requirements evidence was reproduced against the
current tree before designing:

| Evidence claim | Verification result |
|---|---|
| `eval.py:82` `def run_suite`; `:89` `asserts = case.get("assert", {})`; loop :92-105 iterates only assert keys | ✅ exact (`read pbatch/eval.py`) |
| `eval.py:120-137` `_suite_domains` reads `expect` for display only | ✅ exact |
| `_assert_value` :60-79 lacks `system_type`/`includes`/`excludes`; unknown key → `ValueError: unknown eval assertion key` at :79 | ✅ exact |
| `classifier.py:206` `classify_text`; `:190` `_detect_system_type`; `:119` `system_type` field; `:247` returned | ✅ exact |
| `rule_matcher.py:202` `match_rules`; `:243` `_inject_system_type_methodology`; `:240` `system_type` in result | ✅ exact |
| `evals/classifier.yaml` — 6 system-type cases use `name:` (not `id:`), expect at :60/:64/:68/:72/:76/:80 | ✅ exact; 18 cases total |
| `evals/rules.yaml` — 4 expect cases use `name:`, expect at :75/:86/:94/:104; backend `includes` list :95-100 | ✅ exact; 17 cases total (parentheticals :70/:99 are ±1 off — non-material) |
| `python -m pbatch eval --json` → total 41, failed 0, failures [] | ✅ reproduced (exit 0) |
| `eval --quick --json` → total 35 (rules 17 + classifier 18) | ✅ reproduced |
| All 6 classifier `expect.system_type` values match live `classify_text` | ✅ reproduced (state-machine/event-driven/search/optimization/knowledge/deterministic — all OK) |
| `design-intelligence-production-workbench` ORIG input → `tier=standard`, 0 rules | ✅ reproduced (classifies `unknown`, generic domain) |
| `design-intelligence-backend-production` ORIG input ("含支付审批流程") → `frontend_ui` | ✅ reproduced (`classify_text` = frontend_ui; di ids = frontend set, none of the 6 backend ids) |
| Corrected workbench input → `tier=production`, all 6 di ids | ✅ reproduced (product/color/priority/cognitive/emotional/data) |
| Corrected backend input → all 6 backend di ids (+ distribution/maintenance superset — fine for `includes`) | ✅ reproduced (api/arch/datamodel/lifecycle/ops/pipeline) |
| `design-intelligence-demo-login` → [product, priority], no emotional | ✅ reproduced |
| `system-type-methodology-routing` → rules contain `system-type-state-machine` | ✅ reproduced (`m["system_type"] == "state-machine"`) |
| Corrected workbench input proven in `tests/test_design_intelligence.py:112` (TestRegistrySelection); corrected backend input proven equivalent to `tests/test_backend_experience.py:99` (identical manifest, verified live) | ✅ confirmed |
| `Makefile:40-41` `make eval` → `python -m pbatch eval` | ✅ exact |
| `tests/test_entrypoints.py:52-68` quick-mode test (rc 0, failed 0, total ≥ 20) | ✅ exact |
| `evals/assess.yaml`: 6 cases, all assert-backed with `id:` | ✅ exact (untouched) |
| `--filter system-type-state-machine` currently untargetable (total 0) | ✅ reproduced (name-keyed case reports as `classifier/?`) |

**New findings beyond the evidence (no scope change):**

1. `run_suite`/`_assert_value`/`eval_main` have exactly one consumer: the
   `eval:eval_main` dispatch at `pbatch/cli.py:302`. `pbatch/selfcheck.py`
   (the `pi-batch check` eval step) shells out to `python -m pbatch eval` as
   a subprocess — no in-process coupling to the case machinery. No internal
   API-compat risk beyond exit codes.
2. `match_rules` result keys are `[domain, page_types, profile, risk, rules,
   skipped, system_type, tier]` — no `task_type` key. Irrelevant for the
   design (`tier` dispatches through the existing branch), but means any
   future `expect.task_type` would need the classify_text branch, not
   match_rules.
3. The 10 renames are collision-free against existing ids (verified:
   `system-type-*` ×6, `design-intelligence-*` ×3,
   `system-type-methodology-routing` ×1 vs. existing 12 classifier + 13
   rules ids — no overlap), so `--filter` stays unambiguous.
4. `--filter <nonexistent-id>` silently returns `total: 0, failed: 0`, exit
   0 — pre-existing behavior, unchanged by this design (documented in §3 F6).
5. Doc-count drift: AGENTS.md ("25 个 eval 规则回归用例"/"23 个断言用例"),
   `docs/SPEC_SYSTEM.md:54` ("25 个 eval 断言"), `selfcheck.py:6` docstring
   ("25 cases") all say 25; measured total is 41. No test asserts "25" (the
   only count gate is `total >= 20` in test_entrypoints), so this is
   cosmetic — tracked as a follow-up (§7), not a CI risk.

---

## 1. Design overview

Two independent layers, no cross-dependency:

- **Layer 1 — machinery (`pbatch/eval.py` only, ~25 changed lines):**
  `expect` becomes a fallback assertion source when `assert` is absent
  (R1); `_assert_value` gains `system_type` + `includes`/`excludes`
  dispatch keys (R2); a case with neither `assert` nor any *executable*
  `expect` key raises a typed error that `eval_main` maps to **exit 2**,
  naming suite/case (R3).

- **Layer 2 — data (`evals/*.yaml` only):** rename the 10 `name:`-keyed
  cases to `id:` (R4/R5) and correct the 2 divergent `rules.yaml` inputs to
  the evidence-verified variants already pinned by
  `tests/test_design_intelligence.py` (R5). `includes`/`excludes`/`tier`
  expectations stay byte-identical.

```
today:   run_suite → asserts = case.get("assert", {}) → { } for 10 cases → PASS (0 assertions)
after:   run_suite → assert or executable expect keys → 41/41 real assertions
         vacuous case (neither) → VacuousCaseError → eval_main → exit 2 (fail closed)
```

No changes to `pbatch/classifier.py`, `pbatch/rule_matcher.py`,
`pbatch/assessor.py`, `pbatch/product.py`, any registry, or
`evals/assess.yaml` — the two divergent cases are fixed by aligning *test
inputs* to verified behavior, explicitly not by changing routing behavior.

---

## 2. API changes

### 2.1 `pbatch/eval.py` — module-level constants (new)

```python
# Expect keys that are executable as assertions. Display-only expect keys
# (task_type, domain, matched_tier, effective_tier) are consumed solely by
# _suite_domains and never executed.
EXECUTABLE_EXPECT_KEYS = ("system_type", "tier", "includes", "excludes")


class VacuousCaseError(ValueError):
    """A case has neither assert nor any executable expect key (fail closed)."""
```

### 2.2 `_assert_value` — two dispatch extensions (R2)

```python
def _assert_value(key: str, input_text: str) -> object:
    if key in ("task_type", "platform", "profile", "confident", "system_type"):
        cls = classify_text(input_text)
        return {"task_type": cls.task_type, "platform": cls.platform,
                "profile": cls.profile, "confident": cls.confident,
                "system_type": cls.system_type}[key]      # ← new key
    ...
    if key in ("has_rule", "lacks_rule", "includes", "excludes"):
        # includes/excludes share has_rule/lacks_rule semantics:
        # rule-id set from the live match_rules manifest.
        return {item["id"] for item in match_rules(input_text)["rules"]}
    ...
```

`cls.system_type` always exists (dataclass default `"deterministic"`,
classifier.py:119) — including for `unknown` tasks (:232) — so the new key
can never KeyError.

### 2.3 `run_suite` — expect fallback + vacuous detection (R1, R3)

```python
        asserts = case.get("assert", {})
        if not asserts:
            expect = case.get("expect", {}) if isinstance(case.get("expect"), dict) else {}
            asserts = {k: v for k, v in expect.items()
                       if k in EXECUTABLE_EXPECT_KEYS}
            if not asserts:
                raise VacuousCaseError(
                    f"{suite['name']}/{case_id}: no assert and no executable "
                    f"expect keys {list(EXECUTABLE_EXPECT_KEYS)}")
```

And the loop's list-semantics branches extend to the aliases:

```python
            if key in ("has_rule", "includes"):
                missing = [e for e in expected_list if e not in actual]
                if missing:
                    failures.append(...)
            elif key in ("lacks_rule", "excludes"):
                present = [e for e in expected_list if e in actual]
                if present:
                    failures.append(...)
```

Precedence rule (R1): when `assert` is present **and non-empty**, it
wins and `expect` stays inert, exactly today's behavior. `assert: {}` is
treated as **absent** (`{}` is falsy in `_effective_asserts`), so the
executable `expect` keys run instead; only a case with `assert: {}` (or
absent) AND no executable expect key falls through to the vacuous check
→ exit 2 (pinned by `test_assert_empty_dict_uses_expect_fallback`).

### 2.4 `eval_main` — exit-2 mapping (R3)

```python
    for suite in load_eval_files():
        if args.quick and suite.get("name") not in {"rules", "classifier"}:
            continue
        try:
            for case_id, ok, detail in run_suite(suite, args.filter):
                ...
        except VacuousCaseError as exc:
            log.error("EVAL schema error: %s", exc)
            sys.exit(2)
```

The error names the offending suite/case (R3 requirement). Exit 2 matches
`load_eval_files`' existing schema-error exit code — exit 1 stays reserved
for assertion failures (R5 of the requirements).

### 2.5 Data changes — `evals/classifier.yaml` (R4)

Rename `name:` → `id:` on the 6 system-type cases (inputs and `expect`
blocks byte-identical):

```yaml
  - id: system-type-state-machine
    input: "订单审批流，状态流转，审核流程"
    expect:
      system_type: state-machine
  - id: system-type-event-driven
    ...
  - id: system-type-default
    input: "做一个登录页 demo"
    expect:
      system_type: deterministic
```

### 2.6 Data changes — `evals/rules.yaml` (R5)

Rename `name:` → `id:` on all 4 cases; correct exactly 2 inputs (kept
`includes`/`excludes`/`tier` expectations byte-identical):

```yaml
  - id: design-intelligence-production-workbench
    input: "企业订单管理工作台，含 KPI 与趋势图表，生产环境"   # ← corrected
    expect:
      tier: production
      includes: [design-intelligence-product, design-intelligence-color,
                 design-intelligence-priority, design-intelligence-cognitive,
                 design-intelligence-emotional, design-intelligence-data]
  - id: design-intelligence-demo-login      # unchanged input + expectations
    input: "生成一个登录页 demo"
    ...
  - id: design-intelligence-backend-production
    input: "写一个订单创建接口，含支付审批，生产环境，go"        # ← corrected ("流程" removed)
    expect:
      includes: [design-intelligence-api, design-intelligence-lifecycle,
                 design-intelligence-datamodel, design-intelligence-arch,
                 design-intelligence-ops, design-intelligence-pipeline]
  - id: system-type-methodology-routing     # unchanged
    input: "订单审批流，状态流转，审核流程，生产"
    expect:
      includes: [system-type-state-machine]
```

The corrected workbench input is byte-identical to the string asserted in
`tests/test_design_intelligence.py:112` (TestRegistrySelection), so eval and
pytest double-pin it. The corrected backend input (`…生产环境，go`) is not
pinned verbatim in pytest; its nearest pin `tests/test_backend_experience.py:99`
(`…生产，go`) yields an identical manifest (8 di ids incl. all 6 expected,
tier production) — verified live, behavioral risk nil.

### 2.7 Documentation (in-scope, minimal)

- Update `pbatch/eval.py` module docstring: document expect-as-fallback,
  the 3 new executable keys, and the exit-code contract (0 pass / 1
  assertion fail / 2 schema-vacuous). This is the only doc change required
  by the requirements (spec §5 lists no other doc obligations).

### 2.8 New tests — `tests/test_eval_suite.py` (R7)

Uses `monkeypatch.setattr("pbatch.eval.EVAL_DIR", tmp_path)` with crafted
suites — never touches the real `evals/`:

1. `test_system_type_key_dispatches_to_classify_text` — suite with
   `expect: {system_type: state-machine}` on the state-machine input →
   PASS; same input with `system_type: knowledge` → FAIL (exit 1).
2. `test_includes_excludes_dispatch_to_match_rules` — `includes:
   [design-intelligence-product, design-intelligence-priority]` +
   `excludes: [design-intelligence-emotional]` on "生成一个登录页 demo" →
   PASS; `includes: [design-intelligence-api]` on the same input → FAIL.
3. `test_vacuous_case_exits_2` — suite with a case having only
   display-only `expect: {task_type: backend}` → `pytest.raises(SystemExit)`
   with `exc.value.code == 2`; assert the log/error text names suite + case.
4. `test_expect_inert_when_assert_present` — case with both `assert:
   {task_type: backend}` and a deliberately wrong `expect.system_type` →
   PASS (assert wins, expect inert — pins R1 precedence).
5. `test_filter_targets_renamed_cases` — `eval_main(["--filter",
   "system-type-state-machine", "--json"])` on the temp suite (or the real
   suite in a subprocess) → total 1; real-suite variant runs the A1 command
   and asserts `total == 1, failed == 0`.

---

## 3. Compatibility constraints

| Constraint | Detail |
|---|---|
| Exit-code contract | 0 = all pass; 1 = assertion failures (unchanged); **2 = new** schema/vacuous error — consistent with `load_eval_files`' existing exit 2. No consumer tests exit 2 today. |
| JSON output shape | `--json` emits `{total, failed, failures}` (plus `coverage` only with `--coverage`) — unchanged; `--coverage` still runs every suite's `run_suite` first (coverage is computed after the run loop), so a vacuous case triggers exit 2 without a coverage payload (fail-closed, verified live). |
| `--quick` | Suite-name filter (`rules` + `classifier`) unchanged; quick total 35 unchanged. |
| `--filter` | Semantics unchanged; now additionally targets the 10 renamed cases. Non-existent id → total 0, exit 0 (pre-existing; §3 F6). |
| assert-present cases | Byte-identical behavior — `expect` remains inert (R1 precedence). |
| `_suite_domains` | Display keys (`task_type`, `system_type`, `domain`, `tier`, `matched_tier`, `effective_tier`) unchanged — note `system_type`/`tier` are dual-purpose (display + execution), which is fine: display reading is read-only and independent of assertion execution. No output-format change. |
| Classifier/rule-matcher | Zero changes to `pbatch/classifier.py`, `pbatch/rule_matcher.py` or registries — the 2 corrected inputs are the only behavior alignment, and they match already-pinned pytest expectations. |
| Consumers | `cli.py:302` (eval_main), `selfcheck.py` subprocess (`pi-batch check` eval step, rc must stay 0), `test_entrypoints.py:52-68` (rc 0, failed 0, total ≥ 20), `make eval`. All satisfied: real suite = 41 real assertions, 0 failures. |
| Id collisions | None (verified §0 finding 3). |
| Ids in external code | `tests/test_rule_matcher.py:233` references the rule id `system-type-state-machine` (a rule-matcher artifact), not the eval case — unaffected. |
| 25-case doc counts | AGENTS.md / SPEC_SYSTEM.md / selfcheck.py docstring — stale (actual 41), no assertion depends on them; follow-up §7. |

---

## 4. Failure modes

| # | Failure mode | Behavior | Mitigation |
|---|---|---|---|
| F1 | A future case has `expect` with only display keys (or no expect at all) | Exit 2 naming suite/case — **intended fail-closed** (was silent PASS) | Error message is self-explanatory; fix = add an executable key or `assert`. Audited: all 41 current cases non-vacuous after the change. |
| F2 | `_SYSTEM_TYPE_TERMS` keyword edit re-routes a system-type input | Assertion FAIL → exit 1 (visible regression signal — the point of R4) | The 6 expectations are pinned to current behavior; failure is loud, not vacuous. |
| F3 | Classifier/registry keyword edit re-routes a corrected input (workbench/backend) away from production/backend tier | Assertion FAIL → exit 1 | Same inputs already pinned in `tests/test_design_intelligence.py` — pytest flags the same drift; no silent divergence possible. |
| F4 | `includes`/`excludes` value semantics misread | `includes` = all listed present; `excludes` = none present; extra rules (e.g. corrected backend → 8 di ids) never fail `includes` | Semantics identical to `has_rule`/`lacks_rule` (verified live); documented in the eval.py docstring. |
| F5 | `assert: {}` explicitly empty | Treated as **absent** → executable `expect` keys run (assert-wins applies only to non-empty `assert`); vacuous exit 2 only when no executable expect key remains | Pinned by `test_assert_empty_dict_uses_expect_fallback`; no current case does this. |
| F6 | `--filter <typo>` | total 0, exit 0 — silent no-op (pre-existing, unchanged) | Documented limitation; A1/A2 use verified ids. |
| F7 | Exception escapes `eval_main` | `VacuousCaseError` is caught per-suite → exit 2; other suites' logs may already be emitted, but `--json` payload is never printed on exit 2 (fail-closed, script-friendly) | New pytest test 3 pins `SystemExit(2)`. |
| F8 | External consumer parses `--json` stdout unconditionally | On exit 2 there is no JSON payload | Contract documented: check exit code first (same as today's exit 1 path, which also prints `{total, failed}` — note: exit 1 still prints the JSON payload today; exit 2 will not. This asymmetry is intentional: schema errors are not a run result). |

---

## 5. Migration steps

Ordered, each step independently verifiable:

1. **Implement machinery** — edit `pbatch/eval.py`: add
   `EXECUTABLE_EXPECT_KEYS` + `VacuousCaseError` (§2.1), extend
   `_assert_value` (§2.2), extend `run_suite` fallback + list branches
   (§2.3), wrap `eval_main` (§2.4), update module docstring (§2.7).
   *Verify:* `python -m pbatch eval --json` still exits 0 but now shows
   assertion-driven PASS for the 10 cases once data is migrated; before the
   data migration the 10 name-keyed cases would **not** be vacuous (they
   have executable expect keys) — so this step alone must not exit 2.
2. **Migrate classifier data** — rename 6 `name:` → `id:` in
   `evals/classifier.yaml` (§2.5).
3. **Migrate rules data** — rename 4 `name:` → `id:` and correct the 2
   inputs in `evals/rules.yaml` (§2.6).
4. **Add regression tests** — `tests/test_eval_suite.py` (§2.8).
5. **Full verification** — the acceptance commands (§6), `make eval`,
   `python -m pytest tests/test_entrypoints.py::test_eval_quick_mode_runs_core_domains -q`,
   `make ci`.
6. **Commit** — single commit per repo convention (machinery + data +
   tests + docstring); gate scripts (`make eval` / `make ci`) green before
   push. `docs/auto/` artifacts follow the campaign flow, not this commit.

---

## 6. Testable acceptance mapping

| Acceptance (requirements) | Mechanism | Verification command / test | Expected |
|---|---|---|---|
| A1 — `system_type` key makes `system-type-state-machine` really check `'state-machine'` | R2 (`system_type` dispatch) + R4 (rename → targetable) | `python -m pbatch eval --filter system-type-state-machine --json` | `total: 1, failed: 0`; code inspection shows the case's executed assertion set is `{system_type}` (non-empty) |
| A2 — `includes`/`excludes` make all 4 rules cases execute real assertions | R2 + R5 (rename + 2 input corrections) | `--filter design-intelligence-demo-login` / `-production-workbench` / `-backend-production` / `system-type-methodology-routing` (4 × `--json`) | each `total: 1, failed: 0`; expectations per §0 manifests (verified live) |
| A3 — vacuous case → exit 2 (fail closed) | R3 | `pytest tests/test_eval_suite.py::test_vacuous_case_exits_2` (monkeypatched `EVAL_DIR`, real `evals/` untouched) | `SystemExit` code 2, error names suite + case; real-suite `pi-batch eval` still exit 0 |
| A4 — 41 cases, 10 previously-vacuous now real | R1 + R4 + R5 (+ R3 guarantee) | `python -m pbatch eval --json`; `make eval`; `pytest tests/test_entrypoints.py::test_eval_quick_mode_runs_core_domains -q` | `total: 41, failed: 0, failures: []`, rc 0 everywhere; `total >= 20` gate holds |
| R7 — new pytest coverage | §2.8 | `python -m pytest tests/test_eval_suite.py -q` | 5 tests pass |
| R6 — suite stays green | all of the above | `make ci` | full gate set green |

---

## 7. Out of scope / follow-up recommendations

1. **Doc-count sync (recommended follow-up, cosmetic):** AGENTS.md
   ("25 个 eval 规则回归用例", "23 个断言用例"), `docs/SPEC_SYSTEM.md:54`
   ("25 个 eval 断言"), `pbatch/selfcheck.py:6` docstring ("25 cases") all
   predate the 41-case suite. No assertion depends on the numbers, but the
   docs are misleading; a one-line sync per site is cheap.
2. **`--filter` silent-no-op for unknown ids** (F6): pre-existing;
   consider a future warning ("no case matched id X") — explicitly not in
   this direction's scope.
3. **Classifier/rule_matcher routing** of the original workbench/backend
   inputs (`unknown` → generic `standard` tier; "流程" → `frontend_ui`):
   deliberately untouched per spec §5. If product behavior is ever wanted
   (e.g. backend domain keywords extended to 流程 phrases), that is a
   separate direction with its own eval additions.
