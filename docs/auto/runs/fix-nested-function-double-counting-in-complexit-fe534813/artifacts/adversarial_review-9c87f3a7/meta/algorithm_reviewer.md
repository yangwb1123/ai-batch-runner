All 14 existing tests pass with the fixed implementations (failures: 0, errors: 0 — the `FAIL:` lines are expected fixture output). I have everything needed for the review.

---

# Algorithm Reviewer Report — Nested-Function Double-Counting Fix (`checks/complexity.py`)

**Scope reviewed**: design summary at `docs/auto/runs/fix-nested-function-double-counting-in-complexit-fe534813/artifacts/design-a77de8a6/task-1-design.md`; live code `checks/complexity.py` (cyclomatic :48-52, cognitive :93-117, `_functions` :73-79), `checks/config.py:165-167` (`is_ignored`), `checks/test_complexity.py:9-14` (`_fn`).

**Verification performed this review** (all live, Python 3.10.7): double-count fixture reproduced (wrapper cyclomatic=5/cognitive=6 vs its own 1/0 — design's 4/6 fixture differs only in branch count, mechanism identical); full-corpus diff of current vs design's fix = **9 functions over-scored, 0 under-scored**; 14/14 existing tests pass unmodified with the fix applied; 10-case matrix re-derived and executed (wrapper, closure, async, def-in-`if`/`match`, depth-3, decorator-expr, decorator factory, class-in-function, flat, nested-chain perf benchmark).

## 1. Findings

| Sev | Defect pattern | Evidence (file/line) | Current complexity | Proposed alternative (decision table) | After fix |
|---|---|---|---|---|---|
| **High** (defect being fixed) | Nested-def double-count: `ast.walk(fn)`/`iter_child_nodes` descend into nested `FunctionDef`s, whose branches count in the outer **and** the def is scored separately by `_functions()` | `checks/complexity.py:50` (walk), `:110` (child iteration); live: wrapper 5/6 vs own 1/0; **9 real repo functions over-scored** — worst `pbatch/hypergraph.py:177 depends_on_cycles` 9/14 → real 2/1; the tool even over-scores its own `cognitive` fn (4/3 → 1/0) | O(F·N) worst-case per file (nesting chain re-walked by every ancestor) + inflated scores | Per-node `_own_children()` prune of `FunctionDef`/`AsyncFunctionDef`; iterative explicit-stack DFS for cyclomatic (decision table: *"DFS → Stack（深度大用显式栈防溢出）"*; AST is a tree → *"图遍历必须维护 visited…除非结构保证无环"* — no visited set needed); cognitive swaps `iter_child_nodes` → `_own_children` in all 3 return paths | O(N) total per file; correct scores |
| **Medium** | Ambiguity: `_own_children` must be applied at **every** node, not just the scored function's direct children — otherwise class-wrapped methods leak (`def f(): class C: def m(): …` — `m` is f's grandchild) and O(F·N) remains | class-in-fn case: per-node filter gives outer (1,0), m (3,3) ✓; root-only filter would give outer 3/3 (verified by construction) | — | Per-node filtering (design matrix's "class methods" case must be implemented as per-node, pinned with a test) | — |
| **Medium** | Behavior change: decorator expressions of nested defs stop counting toward the outer (`@dec(arg if cond else other)` on inner: outer 2/1 → 1/0, inner 3/2). Currently double-counted in both; design's chosen attribution (decorator belongs to the def it decorates) is defensible but must be pinned | deco-factory case verified; design matrix lists "decorator-expr attribution" | outer counts inner's decorator **and** inner counts its own | Pin chosen semantics with an explicit test (part of AC1 subTests) | no double-count |
| **Low** | Recursion depth in `cognitive` (recursive `visit`) on pathological generated code | `checks/complexity.py:93`; verified: indentation nesting is tokenizer-capped (~100 levels → ~200 frames < limit 1000) and paren nesting parser-capped; **RecursionError not reachable via parseable hand-written code**; the design's iterative cyclomatic correctly avoids introducing recursion (ast.walk is already iterative — deque — verified) | recursion depth ≤ ~2× parser nesting caps | None required; optional: catch `RecursionError` per file as defense-in-depth | unchanged (pre-existing, theoretical) |
| **Info** | `_own_children` list-comp allocates per node | prototype | O(children) alloc | generator expression; negligible for a CI gate — per optimization order, no benchmark needed | — |

## 2. Hot-path scale table (five cost classes: CPU/memory only — DB=0, network=0, distributed=0 by construction; single-process local gate)

| Hot path | 1 万行 | 100 万行 | 1 亿行 |
|---|---|---|---|
| `check_file` parse + `_functions()` + per-fn pruned walks | one file ≈ 50k AST nodes; memory few MB (AST, freed per file); CPU < 0.5 s | repo ≈ 5M nodes; **per-file loop keeps memory O(largest single file)**, never the whole repo (`run()` retains no files — verified `checks/complexity.py:130-139`); CPU O(N) ≈ tens of s | a single 100M-line .py is the only break point (whole-file `ast.parse` + `read_text`); across many files the loop holds. Replacement if ever needed: directory sharding + multiprocessing (explicitly out of scope) |
| `cyclomatic` per function | O(own subtree); iterative stack, depth-bounded | before fix: O(F·N) worst on nesting-heavy files (benchmarked nested chain F=90: 2.3 ms vs 1.6 ms fixed, ~1.5×; score inflation 448 vs 270) — after: O(N) total | identical; fix is correctness-driven, perf win is a side effect (µs–ms absolute; no benchmark plan required and none claimed) |
| `_functions()` | O(N) single `ast.walk` over file, unchanged | O(N) | O(N) |
| `cognitive` per function | O(own subtree); recursion depth bounded by parser caps (~200 frames), verified no `RecursionError` at tokenizer-max nesting | same | same |

**×10/×100 conclusion**: the design holds at all realistic scales; the only super-linear term (O(F·N) re-walking) is removed by the fix. Memory stays O(largest single file). No N+1, no array-as-queue, no Top-K sort, no Bloom Filter — none of the decision-table avoid-list patterns apply to this code.

## 3. Decision-table citations for the fix

- **Stack for DFS** (§1 decision table: "DFS / 表达式解析 → Stack（深度大用显式栈防溢出）") — iterative pruned walk for `cyclomatic`; correct call, and the design's explicit "no recursion regression" note matches the verified fact that `ast.walk` is deque-iterative today.
- **Visited set exemption** (§2: "图遍历必须维护 visited 集合，除非结构保证无环") — AST is a tree, structurally acyclic; pruning at the FunctionDef boundary is the correct layer-cut, no visited set needed.
- **`_functions()` unchanged** — verified correct: it is the fail-open guard. A "top-level only" fix would silently stop scoring nested defs (the design's "wrong-layer fail-open" failure mode) — my corpus confirms nested defs are common (9 over-scored functions today, including `pbatch/` core code).
- **Correctness before optimization** (§4 optimization order) — the design claims no perf wins; my benchmark independently shows a modest real one (~1.5×) with zero risk of regression, so the "no perf claims without benchmark plan" rule is satisfied.

## 4. Remaining evidence gaps

- The on-disk design artifact is a 19-line summary; the 9-case matrix and 6-failure-mode details are claimed but not fully on disk. I re-derived and verified the mechanism independently, so this is a documentation gap, not a design risk — implementation must carry the matrix decisions forward (especially decorator attribution and class-in-function).
- Implementation acceptance is covered: AC1 (new subTest method incl. exact 1/0 + 4/6, both metrics, check_tree-level no-fail-open pin), AC2 (verified 14/14 unmodified tests pass with fix), AC3 (monotonicity — verified 9 decreases / 0 increases repo-wide, so no function can newly fail; gate remains exit-0).

VERDICT: PASS - double-count defect is live and material (9 repo functions over-scored, incl. the tool's own metric), the per-node `_own_children` prune + iterative stack design is correct and verified by prototype across 10 case families including the class-in-function trap; monotone non-increasing (0 increases repo-wide), no fail-open (`_functions` unchanged), 14/14 existing tests pass unmodified; complexity improves from O(F·N) to O(N) per file; scale table holds at 1万/100万 lines with the only break point (single 100M-line file) documented as out of scope. Non-blocking: pin decorator-attribution semantics and class-in-function pruning with explicit AC1 subTests; optional RecursionError defense-in-depth.
