All key facts independently re-verified live before synthesizing: bug reproduces exactly (outer 4/6, inner 4/6, `_functions` collects both), baseline `checks/` = 49 passed, `test_complexity.py` = 14 passed, `cli.py check-complexity` exit 0; design artifact is 19 lines / ~2.4KB (claims matrix/failure-modes/sequence it does not contain); meta role outputs **are** on disk (resolves qa_lead's transient Info); `quality.py:32-42` + `:85-93` confirmed to share the identical walk pattern (M4 real); working tree confirmed concurrently mutated (AGENTS.md/Makefile/README/docs modified, other campaign archives deleted → flake risk real).

---

# Principal Review — Nested-function double-counting fix (`checks/complexity.py`), design stage

## 1. Advisory Recommendation

**Conditionally ready — proceed to implement only after the design-gate conditions in §4 are met (primarily H1: materialize the claimed matrix/failure-modes/sequence in the on-disk artifact, or delete the claims).**

Confidence: **High** on the technical approach (three independent reviewers + my own live run reproduce identical expected values; all three re-derived the prune prototype with consistent 12-case results; algorithm_reviewer's repo-wide diff shows 9 over-scored / 0 under-scored, monotone non-increasing). **High** on the blocking gap (I read the 19-line artifact myself; it self-references claims that are absent). **Medium** on pipeline completion: the implement stage consumes this artifact, and the working tree is being concurrently mutated by other campaign processes, so the AC2/AC3 numbers must be re-measured at implement time, not inherited from design.

This is advisory; the design_gate stage and maintainers retain binding authority.

## 2. Consolidated Findings (deduplicated, by severity)

**H1 — Design artifact does not contain the design it claims (traceability/pipeline blocker).**
*Sources*: qa_lead H1 (High), testing_reviewer F1 (Medium), algorithm_reviewer §4 (documentation gap). *Dedup*: one finding, three framings. The 19-line `task-1-design.md` names a 9-case matrix, 6 failure modes, step-by-step implementation sequence, and re-enumerated FRs — none are on disk (verified: `wc -l` = 19; requirements.md likewise claims 7 FRs without enumerating them). The three cognitive return-path swaps (BoolOp/Match/generic) are asserted, not specified. *Impact*: implementer cannot reproduce the validated detail; matrix cases (def-in-if/match, class-in-function, decorator attribution) can be silently dropped. *Resolution*: materialize the matrix with the 12 verified expected values, the failure-mode table with detection, and the sequence into the artifact before `implement` consumes it — or explicitly delete the unverifiable claims per `prompts/README.md` evidence standard.

**M1 — Prune formulation not pinned; two distinct fail-open variants possible.**
*Sources*: qa_lead H2 (root-skip walker ⇒ every function scores 1; probe B verified), algorithm_reviewer Medium #1 (root-only filter ⇒ class-wrapped methods leak into outer, `def f(): class C: def m(): if a: if b:` gives outer 3/3 instead of 1/0, verified by construction). *Dedup*: same root cause (underspecified `_own_children` formulation), two failure modes. *Fix*: pin the parent-filter formulation in the artifact + add flat if-chain assertion (cyc==3) and class-in-function subTest to AC1.

**M2 — Matrix cases have no committed regression tests; decorator/class attribution semantics unpinned.**
*Sources*: qa_lead M3, testing_reviewer F4, algorithm_reviewer Medium #2. Async wrappers, decorator factories/exprs, class-in-function, def-in-if/match, depth-3 are prototype-validated only. Cognitive Match-branch and BoolOp-branch prunes entirely untested. Expected values consistent across reviewers (def-in-if outer 2/1; def-in-match outer 2/1; class-in-fn outer 1/0, m 3/3; decorator-lambda inner 3/2 per testing; module-level branchy decorator outer 2/1, inner 2/1 per qa — different fixtures, not conflicting). *Fix*: one subTest per matrix case inside the single AC1 method (preserves the 50-count pin).

**M3 — Reproduce-first evidence not mandated.**
*Source*: testing_reviewer F2. The implement prompt says only "add focused regression tests"; `backend-specs/testing.md` §2 and the completion-evidence contract require the pre-fix failing run quoted. `python -m pytest checks/test_complexity.py::TestNestedFunctions -q` before touching `complexity.py` must show `assert cyclomatic(outer)==1` failing with actual 4. This aligns with the repo's `check-completion-report.py` DoD gate (no fabricated/absent verification).

**M4 — `quality.py` shares the identical double-count defect; out of scope.**
*Source*: qa_lead M4 (verified by me: `quality.py:32-42` `_complexity` full-subtree `ast.walk`; `:85-93` collects every FunctionDef). This is the `pyquality` validator that the implement stage itself runs and part of `make ci`. Scope decision (exclude) is defensible and documented; the stated value ("false-positive gate failures → ignore_patterns widening") remains live in `make ci`. Record a follow-up direction; do not expand this change.

**M5 — Acceptance omits aggregate gates.**
*Source*: qa_lead M5. AC2/AC3 cover `pytest checks/` and `check-complexity` only; the file also participates in `cli.py check`, `cli.py check-test` (unittest discover / `make harness`), and `make ci`. Add all to acceptance; run `make ci` after the tree settles.

**L1 — Optional property test for traversal.**
*Source*: testing_reviewer F3. A prune regression on new node types is invisible to 1–2 fixed fixtures; a 3-line property over ~20 generated nesting templates is cheap. Low value for a CI gate with exact-value oracles; leave optional.

**L2 — `_own_children` list-comp allocates per node.**
*Source*: algorithm_reviewer Info. Generator expression suffices; no benchmark needed (CI gate, µs scale). Low.

**L3 — Recursion depth (resolved as non-issue).**
*Sources*: testing_reviewer F5 + algorithm_reviewer Low, merged. CPython parser caps (≤100 indent, ≤200 parens) make `RecursionError` unreachable via valid source; the iterative cyclomatic choice is still correct style. Optional defense-in-depth `try/except RecursionError` per file.

**I1 — Lambda bypass (documented non-goal).**
*Source*: qa_lead L6. Branchy lambda `IfExp` still counts inside the enclosing function; no double count, attribution diverges from gocyclo. Acceptable; record as future direction.

**I2 — Meta outputs on disk (resolved).** qa_lead's transient observation (empty meta dir at review time) is resolved: `algorithm_reviewer.md`, `qa_lead.md`, `testing_reviewer.md` all present under `artifacts/adversarial_review-9c87f3a7/meta/`.

**Minor cross-review inconsistency (non-conflict):** case-matrix counts differ (testing/qa: 9; algorithm: 10 incl. nested-chain benchmark). Different taxonomies, identical expected values; no action beyond pinning the canonical list on disk (H1).

## 3. Trade-off Ledger

| Conflict | Options | Recommendation | Consequence | Decision owner |
|---|---|---|---|---|
| H1: implement on summary vs. hold for full artifact | (a) Proceed — implementer has prototype-verified design in review transcripts; (b) hold — rewrite artifact first | **(b) hold at design_gate**; the gate exists precisely for this; transcripts are not consumable by the implement task | (b) delays implement by one gate cycle; (a) risks silent matrix-case drift with no detection (fail-open on match-wrapped defs) | design_gate / pipeline maintainer |
| Decorator attribution semantics: decorator counts in enclosing function (today) vs. in the def it decorates (gocyclo-consistent) | Both defensible; behavior change: outer scores decrease (e.g. 2/1 → 1/0) | Adopt gocyclo-consistent attribution, **pinned by test** | Monotone-decreasing scores only — no function can newly fail (verified repo-wide: 9 decreases / 0 increases); gate strictness unchanged in direction | Maintainers to ratify (gate scoring semantics change), implement engineer to pin |
| Test granularity vs. AC2 count pin | (a) one method + subTests (keeps 50/15 counts); (b) separate methods (breaks counts, clearer failures) | **(a)** — counts are part of the acceptance contract and flake-prone already | Slightly less granular failure reporting; acceptable with `subTest` names | implement engineer / qa_lead |
| `quality.py` in scope vs. follow-up | Fix both (expands change, fixes `pyquality` now) vs. fix `complexity.py` only + record follow-up | **Follow-up direction** (documented exclusion, per qa_lead and requirements out-of-scope list) | `make ci`'s `pyquality` keeps over-scoring until follow-up lands; implement stage self-gate unchanged | Campaign maintainer |
| Iterative vs. recursive cyclomatic walk | Both correct at parser-bounded depths | **Iterative** (design's choice) — avoids introducing recursion where today's `ast.walk` is deque-iterative; no regression | Slightly more code; zero risk of recursion regression | algorithm_reviewer (agreed) |
| AC2/AC3 measured on moving tree | Inherit design numbers vs. re-measure at implement | **Re-measure at implement**; pin per-file `test_complexity.py == 15`, re-run AC3 live in implement stage | Guard against concurrent-campaign drift (4+ processes observed) | implement stage |

## 4. Preconditions, Checks, Rollback, Monitoring

**Preconditions (design_gate must verify before implement):**
1. **H1**: on-disk `task-1-design.md` contains the matrix (with the 12 verified expected values), 6 failure modes with detection, and step sequence — or the claims are deleted; requirements.md enumerates the 7 FRs.
2. **M1**: parent-filter `_own_children` formulation pinned; flat if-chain (cyc==3) and class-in-function assertions named in AC1.
3. **M2**: AC1 subTests cover all matrix cases incl. decorator attribution and Match/BoolOp prune paths.
4. **M3**: implement prompt requires pre-fix failing AC1 output quoted in the change report.
5. **M5**: acceptance extended to `cli.py check`, `cli.py check-test`; `make ci` scheduled after tree settles.
6. **M4**: follow-up direction recorded (docs/feature-matrix.md or deferred backlog per AGENTS.md §5).

**Executable acceptance checks (implement/gate):**
- `python -m pytest checks/test_complexity.py -q` → **15 passed** (14 unmodified + 1 new method)
- `python -m pytest checks/ -q` → **50 passed** (re-measured at implement time)
- `python cli.py check-complexity` → exit **0**
- `python cli.py check` → exit 0; `python cli.py check-test` → exit 0
- `python quality.py .` → unchanged pass (pyquality validator on implement stage)
- AC1 fixture: outer (1,0), inner (4,6), both metrics; fail-open pin: nested def over budget still fails `check_tree`; flat cyc==3
- No `ignore_patterns` diff: `git diff -- checks/config.py` empty for config

**Rollback triggers** (single-commit revert; no migration): any of the 14 existing tests fail; any function's score **increases** vs. baseline (monotonicity invariant — algorithm_reviewer verified 0 increases today); `check-complexity` exit code changes on an unchanged tree; gate failure counts change without code change (config/ignore_patterns drift).

**Monitoring:** post-merge CI: compare `check-complexity` violation reports before/after (expect the 9 over-scored functions to drop — worst `pbatch/hypergraph.py:177` 9/14 → 2/1); watch that no new violations appear on files that were previously clean; `make ci` green on a settled tree.

**Explicit exclusions:** `ast.Lambda` handling (I1); `quality.py` complexity (M4, follow-up); cognitive metric definition; `ignore_patterns`/config changes; all other gates (backendquality/uiquality); perf optimization (the ~1.5× O(F·N)→O(N) win is a side effect, not a requirement).

**Residual risks:** (a) concurrent campaign mutation of `checks/` invalidates inherited counts — mitigated by re-measure precondition; (b) if H1 is waived, matrix cases silently drop with no detection (fail-open on match-wrapped defs); (c) property test is optional (L1) — a future prune regression on new node types is only caught by fixed fixtures; (d) decorator-attribution behavior change alters gate scores for nested-def-heavy files (decreases only) — requires maintainer ratification, not just QA sign-off.

## 5. Missing Reviews / Evidence and Next Actions

**Missing:** (1) design_gate output for the *current* cycle — the git log shows a design_gate only for the previous cycle (`39a6021`); the gate must rule on H1/M1 explicitly. (2) `make ci` full suite (387 tests + eval + org gate) — deliberately not run on a moving tree; mark Missing, schedule after the tree settles. (3) The matrix/failure-modes/FR enumeration on disk (the core evidence gap). (4) Pre-fix failing test output (will exist after implement, per M3). (5) No architect/security review exists — **deliberately acceptable**: pure-stdlib local AST traversal, no I/O/concurrency/shared state (confirmed by all three reviewers and by code reading); no security/protocol surface. Do not add these roles.

**Narrow next actions (each verifiable):**
1. design_gate: verify H1 artifact completeness (or claims removed) + M1 formulation pinned → VERDICT PASS/FAIL.
2. Implement stage: execute M3 sequence — write AC1 (subTests: all matrix cases + fail-open pin + flat assertion), confirm FAIL pre-fix, apply `_own_children` + iterative walk + three cognitive swaps, quote pre-fix output in change report, run AC2/AC3 re-measured + `cli.py check`/`check-test`.
3. Record M4 follow-up direction + I1 lambda non-goal in the deferred/feature list.
4. Maintainers: ratify decorator-attribution semantics (gate scoring change) — this is the only decision in this review requiring maintainer/CTO authority; everything else is within QA/gate discretion.

No fabricated sign-offs: no maintainer, CTO, or design_gate approval is claimed or implied by this review; the previous-cycle design_gate PASS does not cover the current artifact revision.
