I have everything needed. All claims verified live. Here is the QA lead review.

---

# QA Lead Review — "Fix nested-function double-counting in complexity gates" (design stage)

**Review target:** `docs/auto/runs/fix-nested-function-double-counting-in-complexit-fe534813/artifacts/design-a77de8a6/task-1-design.md` (19 lines, 2443 bytes)
**Status of implementation:** none — `checks/complexity.py` and `checks/test_complexity.py` are unmodified in the working tree (git status shows no `checks/` changes; run git log = requirements + design commits only).

## 1. Test inventory and commands actually run (this revision)

| Command | Result | Purpose |
|---|---|---|
| `python -m pytest checks/ -q` | **49 passed** | Baseline suite (AC2 precondition) |
| `python -m pytest checks/test_complexity.py -q` | **14 passed** | Target file (14 existing tests, not 13) |
| `python -m unittest discover -s checks -p 'test_*.py'` | **exit 0** | `cli.py check-test` / Makefile `self-test` equivalent |
| `python cli.py check-complexity` | **exit 0** | AC3 precondition |
| `python cli.py check` | **exit 0** (existing style WARNs) | Aggregate gate `make check` |
| Empirical fixture reproduction | outer 4/6, inner 4/6 — **double count confirmed exactly** | Direction problem claim |
| **Prototype validation** (design's `_own_children` + iterative pruned walk, monkeypatched) | 9-case matrix **all pass**; **14/14 existing tests pass** against prototype | Feasibility claim, verified independently |
| Fail-open probes (see §3) | A: pin works; B: root-skip variant ⇒ all scores 1; C: top-only `_functions` ⇒ **fully silent fail-open** | Design's risk pins, necessity confirmed |

**Not run this revision:** `make ci` (full 387-test + eval + org gate), `pytest tests/`, `coverage`. The tree is being concurrently mutated by other campaign directions (see §5) — full-suite runs now would measure a moving target. Marked **Missing** for exit criteria.

## 2. Requirement-to-test matrix

Requirements/FRs are **not fully on disk**: `requirements.md` is a ~24-line summary (states "7 functional requirements" without enumerating them); the design summary says "FRs were re-enumerated in the design to be self-contained" but the on-disk design artifact does not contain that enumeration either. Matrix below maps the direction's acceptance criteria + design invariants (all claims **Verified** by my own runs unless marked Planned).

| Requirement / invariant | Test (planned or existing) | Status | Evidence |
|---|---|---|---|
| AC1: wrapper scores only own body (cyclomatic==1, cognitive==0) | New subTest in AC1 method | **Planned** — exact numbers verified feasible | Prototype: outer (1, 0) |
| AC1: inner closure scores own branches once (4/6 fixture) | Same | **Planned** — verified feasible | Prototype: inner (4, 6) |
| AC1 pin: nested def over budget must still fail `check_tree` (no fail-open) | check_tree-level assertion | **Planned** — pin is *necessary* | Probe C3: top-only `_functions` + pruned metrics ⇒ **0 failures with inner=4 > budget 2** (fully silent fail-open) |
| Invariant: `_functions()` keeps collecting nested defs | Only via the pin above; no direct unit test | Planned (indirect) | — |
| AC2: `pytest checks/` = 50 (49 + 1), 14 existing unmodified | New method + existing | Baseline **Verified** (49 passed); target 50 **at risk** (§5 flake) | §1 rows 1–2 |
| AC3: `cli.py check-complexity` exit 0, no ignore_patterns diff | Live command | Baseline **Verified**; monotonicity argument sound (scores only decrease; nested defs were already scored individually) | §1 row 4 |
| FR: async wrappers prune correctly | **Not covered** by planned tests (matrix-only claim) | Gap | Prototype OK, no committed test |
| FR: decorator factory / branchy-decorator attribution | **Not covered** | Gap | Prototype OK incl. module-level branchy decorator (outer 2/1, inner 2/1) |
| FR: class methods unaffected | **Not covered** | Gap | Prototype OK |
| FR: def-inside-`if` / def-inside-`match` (cognitive Match-branch prune) | **Not covered** | Gap | Prototype OK |
| FR: depth-3 | **Not covered** | Gap | Prototype OK |
| FR: flat functions unchanged | Covered by existing 14 tests | **Verified against prototype** (14/14 pass) | §1 |
| Quality gates unaffected | `cli.py check` | **Verified** exit 0 | §1 |

## 3. Findings (by severity)

**H1 — Design artifact does not contain the design (pipeline blocker risk).**
The deliverable is a 19-line summary. It *names* 6 failure modes and a "step-by-step implementation sequence" but the enumeration, detection methods, and sequence are **not on disk**; the "acceptance mapping" is one line. The implement stage consumes this file (pipeline `implement` aggregates requirements+design+gate). An implementer cannot reproduce the claimed detail. *Impact:* implementation drifts from the validated matrix; the three cognitive return-path swaps (BoolOp/Match/generic) are only asserted, not specified. *Recommendation:* before implement, rewrite the design artifact with the full content (or accept that the summary is the spec — then explicitly delete the unverifiable claims, per `prompts/README.md` evidence standard). *Validation:* `wc -l` the artifact; it must contain the matrix + failure modes + sequence it claims.

**H2 — Root-skip walker variant silently scores everything as 1 (fail-open) — no committed guard beyond existing flat tests.**
My first prototype of "iterative pruned walk" skipped the root `FunctionDef` and every function scored 1 (flat `if`-chain scored 1 instead of 3). The design's `_own_children` formulation (filter at parent) avoids this, and existing tests (`test_if_chain` etc.) would catch it — **but only if the implementation follows the parent-filter formulation exactly.** The design does not pin the formulation. *Recommendation:* add the flat `if`-chain assertion (cyc==3) into the new subTests as an explicit "prune must not touch flat functions" case (it is currently implicit in old tests, which is sufficient only if `_functions`-level semantics hold).

**M3 — 7 of 9 matrix cases have no committed regression tests.**
AC1's subTests cover wrapper/closure + fail-open pin; the matrix claims (async, decorator factories, class methods, def-in-`if`/`match`, depth-3, decorator attribution) are validated only by my prototype run, not by committed tests. The cognitive **Match branch and BoolOp branch prunes are entirely untested** (AC1 fixtures exercise only the generic branch). *Regression risk:* a future change that reintroduces double count only for match-wrapped defs passes CI. *Exact test to add:* in the single AC1 method, one subTest per matrix case, e.g. `subTest("def-in-match")` asserting outer (2, 1) / inner (2, 1) — values I verified live. Keeps AC2's 50-count pin (one method, many subTests).

**M4 — `quality.py` has the identical double-count defect and is out of scope.**
Verified: `quality.py:32-42` `_complexity()` walks the full subtree via `ast.walk`; `:85-93` collects every FunctionDef. This is the `pyquality` validator (`pi-batch.yaml`) that the implement stage itself runs, and part of `make ci`. The scope decision ("other gates" excluded) is defensible and documented, but the direction's stated value ("false-positive gate failures... pressure to widen ignore_patterns") remains live in `make ci`. *Recommendation:* record a follow-up direction ("same prune for quality.py complexity"); do **not** expand this change. *Validation:* `python quality.py .` on a wrapper-heavy module shows the same inflation.

**M5 — Acceptance omits the aggregate gates.**
AC2/AC3 cover `pytest checks/` and `check-complexity` only. The change touches a file inside `make check` and `make ci` (self-test via unittest discover). *Recommendation:* add `python cli.py check` and `python cli.py check-test` (or `make harness`) to acceptance; `make ci` after the tree settles.

**L6 — Lambda bypass documented as out of scope.** The rejected predecessor direction ("lambdas bypass the gate") confirms awareness; a branchy lambda's IfExp is still counted inside its enclosing function (no double count, but attribution divergence from gocyclo). Acceptable non-goal; recommend a future direction.

**Info — Traceability:** this run's `adversarial_review` meta dir was empty at review time (orchestrator PASSED per memory index at 15:38:21); verify role outputs land on disk for the gate stage. Also: design claims "all 6 line citations confirmed" — I re-confirmed 4 (`complexity.py:48-52, 73-79, 82-89, 93-117`, `config.py:165-167`, `test_complexity.py:9-14`) by reading the files; no line shifts.

## 4. Prioritized scenario list (happy → boundary → error → race → recovery)

1. **Happy:** wrapper + nested closure — outer (1, 0), inner (4, 6), both metrics. *(AC1, verified)*
2. **Happy:** flat function scores unchanged after prune. *(covered by 14 existing; add explicit subTest)*
3. **Boundary:** nested def just at budget (15/20) — outer 1/0, inner 15/20 both pass; inner 16/21 fails. *(pin test; values verified)*
4. **Boundary:** async wrapper + async inner; depth-3 chain. *(M3 gap)*
5. **Boundary:** decorator-expr on inner (`@(flag if mode else other)`) attributed to inner only; on module-level outer attributed to outer. *(M3 gap; verified outer (2,1), inner (2,1))* — note: decorators are children of the FunctionDef, so the parent-filter formulation must keep them (my prototype does).
6. **Boundary:** class methods with nested defs; def-inside-`if`/`match` (cognitive Match branch). *(M3 gap)*
7. **Error:** syntax-error file still fails check_file (existing test covers; unchanged by design — but design should state it).
8. **Error/fail-open:** `_functions()` regresses to top-level-only ⇒ inner complexity silently unchecked — must FAIL the pin. *(probe C3: today it yields 0 failures; the pin is the only guard)*
9. **Error/fail-open:** walker skips root ⇒ all scores 1 (H2) — caught by flat assertions.
10. **Race:** not applicable — single-process, pure functions, no shared state (Verified by code reading); determinism trivially holds (sums independent of traversal order).
11. **Recovery:** gate returns failure count, no partial state; `cli.py check-complexity` exit code reflects it (existing behavior, unchanged).

## 5. CI/manual-suite gaps, flake risks, fixtures, exit criteria

- **CI gaps:** AC omits `make harness`/`cli.py check`/`make ci` (M5); `quality.py` double-count not addressed (M4, follow-up); no coverage requirement — line coverage would not establish adequacy here anyway; behavioral assertions are the right oracle (they exist and are exact-value based — good, oracle-safe).
- **Flake risk — concurrent campaigns:** 4+ `pi-batch campaign` processes running; working tree already carries their edits (`AGENTS.md`, `Makefile`, `README.md`, `docs/*` modified; archived-run files deleted). AC2's absolute "50 in checks/" and AC3's "exit 0 on current repo tree" are measured on a **moving tree**. Another direction touching `checks/` tests shifts the count. *Mitigation:* pin per-file count (`test_complexity.py` == 15) and re-measure `checks/` at implementation time; re-run AC3 in the implement stage, not inherited from design.
- **Fixtures needed:** none — `_fn` helper already supports multi-line bodies (`test_complexity.py:9-14`); new test needs no filesystem, tempdirs, or monkeypatching (existing TestCheckFile uses `mkdtemp` + `try/finally` restored `repo_root` — pattern is sound; new test should assert on integer return counts, not captured stdout, to avoid brittle diagnostics coupling).
- **Exit criteria (my recommendation):** (1) AC1 method with subTests covering §3-M3 matrix cases + fail-open pin — all passing; (2) 14 existing tests unmodified and passing; (3) `test_complexity.py` == 15, `checks/` == 50 (re-measured); (4) `cli.py check-complexity` + `cli.py check` + `cli.py check-test` exit 0; (5) `python quality.py .` unchanged-pass (pyquality validator on implement stage); (6) after tree settles: `make ci` green. (7) H1 resolved — design artifact on disk must contain the matrix/failure-modes it claims, or claims must be removed.

**Overall:** the design's core approach is **correct and feasible** — my independent prototype reproduces the exact AC1 numbers, all 9 matrix cases, and passes all 14 existing tests. The blocking issue is traceability (H1: artifact completeness), and the load-bearing risk control (the `_functions()`-unchanged fail-open pin) is both necessary and correctly planned. Medium gaps are the untested matrix cases and the aggregate-gate acceptance.
