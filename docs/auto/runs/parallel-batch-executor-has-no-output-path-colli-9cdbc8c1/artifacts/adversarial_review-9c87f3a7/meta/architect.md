All claims verified. I ran the checks myself: line counts, `run_parallel`/`_collect_parallel` bodies, `waves.py` internals and importers, `cli_batch` round loop and catch placement, `models.output_path`, `quality.py` gate, `pyproject` floor, exit-2 pattern, `mkstemp+rename` write paths, campaign/from_dir cwd shapes, `TaskNode` construction sites. Here is the architecture review.

---

# Architecture Review — pbatch output-collision detection + wave-scheduling wiring (direction `ba9cd1a4`)

## 1. Scope, assumptions, verified architecture summary

**Scope**: fail-closed output-collision detection for `run_parallel` (R1), optional wave co-scheduling via `waves=None` param (R2), `waves.py` first production importer (R3), docs (R4). No CLI/YAML wiring, serial mode untouched, `waves.py`/`fabric_sched.py` byte-identical.

**Checks I actually ran**: `wc -l` on runner (996) / cli_batch (500) / waves (248); read `run_parallel` (:935–962), `_run_parallel_one` (:886), `_collect_parallel` (:909–932); `waves.py` `_conflicts` :49 / `plan_waves` :56 / full-conflict serialization :72–73; `grep waves` across `pbatch/` (zero production importers, exactly the 5 named test files); `cli_batch._run_batch_round` :277 → `_execute_batch` :306 → `run_parallel` :496; `cli_rounds.py` round loop (no `except`/`SystemExit` swallow — exit 2 propagates); `meta.py` dedupe :392; `models.output_path` :145–156; both deliverable write paths use `mkstemp`+`rename` (runner :356–360, :580–588); `module_slug` includes an 8-hex digest (slug collision impossible); `quality.py` `MAX_FILE_LINES=1000`, `_LEGACY_OVERRUN_FILES={"pi-batch.py"}`; `pyproject.toml:9` `>=3.9`; exit-2 pattern in assessor/eval/rule_cli/selfcheck; `Makefile ci` includes `quality.py .`; 3 production `run_parallel` callers; `--reuse` filter at cli_batch :291–295 precedes `_execute_batch`. **Every claim in the design's §0 evidence table reproduced exactly; the four corrections (C1–C4) are accurate.**

