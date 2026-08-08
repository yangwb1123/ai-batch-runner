All verification complete. Here is my review.

---

# Testing Review — Nested-function double-counting fix (campaign `fix-nested-function-double-counting-in-complexit-fe534813`)

**Scope note**: this review runs at the `adversarial_review` stage — the implementation does **not exist yet**. `checks/complexity.py` still contains the bug (verified live: wrapper scores cyclomatic=4/cognitive=6). I therefore review the design's implementation + test plan for sufficiency, per `backend-specs/testing.md` (reproduce-first, risk coverage, property/fuzz) and `product-specs/completion-evidence.md` (claims must name commands/results).

## 1. Verdict

```
VERDICT: PASS - AC1/AC2/AC3 test plan is sufficient to prove the double-count defect cannot return (exact 1/0 + 4/6 assertions on both metrics, check_tree-level no-fail-open pin, 14 existing tests unmodified, live cli run); all design claims independently re-verified live including a re-derived prune prototype (12/12 cases correct). Two Medium conditions must be enforced at design_gate/implement: (a) the 9-case matrix with expected values must be materialized on disk (currently claimed but absent from the artifact — implement consumes this artifact), (b) reproduce-first evidence (pre-fix failing test output) must be reported in the change report since the implement prompt does not mandate it.
```

## 2. Findings

| # | Severity | Defect pattern | Missing test / gap | Exact test case that would catch it |
|---|---|---|---|---|
| F1 | **Medium** | Design artifact claims a "9-case matrix" and "6 failure modes" but contains neither — the on-disk design is a summary pointing at itself. The implement stage consumes this artifact; matrix cases (def-in-if, def-in-match, class-in-function, decorator-lambda attribution) can be silently dropped. | Matrix as concrete subTest expectations with expected values, on disk. | `subTest` each: `def outer(): def inner(): if a: if b: if c:` → outer cyc==1/cog==0, inner 4/6; `def outer(): if a: def inner(): if b:` → outer 2/1; `def outer(): match x: case 1: def inner(): if b:` → outer 2/1; `def outer(): class C: def m(): if a: if b:` → outer 1/0, m 3/3; `@deco(lambda v: v if x else y)` on inner → outer 1/0, inner 3/2. **All 12 expected values verified correct** against an independent re-derivation of the prune prototype. |
| F2 | **Medium** | Reproduce-first not enforced anywhere: pipeline implement prompt says "Add focused regression tests" — no write-test → confirm-FAIL → fix sequence. `testing.md` §2 mandates it; post-hoc tests can't prove the defect was seen failing. | Change report must quote the pre-fix failing run of AC1. | `python -m pytest checks/test_complexity.py::TestNestedFunctions -q` before touching `complexity.py` → must show `assert cyclomatic(outer)==1` failing with actual `4` (verified: current code yields 4/6 for the AC1 fixture). |
| F3 | Low | No property/fuzz test for the traversal. A prune regression (new node type added to `_NESTING_NODES`, wrong-level prune check) is invisible to 1–2 fixed fixtures. `testing.md` §4 calls for property tests on processing logic. | 3-line property over generated nesting templates. | For ~20 generated sources (random nesting of if/for/while/with/try/match × def/async def/class at depth 1–3): assert `cyclomatic(fn) == 1 + manually_counted_own_branches(fn)` and every branch construct in the file's subtree is counted in exactly one function's score. |
| F4 | Low | "class methods" in the matrix is ambiguous — class-inside-function (method branches must not leak into the enclosing function) is the non-trivial variant and is not pinned. | Explicit class-in-function subTest. | `def outer(): class C: def m(self): if a: if b:` → outer cyc==1/cog==0, m cyc==3/cog==3 (verified correct under the proposed prune). |
| F5 | Info | Recursion depth of `cognitive` — moot: CPython parser caps indentation at 100 levels and nested parens at ~200 (`too many nested parentheses`), so valid sources cannot reach `RecursionError`. The design's iterative cyclomatic choice is still correct style; no test needed. | — | — |
| F6 | Info | `AC2 "50 in checks/"` consistency: 49 current + 1 new test method (subTests in one method) = 50 ✓; the 14 existing tests stay unmodified ✓; AC3 monotonicity argument is sound (prune only removes branch points, `_functions` unchanged, per-function scores cannot increase, baseline exit 0 verified ⇒ exit 0 preserved; `ignore_patterns` untouched, config.py:49-51). | — | — |

## 3. Risk-coverage matrix

Domain-adapted: this is a pure stdlib ast traversal with no I/O, concurrency, or shared state — the checklist's I/O-domain risks (async reordering, 409, permissions, unmount, pagination, timezone, partial success) are N/A here; their analogues are traversal/pruning boundaries.

