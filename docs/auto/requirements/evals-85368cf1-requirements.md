# Requirements Specification — Execute expect-only eval cases instead of passing them vacuously

| Field | Value |
|---|---|
| Module | `evals` |
| Direction id | `85368cf1` |
| Source analysis | `docs/auto/analyses/evals-85368cf1.json` (direction #1) |
| Status | Proposal (evidence-verified, one count correction, two case-input corrections) |
| Scope | This direction only — no expansion |

---

## 1. Summary

10 of the 41 eval cases (24.4%; direction said "10 of 40" — measured total is
**41**: 6 assess + 18 classifier + 17 rules) contain only an `expect:` block
and no `assert` key: all 6 system-type cases in `evals/classifier.yaml` and
all 4 design-intelligence/system-type cases in `evals/rules.yaml`.
`run_suite` in `pbatch/eval.py` iterates only `case.get("assert", {})`, so
these cases execute **zero assertions** and report PASS unconditionally
(verified live: `python -m pbatch eval --json` → `total: 41, failed: 0`).
`expect` is read solely by `_suite_domains` (eval.py:120-137) for coverage
display; the keys those cases exercise (`system_type`, `includes`,
`excludes`) are not in `_assert_value`'s dispatch and would raise
`ValueError: unknown eval assertion key` (eval.py:79) if placed in `assert`.

The system-type routing and design-intelligence rule machinery (newest
classifier/rule_matcher features) therefore has zero real regression
coverage, and a case that is missing both `assert` and executable `expect`
keys passes vacuously (fail-open) instead of failing closed.

**Two of the four `rules.yaml` expect blocks do not match actual machinery
behavior** (verified live — see §2.3). They must be aligned to
evidence-verified inputs (both already proven by
`tests/test_design_intelligence.py::TestRegistrySelection`) so the
newly-executed assertions pass and CI stays green. This is an input-text
correction to the test cases, **not** a behavior change to
classifier/rule_matcher (explicitly out of scope, §5).

---

## 2. Evidence verification

### 2.1 Cited references — verified against the current repository

| Cited evidence | Verification result |
|---|---|
| `pbatch/eval.py:82` — `def run_suite` | ✅ **Confirmed at line 82.** |
| `pbatch/eval.py:93` — iterates only `case.get("assert", {})` | ✅ **Confirmed, line shifted.** `asserts = case.get("assert", {})` at **line 89**; the loop at 92-105 iterates only `asserts.items()`. `expect` never executed. |
| `pbatch/eval.py:121` — `_suite_domains` reads `expect` | ✅ **Confirmed, line shifted by one.** `def _suite_domains` at **line 120**; reads `case.get("expect", {})` keys `task_type/system_type/domain/tier/matched_tier/effective_tier` for display only. |
| `evals/classifier.yaml:60` — system-type expect | ✅ **Confirmed at line 60** (`expect:` of `system-type-state-machine`). Case uses `name:` not `id:` (shows as `classifier/?` in reports; `--filter` cannot target it). |
| `evals/classifier.yaml:76` — system-type expect | ✅ **Confirmed at line 76** (`expect:` of `system-type-knowledge`). |
| `evals/rules.yaml:70` — design-intelligence expect | ⚠️ **Shifted.** `expect:` blocks at **lines 75, 86, 94, 104** (line 70 is `- id: business-ui-contract-skipped-on-demo`). All 4 cases use `name:` not `id:`. |
| `evals/rules.yaml:99` — design-intelligence includes | ⚠️ **Shifted.** Backend di `includes` list at **lines 95-100** (line 99 is `- design-intelligence-pipeline`). |
| `pbatch/classifier.py:206` — `def classify_text` | ✅ **Confirmed at line 206.** `system_type: str = "deterministic"` at :119, `_detect_system_type` at :190, `system_type` returned in `TaskClassification` at :247. |
| `pbatch/rule_matcher.py:202` — `def match_rules` | ✅ **Confirmed at line 202.** `_inject_system_type_methodology` at :243 injects `system-type-{system_type}` rule ids into `rules`; result carries `system_type` at :240. |
| `_assert_value` dispatch (eval.py:60-79) | ✅ `system_type`/`includes`/`excludes` absent; unknown keys → `ValueError: unknown eval assertion key` at :79. |

### 2.2 Live behavioral probes (this repository)

```
$ python -m pbatch eval --json        → total: 41, failed: 0, failures: []
$ python -m pbatch eval --quick --json  → total: 35 (rules 17 + classifier 18), failed: 0
```

All 10 expect-only cases PASS with zero assertions. The 6 classifier
system-type cases are reported as `classifier/?` (no `id:` key).

`classify_text` system_type — all 6 case expectations **match actual behavior**
(each would PASS once `system_type` becomes an executable assertion key):

| Case input | expect.system_type | actual |
|---|---|---|
| 订单审批流，状态流转，审核流程 | state-machine | state-machine ✅ |
| 事件驱动的消息队列通知 | event-driven | event-driven ✅ |
| 客户搜索检索，模糊匹配 | search | search ✅ |
| 生产排程调度，资源分配优化 | optimization | optimization ✅ |
| 知识库问答，文档检索 | knowledge | knowledge ✅ |
| 做一个登录页 demo | deterministic | deterministic ✅ |

`match_rules` rule-id manifests — 2 of 4 `rules.yaml` expect blocks match,
**2 diverge** (the divergence is currently masked by vacuous passing):

| Case | Expected (expect block) | Actual (verified) | Verdict |
|---|---|---|---|
| design-intelligence-demo-login | includes product, priority; excludes emotional | di ids = [priority, product] | ✅ match |
| system-type-methodology-routing | includes system-type-state-machine | rules include system-type-state-machine | ✅ match |
| design-intelligence-production-workbench | tier production; includes 6 di ids | `tier=standard`, **0 rules** (classifies `unknown`, generic domain; registry has no production scale keywords) | ❌ diverge |
| design-intelligence-backend-production | includes 6 backend di ids | classifies `frontend_ui` (the word "流程" flips it; ui-specs registry loaded; none of the 6 backend ids present) | ❌ diverge |

### 2.3 Correction evidence for the two divergent cases (input-text alignment)

The original inputs fail for reasons orthogonal to this direction
(classifier keyword routing). The workbench replacement is already proven
in `tests/test_design_intelligence.py:112`; the backend replacement is
manifest-equivalent to `tests/test_backend_experience.py:99` (identical
8-di-id manifest, tier production — verified live). Both produce exactly
the intended manifests:

| Case | Original input | Corrected input | Verified manifest |
|---|---|---|---|
| design-intelligence-production-workbench | 企业工作台：生产达成率 KPI + 异常列表 + 趋势图，生产 | **企业订单管理工作台，含 KPI 与趋势图表，生产环境** | `tier=production`, di = product, color, priority, cognitive, emotional, data (all 6 expected ✅) |
| design-intelligence-backend-production | 写一个订单创建接口，含支付审批流程，生产环境，go | **写一个订单创建接口，含支付审批，生产环境，go** | `task_type=backend`, `tier=production`, di = api, arch, datamodel, lifecycle, ops, pipeline (all 6 expected ✅; also distribution, maintenance — superset is fine for `includes`) |

`includes` semantics = all listed ids present (same as `has_rule`);
`excludes` = none present (same as `lacks_rule`).

### 2.4 CI wiring (why green is mandatory)

- `Makefile:40-41` — `make eval` runs `python -m pbatch eval` (exit 1 on any failure).
- `tests/test_entrypoints.py:52-68` — `test_eval_quick_mode_runs_core_domains` runs
  `eval --quick --json`, requires `returncode == 0`, `failed == 0`, `total >= 20`;
  `--quick` includes exactly the `rules` and `classifier` suites. Runs under `make ci`
  via `make test` (Makefile:37).

---

## 3. Requirements

- **R1 — Execute `expect` as assertions.** `run_suite` (pbatch/eval.py:82) must
  execute a case's `expect` block as its assertion set when `assert` is absent
  (cases with `assert` keep today's behavior; `expect` stays inert when `assert`
  is present). `_suite_domains` display behavior is unchanged.
- **R2 — New executable assertion keys** in `_assert_value` (pbatch/eval.py:60):
  - `system_type` — dispatched to `classify_text` (same call batch as
    `task_type/platform/profile/confident`; `TaskClassification.system_type`).
  - `includes` — rule-id list, all listed ids must be present in
    `match_rules(input)["rules"]` (same semantics as `has_rule`).
  - `excludes` — rule-id list, none of the listed ids may be present (same
    semantics as `lacks_rule`).
  - `tier` already supported; `expect.tier` resolves through the existing
    `match_rules` dispatch.
- **R3 — Fail closed on vacuous cases.** A case with neither `assert` nor any
  executable `expect` key (executable keys: `system_type`, `tier`, `includes`,
  `excludes`) is a schema error: `pi-batch eval` must exit **2** naming the
  offending suite/case, instead of passing. Display-only `expect` keys
  (`task_type`, `domain`, `matched_tier`, `effective_tier`) do **not** count as
  executable.
- **R4 — classifier.yaml system-type cases become real regression cases.**
  Rename the 6 cases' `name:` to `id:` (currently reported as `classifier/?`
  and un-targetable by `--filter`) so `expect.system_type` executes and the
  case is addressable (e.g. `--filter system-type-state-machine`). All 6
  expectations verified to match current classifier output (§2.2).
