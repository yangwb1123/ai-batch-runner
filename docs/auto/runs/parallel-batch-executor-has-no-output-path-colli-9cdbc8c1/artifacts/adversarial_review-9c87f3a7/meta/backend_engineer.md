All verification complete. Final state: design doc's embedded code passes the 41-case harness, quality gates green, runner at 998 lines. Here is the review.

---

# Review — `parallel_plan.py` reference implementation + unified wave loop (direction `ba9cd1a4`)

**Verdict: approve with the corrections below applied.** All three qa_lead F2 holes were **reproduced live against the design's embedded code** and are **closed**; the `collides()` audit found **one genuine new hole (C5)** beyond the documented edges; the error contract is sound end to end. Concrete patches are applied to `docs/auto/design/pbatch-ba9cd1a4-design.md` (§0.2 C5/C6, §2.1 module, §2.2 runner, §2.3 import+catch note, §2.4 bullet, §4 F2/F7/F9/F11, §5 Phase 3, §6 acceptance mapping) and verified against a copy of the real `pbatch` package.

## 1. qa_lead F2 — reproduced, then closed

Reproduction of the design's original `_validate_plan` (verbatim) against real `Task` objects:

```
waves=5             -> TypeError: 'int' object is not iterable        <-- not OutputCollisionError
waves='01'          -> ACCEPTED: [[0], [1]]                           <-- silent pass
waves=[["9"*5000]]  -> ValueError: Exceeds the limit (4300) for integer
                       string conversion: value has 5000 digits       <-- crash
```

Two findings beyond qa_lead's write-up:
- **The crash is on the repo's own floor version.** `sys.get_int_max_str_digits()` = 4300 on this box's Python 3.10.7 — the limit was backported to 3.10.7/3.9.14 security releases, so the design's "3.11+ only" framing understates it. The docstring now says so.
- The `waves='01'` silent pass is a *coverage hole in the fail-closed guarantee*, not just a cosmetic accept: a caller typo'ing a string plan gets a full silent run instead of exit 2.

**Patch** (`_validate_plan` rewritten; new `_plan_error` + `_node_index` helpers):
- `isinstance(waves, list)` and `isinstance(wave, list)` guards → `OutputCollisionError` for `5`, `"01"`, tuples, `[["0"], 5]`.
- **Digit-count guard** `len(nid) > len(str(len(tasks)-1))` ⇒ invalid, evaluated *before* `int()` — any id longer than the largest valid id is invalid by construction, so the 4300-digit `ValueError` becomes unreachable on every Python version; `int()` is wrapped in `try/except (TypeError, ValueError)` as defense-in-depth.
- `_node_index` centralizes the canonical-id checks (`str(i)`, `str(int(nid)) == nid` rejects `"01"`/unicode digits like `"١"`).
- Result: every invalid shape → `OutputCollisionError` → exit 2, never a traceback. 8 new tests pin this (`test_f2_*`), including `"9"*5000`/`"9"*50000` through **`run_parallel` itself**, asserting the counter agent is never invoked.

## 2. `collides()` predicate audit

| Edge | Audit result | Action |
|---|---|---|
| **Symlink aliasing (F7)** | Correct: `output_path()`/`workdir()` both `.resolve()`; `real/o.md` vs `link→real/o.md` → identical; symlinked cwd spellings resolve to one dir → C1 exemption correctly applies (no false positive). Was **unpinned** | 2 tests added (`test_collides_symlink_alias_outputs`, `test_collides_symlinked_cwd_equalization`) |
| **Case-insensitive FS (F9)** | Partial fix landed: comparisons go through `os.path.normcase` — **Windows case-variant paths now flagged**; Linux CI is identity (byte-identical). Honest limitation kept: `posixpath.normcase` is a no-op on darwin, so macOS APFS case-insensitive volumes stay undetected (documented; simulated-Windows test monkeypatches `_normcase`) | `_normcase` helper + docstring; 2 tests |
| **output == other cwd** | Flagged via `is_relative_to`'s inclusive equality — but the reason string was misleading ("nested") when the other task's output also sits under that path. Restructure now reports the true containment reason when the output *is* the other's cwd | Test `test_collides_output_equals_other_cwd` |
| **Same-cwd exemption residual** | **New hole (C5)**: the original early return `if pa is None or pb is None: return None` also skipped the *containment* check — so a task whose (distinct, nested) cwd contains the other task's deliverable was unflagged whenever either task was stdout-only. That is exactly the C1 "genuinely new exposure" (outer agent's side effects land on the inner deliverable), and C1's rationale never depended on both tasks writing outputs. Fixed: stdout-only pairs are exempt from everything; a single stdout-only task's **cwd still participates in containment**. Bonus: this heals qa_lead's F1 under-specified T1b sub-case on its own | `collides` restructured (12 lines); 2 tests (both directions); F11 row; §6 R1.2b updated |