| Risky path | Covered? | Evidence |
|---|---|---|
| Double-count core (duplicate-submit analogue) | ✅ | AC1 exact 1/0 + 4/6, both metrics; verified this test fails on current code |
| Partial fix (cyclomatic-only or cognitive-only) | ✅ | AC1 subTests assert **both** metrics |
| Fail-open wrong fix (`_functions` → top-level only) | ✅ | check_tree-level no-fail-open pin (design decision, explicitly called out) |
| Over-prune (def-inside-if/match/try) | ⚠️ | In claimed matrix, **not on disk** (F1) — verified semantics correct: 2/1, 2/1, 2/1 |
| Class-inside-function boundary | ⚠️ | Not pinned (F4) — verified semantics correct under recursive prune: outer 1/0, method 3/3 |
| Decorator-expr attribution (lambda in decorator) | ⚠️ | Claimed matrix case, not on disk; ambiguity on expected value (probe: decorator counted in decorated function, inner 3/2, outer 1/0 — gocyclo-consistent, must be pinned) |
| Async def/for/with | ⚠️ | Claimed "async" case; `AsyncFor`/`AsyncWith` already in `_BRANCH_NODES`; verified wrapper 1/0 |
| Flat semantics unchanged | ✅ | 9 existing metric tests (5 cyclomatic + 4 cognitive) unmodified + matrix "flat unchanged" |
| Recursion blowup | ✅ | Parser-bounded (≤100 indent, ≤200 parens) — not reachable via valid Python |
| `ignore_patterns` drift | ✅ | AC3 live exit-0 + no diff; `is_ignored`/config untouched |
| Syntax error / non-.py files / scan exclusion | ✅ | Existing tests unmodified (test_syntax_error_fails, test_non_py_passes, test_run_scan_excludes_generated_dirs) |
| Layers: unit / integration / contract / E2E / non-functional | ✅ | unit = AC1; integration = check_tree pin + AC2 pytest; contract = zero API change + 14 unmodified tests; E2E = `cli.py check-complexity` live; non-functional = monotonicity argument (F3 property test optional) |

## 4. Honesty audit (completion-evidence)

**Design claims vs. executed verification — I re-ran everything independently:**

| Claimed | Claimed command/result | My independent run | Verdict |
|---|---|---|---|
| Bug reproduces: outer 4/6, inner 4/6 | (analysis fixture) | Same output | ✅ Verified |
| `pytest checks/` = 49 passed | — | `49 passed in 0.16s` | ✅ Verified |
| `cli.py check-complexity` exits 0 | — | exit 0 | ✅ Verified |
| 14 existing tests, 9 with metric expectations | — | 14 collected; 9 metric tests (5+4) | ✅ Verified |
| 6 line citations, no shifts | — | `complexity.py:73-79` `_functions` ✓, `:48-52` cyclomatic walk ✓, `:82-89` check_tree ✓, `:93-117` = `cognitive(fn)` call + check_file/run flow ✓, `config.py:165-167` is_ignored ✓, `test_complexity.py:9-14` `_fn` ✓ | ✅ Verified |
| 9-case matrix "validated" | — | Re-derived prototype; **12/12 cases correct** including class-in-fn and decorator-lambda | ✅ Semantics correct, **but matrix content not on disk** (F1) — a claim of validation without the artifact of validation, violating the "已运行/未运行可区分" rule |
| AC3 monotonicity | argument + live exit-0 | Argument independently checked: prune ⊆ old node set, `_functions` unchanged ⇒ scores can only decrease; baseline exit 0 ⇒ exit 0 guaranteed | ✅ Verified |

**Gaps with reasons**: no fabricated command results found; every command the design cites matches my execution. The one honesty deficiency is F1 — the design asserts validation artifacts (matrix, failure-mode table, step sequence, re-enumerated FRs) that exist only as claims in a summary file. Per `completion-evidence.md` §2, claimed-but-absent evidence must be listed as `not_executed` with a reason or materialized; the design gate should require the matrix on disk before `implement` consumes it. `not_executed` correctly stated for nothing else; no skip-to-pass, no "理论上应该通过" phrasing found (all pass claims name commands that I confirmed pass).

---

**Conditions for the design gate** (non-blocking for the design's core approach, but required before `implement`): F1 — materialize the 9-case matrix (with the 12 verified expected values) and 6 failure modes in `task-1-design.md`; F2 — extend the implement prompt to require the pre-fix failing AC1 output in the change report; F4 — pin class-in-function and decorator-lambda attribution explicitly. F3 optional.