- **R5 — rules.yaml design-intelligence cases become real regression cases.**
  Rename the 4 cases' `name:` to `id:`; correct the two divergent inputs per
  §2.3 (keeping their original `includes`/`excludes` expectations, which are
  verified against the corrected inputs). The 2 already-matching cases stay
  byte-identical.
- **R6 — Suite stays green.** `python -m pbatch eval` exits 0 with **41** cases
  and 0 failures (direction's "40" corrected to the measured 41); `make eval`
  and `tests/test_entrypoints.py::test_eval_quick_mode_runs_core_domains`
  (total ≥ 20, failed == 0) keep passing.
- **R7 — Regression tests for the new mechanics.** Add pytest coverage:
  (a) `system_type` assertion key dispatches to `classify_text`;
  (b) `includes`/`excludes` assertion keys dispatch to `match_rules` rule-id
  sets; (c) vacuous case → exit 2 (monkeypatch `pbatch.eval.EVAL_DIR` to a
  temp dir containing a crafted suite — do not touch the real `evals/`).

---

## 4. Acceptance criteria (supplied by the direction, made testable)

- **A1.** Adding the `system_type` assertion key (dispatched to
  `classify_text`) makes `evals/classifier.yaml` `system-type-state-machine`
  actually check `'state-machine'`.
  *Test:* `python -m pbatch eval --filter system-type-state-machine --json`
  → `failed: 0`; the case's PASS is now backed by a real assertion
  (`expect.system_type: state-machine` executed; actual verified
  `state-machine`, §2.2). Verified by code inspection of the run (case no
  longer has an empty assertion set).
- **A2.** Adding `includes`/`excludes` assertion support makes all 4
  `rules.yaml` design-intelligence/system-type cases execute real assertions.
  *Test:* `python -m pbatch eval --filter design-intelligence-demo-login
  --json` (and each of the other 3 ids) → `failed: 0`; expected ids per
  §2.2/§2.3 manifests. The 2 divergent cases' inputs corrected (§2.3) so the
  execution passes; the 2 matching cases unchanged.
- **A3.** A case with neither `assert` nor supported `expect` keys fails
  `pi-batch eval` with exit code **2** (fail closed) instead of passing.
  *Test:* pytest with `EVAL_DIR` monkeypatched to a temp suite containing
  such a case → `SystemExit(2)` (or subprocess exit code 2); real `evals/`
  untouched. Implemented via R3.
- **A4.** `python -m pbatch eval --json` reports **41** cases (direction's
  "40" corrected: measured total = 6 assess + 18 classifier + 17 rules) with
  the previously vacuous 10 now exercising real assertions.
  *Test:* run the command → `total: 41, failed: 0, failures: []`; verify by
  inspection that every one of the 10 cases now carries an executable
  assertion set (`assert` or executable `expect`) — no case passes with zero
  assertions (guaranteed by R3 fail-closed).

---

## 5. Scope boundaries (explicitly excluded)

- **No classifier/rule_matcher behavior changes.** The `unknown`-task
  classification and generic-domain `tier: standard` for the original
  workbench input, and the `frontend_ui` classification for the original
  backend input ("流程" keyword), are pre-existing routing behaviors of
  `pbatch/classifier.py` / `pbatch/rule_matcher.py` — out of scope. Test
  inputs are aligned to verified behavior instead (§2.3).
- **No new/changed rules** in `ui-specs/rules.yaml` / `backend-specs/rules.yaml`;
  no changes to `scripts/check-design-intelligence.py` or
  `tests/test_design_intelligence.py`.
- **No changes to `evals/assess.yaml`** (6 cases, all already assert-backed).
- **No changes to `_suite_domains`** display keys or coverage output format.
- **No changes to `pi-batch eval` exit-code 1 semantics** for assertion
  failures (exit 2 is reserved for the new schema-level vacuous-case error,
  consistent with `load_eval_files`' existing exit 2).

---

## 6. Verification commands

```bash
python -m pbatch eval --json                     # A4: total 41, failed 0
python -m pbatch eval --filter system-type-state-machine --json    # A1
python -m pbatch eval --filter design-intelligence-demo-login --json   # A2 (×4 ids)
make eval                                        # R6: exit 0
python -m pytest tests/test_entrypoints.py::test_eval_quick_mode_runs_core_domains -q
python -m pytest tests/ -k "eval"                # R7 new tests
make ci                                          # full gate set stays green
```