No false-positive risk to existing shapes: campaign/`from_dir`/default tasks.yaml are all same-cwd (containment only runs between distinct resolved cwds), and the C5 flag only fires for explicitly nested distinct cwds — the design's own hazard class.

## 3. Error contract end-to-end (verified in code, then in harness)

| Layer | `OutputCollisionError` (scheduling) | `SystemExit(3)` (execution, pre-existing) |
|---|---|---|
| `run_parallel` | raised by `resolve_plan` **before the pool exists** | raised after a pool ran (`if capped: raise` inside wave loop — no next wave; workers' cap-3→`None` marker path untouched) |
| `cli_batch._execute_batch` | caught → `log.error` → `sys.exit(2)` | **not caught** → exit 3, unchanged |
| round loop (`cli_rounds` `_execute_round`) | no `except` anywhere (`:66–102` re-verified) — 7×24 loop cannot spin on a collision | same |
| pipeline/campaign | propagates → traceback + exit 1 (spec R1.5, loud) | propagates (pre-existing) |
| overlap | none: OCE only pre-pool, `SystemExit(3)` only post-pool; `_run_parallel_one` catches only worker-level cap-3 | — |

Contract holds. The one nuance worth recording (now in §2.3): the catch is deliberately scoped to `OutputCollisionError` only — exit-2 (definition error) vs exit-3 (budget) remain distinguishable by exit code, which is what the CLI companion test asserts (returncode 2 + stderr naming both tasks).

## 4. Hardening beyond qa_lead's ask (all verified)

- **runner line budget**: fold `if capped: raise SystemExit(3)` → **998 lines** (2 headroom, was 999/1); design accounting updated; `_collect_parallel` now gets `len(wave)` so wave-mode PROGRESS logs are monotonic (plain path identical); `min_interval` documented as per-wave.
- **cli_batch**: import line specified (`sys` already at :20); `# Pass task_index` comment removal is safe (the `i + 1` in the new loop preserves 1-based display indices — the stub's recorded indices prove it).
- **`run_parallel.__doc__`** now exists (was `None` — misplaced docstring), satisfying R4.2's substance.

## 5. Verification evidence

- **41-case harness** (`/tmp/pbatch-review/test_review.py`, run against a copy of the real `pbatch` with the corrected module/runner/cli_batch overlaid): **41/41 pass**, including real `run_parallel` + real `_collect_parallel`/`_run_parallel_one` with a stubbed agent — plain path, collision-no-agent, bad shapes through the runner, wave barrier (wall-clock overlap + t2 after t0/t1 end + final write wins), `plan_waves_for` end-to-end, budget cap plain (exit 3, 1 agent) and **wave mode (second wave never starts)**, min_interval, empty tasks, cli_batch exit-2 companion.
- **Design-doc code is the verified code**: the module extracted verbatim from the patched design replaces the harness copy → 41/41 still pass; `quality.py --strict` OK; fences balanced.
- **Regression net**: repo's `test_pi_batch.py -k parallel` → 6/6 in-process tests pass, 88/88 waves-adjacent tests pass against the patched copy. The 3 CLI failures are the pre-existing E1 environmental flake (live campaign pid 1547473 holds the run lock → exit 5; the subprocess exits before reaching the patched code) — qa_lead's `--no-lock` recommendation for the new CLI test stands.

## 6. Open items (documented, non-blocking)

- Harness lives at `/tmp/pbatch-review` — ephemeral; design §5 Phase 3 names it as the starting point for the repo test file (tests cannot land pre-implementation: `waves=` doesn't exist on the current runner).
- macOS case-insensitive volumes (F9), `--dry-run` collision check (F6, 3-line follow-up), pipeline clean-stage-failure (design §7) — unchanged, still out of scope.
- `waves.py`/`fabric_sched.py` remain byte-identical; `OutputCollisionError` stays the single fail-closed type (architect #3 accepted as designed).

**Files changed**: `docs/auto/design/pbatch-ba9cd1a4-design.md` (C5/C6 + corrected reference implementation + acceptance mapping). No production code touched — implementation remains Phase 2/3 of the migration plan, now landing 998-line runner, 505-line cli_batch, 267-line leaf module, all gates pre-verified.