**Verified architecture summary** (mapped across the repo's layers):

| Layer | Responsibility in this change | State |
|---|---|---|
| Interfaces/API | `run_parallel(tasks, ..., waves=None)` — additive; 3 callers untouched (cli_batch:496, pipeline:559, campaign_analysis:149); `print_summary` stays public (`pbatch/__init__.py:11,32`) | Verified |
| Composition | New leaf module `parallel_plan.py` (imports only `models`/`fabric_sched`/`waves` — no cycle, F2); runner loop unified into one wave loop (plain = single wave), not a branch | Verified, sound |
| Infrastructure | `cli_batch._execute_batch` catch → stderr report → exit 2, placed below the 7×24 round loop (round loop does not swallow `SystemExit` — verified cli_rounds.py has no except) | Verified, correct placement |
| Domains | Collision predicate (`collides`) — the only new domain logic; O(n²), documented; `TaskNode` adapter (`tasks_to_nodes`) | Verified |
| Protocols | None touched — no config, YAML, flags, session semantics | Verified |
| Platform/shared | `waves.py`/`fabric_sched.py` dormant scheduler gets its first production importer via adapter without modification | Verified — clean ownership boundary |

**Key architectural property**: the design converts a *dormant capability* (wave scheduler, test-only usage) into production-usable via an adapter (`plan_waves_for`) that re-validates the scheduler's output under the stricter R1.2 predicate (C2). It does not re-architect the scheduler. That is the right-sized move — and C2 is the load-bearing insight: `plan_waves`'s write-set intersection cannot express path containment, so raw scheduler output would fail the runner's own validation.

**Assumptions adopted**: agents' shared-directory side effects are the accepted baseline (runner writes deliverables atomically at distinct resolved paths — verified `mkstemp`+`rename`); same-cwd batches are the runner's standard shape (campaign analysis `cwd=str(root)`, `from_dir` `cwd=fpath.parent`, default-cwd tasks.yaml — all verified); the guard is a heuristic, not a sandbox.

## 2. Findings

| # | Severity | Finding | Evidence | Impact | Recommendation |
|---|---|---|---|---|---|
| 1 | **Medium** | Wave-mode semantic asymmetry: identical outputs **fail closed** in plain path but are **deterministically serialized** in wave path (last wave's write wins) | Design §2.1 `resolve_plan` contract; §6 T3 asserts `file-x == b's write` | Coherent and documented today (cross-wave conflicts are "the point of a plan"), but the design's own follow-up (`--parallel-waves` auto-plan) would silently convert today's hard exit-2 errors into serialized runs — a user-visible error-contract change | Keep waves API-only until a follow-up explicitly decides fail-fast vs auto-serialize; add one sentence to the AGENTS.md bullet: "wave plans may contain cross-wave conflicts by design" |
| 2 | **Low** | Progress counter resets per wave: `_collect_parallel` logs `PROGRESS: %d/%d` with per-wave results and `total=len(tasks)` | runner.py:928; design §2.2 calls it per wave | Cosmetic misreport in wave mode (API-only today); plain path unaffected | Accept, or pass cumulative count in the follow-up — not blocking |
| 3 | **Low** | `OutputCollisionError` is overloaded for *non-collision* plan-validation failures (`a=-1, b=-1`) | Design §2.1 `_validate_plan` | Semantic smell; all catch sites treat it uniformly as fail-closed/exit-2, so no functional defect | Optional: `InvalidPlanError(OutputCollisionError)` subclass; single fail-closed type is defensible — defer |
| 4 | **Low** | No dedicated test pinning "budget cap mid-wave ⇒ `SystemExit(3)`, no next wave" | Design §6 maps R2.4 to existing plain-path T8 tests; F3 relies on shared loop | Property is real (verified F3: empty `fut_map` + `capped=True` → exit 3 before next wave) but unpinned in wave mode | Add one 6-line case to T2 (wave plan, cap on first wave, assert second wave never starts) |
| 5 | **Info** | Same-cwd exemption residual risk: shared-cwd agents' side-effect writes can still clobber each other's deliverables; cross-cwd arbitrary writes (outside the cwd-containment pattern) are undetectable | Design C1 rationale; `collides` predicate | Accepted baseline; guard is a heuristic, not a sandbox — deterministic prevention would need per-task cwd isolation (out of scope) | State explicitly in the AGENTS.md bullet that the check is a scheduling-time heuristic, not a write-safety sandbox |
| 6 | **Info** | Stale doc: `docs/ARCHITECTURE.md` §3 says file ≤500 lines (pbatch/) but the enforced gate is 1000 (`quality.py:24`); runner.py (996) and cli_batch.py (500) already exceed 500 | `quality.py:24`; `docs/ARCHITECTURE.md` §3 | Doc drift only, unrelated to this change | One-line doc fix in the same docs pass (Phase 4) |
| 7 | **Info** | The review prompt's `docs/architecture/DIRECTORY_MAP.md` does not exist in this standalone repo; the canonical architecture doc is `docs/ARCHITECTURE.md` (per AGENTS.md component table) | `ls docs/architecture/` → ENOENT | Navigation friction for reviewers | Update the stale path reference |
| 8 | **Info** | Line-budget headroom is exactly 1 line (999/1000) on runner.py; any future unrelated edit to runner.py trips `make ci` | Design §2.2 accounting; quality.py:24 | Fragile but within gate; design already offers the fold (`if capped: raise SystemExit(3)` → 998) | Adopt the fold proactively during Phase 2 to bank 2 lines of headroom |

**Design corrections C1–C4 — endorsed**: C1 is the most important. The literal R1.2b rule flags every same-cwd pair — which is how the runner builds its own primary batches (campaign analysis, `from_dir`, default-cwd tasks.yaml) and contradicts the spec's own T3. The correction (containment checked only between distinct cwds) is grounded in the verified atomic-write property and keeps the genuinely new exposure (nested distinct cwds) covered. C3's line arithmetic is exact as verified. C4's fail-closed empty-plan choice is consistent with the house doctrine; C2 is necessary for correctness. All four are deviations from the requirements spec text and should be recorded as owner-acknowledged decisions, not silently absorbed (the design already flags them — good).

## 3. Decision options

| Option | Description | Trade-offs | Verdict |
|---|---|---|---|
| **A — Adopt as designed** | New leaf module + unified wave loop + cli_batch catch; waves API-only | Smallest surface; meets all R1–R4; waves stays dormant from CLI; 1-line budget headroom | Preferred base |
| **B — A + zero-cost hardening** (recommended) | Fold the `SystemExit` line (998, +2 headroom); add the wave-mode budget-cap test (#4); tighten the AGENTS.md bullet (#1, #5); fix docs #6/#7 in the same docs phase | Adds ~15 minutes of work, removes the two real gaps (test pinning, error-contract wording) without growing the diff | **Preferred** |
| C — Collision detection only, no wave wiring | Skip R2/R3 | Fails the direction's own title ("wave/conflict scheduler … never wired in") and leaves the scheduler dormant; rejected | Not viable |
| D — Build-vs-buy (external scheduler lib) | Reuse a generic wave/topological scheduler | Predicate is domain-specific (path containment + cwd semantics + fail-closed contract); a 150-line leaf module is right-sized; new dependency + license cost buys nothing | Rejected — build is correct |

**Preferred: Option B.** The design is architecturally sound; B just closes the two soft spots (unpinned wave-cap property, and the documented-but-undersold semantic asymmetry) and banks line headroom, at near-zero marginal cost.

## 4. Prioritized implementation sequence

| Phase | Work | Compatibility | Executable acceptance checks |
|---|---|---|---|
| **P1** | Add `pbatch/parallel_plan.py` (pure addition, ~150 lines) | No behavior change anywhere | `python -c "import pbatch.parallel_plan"`; `python quality.py .` unchanged; `make ci` green; `git diff --stat` shows only the new file |
| **P2** | Rework `run_parallel` (unified wave loop, 998–999 lines) + `cli_batch` catch (exit 2) + fold SystemExit line | Additive `waves=None`; 3 callers untouched; same-cwd batches unflagged; serial untouched | All existing parallel tests green (F6 verified); manual: colliding `tasks.yaml --mode parallel` → returncode 2, stderr names both tasks+paths, counter agent never invoked; `python quality.py .` green |
| **P3** | Tests: T1 (main + T1b/T1c/T1d + same-cwd regression + CLI subprocess companion), T2 negatives **+ wave-mode budget-cap case (#4)**, T3 in `test_runtime_governance.py` | Test-only additions | `make ci` (387 + 25 eval + harness); `python quality.py --strict .`; `git diff --stat`: only `parallel_plan.py`, `runner.py` (+2/-0), `cli_batch.py` (+5), 2 test files, `AGENTS.md` |
| **P4** | Docs: AGENTS.md §2.2 bullet (include #1 cross-wave-contract sentence and #5 heuristic-not-sandbox sentence); fix #6/#7 stale doc references | Docs-only | Review diff; `make ci` still green; decision-log entry recording C1–C4 owner sign-off |

**Risks**: (1) line gate — mitigated by the fold; (2) "waves.py byte-identical" is a review-visible constraint — resist fixing `_conflicts` while wiring; (3) exit-2 collides with nothing (round loop verified not to swallow `SystemExit`); (4) O(n²) — bounded by batch size, documented; (5) blast radius: behavior changes only for batches that today *silently corrupt deliverables* — strictly an improvement; (6) rollback: revert P2–P4 only; the leaf module is inert without its runner call site — no config, schema, or persisted state.

## 5. Unknowns needing owner/product decisions

1. **CLI wiring semantics** (product): should a future `--parallel-waves` flag auto-plan (convert same-output tasks to serialized waves, changing today's exit-2 contract) or keep fail-fast and only offer waves programmatically? The design's own follow-up points here; decide before any CLI exposure.
2. **Pipeline collision handling** (owner): spec chose "let propagate" (traceback + exit 1); a clean stage failure with a decision-log entry is a strictly nicer failure mode for 7×24 operation. Needed only for correctness of diagnosis, not of execution.
3. **Case-insensitive filesystems** (owner): macOS case-variant paths (F9) are undetected; repo CI is Linux. Whether to normalize case in `collides` is a support-scope decision.
4. **Shared-cwd side-effect baseline** (strategic): the C1 exemption rests on "agents' side effects are the accepted baseline." If multi-agent same-cwd batches become the norm (they already are for campaigns), per-task cwd isolation or deliverable-locking is the eventual hardening — deliberately out of scope today.

**Verdict**: approved for implementation as Option B. The design is evidence-verified end to end, fail-closed in every path, honest about its four spec deviations (all endorsed with verified grounding), and right-sized: one leaf module, one unified loop, one catch site, zero dormant-layer churn.
