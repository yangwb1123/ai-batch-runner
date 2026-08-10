All verification complete. Here is the algorithm reviewer's report.

---

# Algorithm Reviewer — Bound chained stage outputs before the prompt-size gate (direction `1f0b642b`)

## Scope and method

Reviewed the design at `docs/auto/designs/ai-1f0b642b-design.md` against the live implementation (`ai/run-review.py`, `pbatch/evidence.py`, `pbatch/config.py`, `tests/test_run_review.py`) per `backend-specs/algorithms-data-structures.md` and `backend-specs/complexity-and-scale.md`. I re-ran the real `_stage_prompt` path, binary-searched the failure thresholds, and executed a faithful prototype of the proposed algorithm (per §2.2/§2.3 semantics: budget split across **variables**, water-fill within a variable).

## Verification results (what I actually measured)

| Claim | Verdict | My measurement |
|---|---|---|
| Current code fails stages 06/07/09 at 30 KiB sources | ✅ Verified | 122.1 / 242.3 / 272.0 KiB prompts — **byte-exact match** to the design's refinement; all > 122880 |
| Failure thresholds ≈ 13.12 / 14.72 / 29.51 KiB | ✅ Verified | 13429 / 15069 / 30193 B (Δ of 4/7/22 B — material claim exact; minor citation drift) |
| Stage 09 water-fill `[6827×6, 6826×3]`, sum 61440 | ✅ Verified | `_fair_allowances([30720]*9, 61440)` = `[6827×6, 6826×3]` — exact |
| Stage 09 chained ≈ 62.5 KiB, 9 markers | ✅ Verified | 62,572 B, 9 prefixes, 9 markers (Δ36 B from design's 62,536 — path-string length; immaterial) |
| Stage 07 = 31,572 + 30,744 B | ✅ Verified | 31,544 + 30,744 = 62,288 B (Δ28 B; immaterial) |
| Budget=3, 9 sources → `count="6"`, 3×1 B shown | ✅ Verified | `[1,1,1,0,0,0,0,0,0]`, `<chained-sources-omitted count="6" />` — exact (matches T3) |
| Bounded full prompt « 122880 | ✅ Verified | Stage 09 full prompt = **64,380 B** (58.5 KiB headroom, not 54-56 as claimed — headroom is *larger* than claimed) |
| In-budget byte-identity (AC2) | ✅ Verified | Prototype output `==` current `chain_variables` output for small sources |
| Multibyte safety | ✅ Verified | No `\ufffd`; shown bytes ≤ allowance; `read_text_bounded` (text_io.py:31-35) strict-decodes, so `errors="replace"` in `_truncate_text` is defensive dead code — no lone surrogates can arrive |
| Per-call `_chain_budget()` (D1 fix) | ✅ Verified correct | Import-time constant would freeze at 61440 and break T1/T3's `monkeypatch.setattr(mod.batch_config, ...)` idiom (exists at tests:184-190); per-call read honors it |
| Single call site, 3 positional unit tests | ✅ Verified | Only `_stage_prompt` line 816; calls at tests:62/71/79 (design cites 57/69/80 — function-def vs call-line drift, immaterial) |
| `//2` budget is necessary under equal split | ✅ Verified | 2 vars × 61440 + overhead > 122880 would re-trigger the gate; 2 × 30720 = 61440 holds |

**Not applicable, checked:** Bloom Filter (rule 5 — no approximate membership anywhere; no misuse), BFS/DFS/recursion (rule 2/7 — chain is flat iteration, no recursion), N+1 (rule 6 — the chain path is pure in-memory: zero DB, zero remote calls, zero awaits), unbounded queues/caches/concurrency (none; budget is a structural hard cap, not an empirical one).

## Findings

| # | Severity | Defect pattern | Evidence (file/line) | Current complexity | Proposed alternative (decision-table-cited) | Complexity after fix |
|---|---|---|---|---|---|---|
| M1 | **Medium** | **Algorithm choice — recency inversion in truncation**: head-preserving truncation on a monotonically-refining chain keeps the *oldest* content and drops the *newest, most decision-relevant* content first | `CHAIN_SOURCES` ascending (run-review.py:358-372); `_fair_allowances` allocates in ascending index order (evidence.py:69-87); **measured**: at 30 KiB sources stage 09 retains only 6.8 KiB head of each stage (`[6827×6, 6826×3]`), stage 07 retains 4.4 KiB head each — and templates 05-08 put the verdicts at the *tail* (Go/No-Go decision, Over/Under-committed, `Met/Partially Met/Not Met`, release blockers — verified in `ai/prompts/05-*`…`08-*` last lines). The CTO prompt (stage 09) thus loses stage 07's critical findings, stage 08's committed stories and release blockers — exactly the inputs that justify stages 07-09's existence. The design's failure-mode row rationalizes this as "same as evidence.py", but that analogy fails: evidence.py sources are independent, symmetric artifacts; chained stage outputs are order-dependent refinements (each stage subsumes all prior) | O(total) head slice, deterministic | Reverse the **allocation/truncation direction** while keeping join order 00→08 (recency-weighted water-fill: newest stage gets first claim on budget; oldest truncated first). k ≤ 9 → no heap needed: fixed reverse order is O(k) (decision table: "顺序遍历/量小 → List" for join order + "Top K/优先级 → Heap O(n log k)" row — for k ≤ 9 a constant-order pass beats a heap). In-budget runs stay byte-identical (no truncation ⇒ AC2 untouched); only the remainder-byte and truncation *direction* change | O(k) reverse pass, same determinism (reverse of a fixed order is still fixed) |
| L1 | Low | **Unbounded-resource residue**: marker `full output: {path}` bytes sit outside the budget arithmetic; the "66-68 KiB" figure is empirical (short paths), not structural | Design §2.3 marker format; worst case 9 × (130 B + PATH_MAX 4096 B) ≈ 37 KiB markers + 61.4 KiB chained + ~2 KiB template ≈ **100 KiB < 122880 — still safe** (verified arithmetically), but the margin is a measured constant, not an invariant | O(1) | Cap the path in markers (ellipsize to ~200 B) or count marker bytes against the budget; keep `_prompt_size_error` as final arbiter (already fail-closed) | O(1) |
| L2 | Low | **Redundant computation**: source text is UTF-8-encoded 2-3× per stage (size pass, `_truncate_text` pass, marker `total` pass) | Design §2.2.3/§2.3; worst case 9 × 2 MiB = 18 MiB → ~50 MiB encode ≈ sub-second (no benchmark claim — see plan below) | O(total)×2-3 constants | Encode once per source, slice the bytes buffer for truncation, reuse for sizes/markers | O(total)×1 |
| I1 | Info | **Spec ambiguity**: §2.3 step 3 defines `nvars` only by example ("stage 07 has 2") | Design §2.3 | — | Pin in implementation: `nvars` = variables that actually enter chaining after the explicit-value check (else a skipped variable wastes a share) | — |
| I2 | Info | **Equal split under-weights the decision variable**: stage 07 gives `CRITICAL_HIGH_FINDINGS` (7 sources) the same 30720 as `ARCHITECTURE_OUTPUT` (1 source) | Design §2.3; measured 31,544 vs 30,744 B | — | Source-count-proportional split (7:1 → 53760:7680) is a defensible policy alternative; the `//2` budget is *required* only for the equal split — verified | — |
| I3 | Info | `_fair_allowances` is O(k²) worst case | evidence.py:69-87 | O(k²), k ≤ 9 | None needed — bounded constant per the "量小 → Array/List" row; `sorted(pending)` gives deterministic prompts (good for fingerprint/reuse) | — |

## Scale table (per hot path)

**Path 1 — `chain_variables` bounded chain (per stage):** n = source count (≤ 9) × source bytes (≤ `OUTPUT_MAX_BYTES` = 2 MiB each, config.py:272).

| n | Memory | DB I/O | Network round trips | Holds? |
|---|---|---|---|---|
| 1万 (9 × ~10 KiB) | ~90 KiB `prior_outputs` + transient 2×10 KiB | 0 | 0 | ✅ trivially |
| 100万 (9 × 2 MiB, at the 2 MiB cap) | ~18 MiB `prior_outputs` + transient ≤ 4 MiB | 0 | 0 | ✅ — chained prompt hard-capped at 61,440 B regardless of source size |
| 1亿 (9 × 200 MiB hypothetical) | **Unreachable by construction** — `read_text_bounded` (text_io.py:21-30) hard-rejects any input > 2 MiB before chaining | 0 | 0 | ✅ the *boundary* is the existing read-time rejection, not the new code |

**Path 2 — `fill_template` + gate check:** O(vars × template) replaces; vars ≤ ~10, templates measured 1.8-1.9 KiB → ~20 KiB of work; dominated by the O(total) chained-string copy. Same table as above; no DB/network anywhere in either path. ×10/×100 conclusion: the design holds at every reachable scale because every input is hard-capped (`OUTPUT_MAX_BYTES`) and every sum is hard-capped (`min(EVIDENCE_MAX_BYTES, PROMPT_MAX_BYTES//2)` = 61,440). **Replacement plan if the caps were ever raised drastically:** switch to evidence.py's disk-side head-read pattern (`_fenced`, evidence.py:89-104 reads only `allowance` bytes) — the design already threads `paths` into `chain_variables`, so this is a read-source swap making cost O(budget) instead of O(size); not needed at current scales, and the design's markers already make full artifacts recoverable on demand.

## Required fix citations (decision table)

- **M1**: `algorithms-data-structures.md` §1 (List for in-order traversal; Top K/priority selection — heap row is the *principle*, fixed reverse order is the k≤9 specialization) + §9 scale reasoning (content value ordering must be explicit when budgeted); `complexity-and-scale.md` §4 optimization order (correctness first — reverse-order allocation changes no bound or AC, only selection policy).
- **L1/L2**: `complexity-and-scale.md` §1 five cost classes (memory/CPU constants made explicit) and §8 (no "optimized" claims without measurement).

## Benchmark plan (for L2 and the scale claims)

1. **Unit micro-bench** (in T1's file, deterministic): time `chain_variables` with 9 × 2 MiB synthetic sources, assert < 1 s on CI hardware — pins the O(total) encode ceiling.
2. **e2e AC1 already measures** the prompt byte count; extend it to assert the stage-09 prompt stays ≤ 61,440 + 8 KiB overhead (structural, not empirical).
3. If M1's reverse-order fix is adopted: one T1 assertion that under truncation the *newest* source's retained bytes exceed the oldest's — deterministic, no timing.

## Verdict

The core algorithm is correct: budget is a structural invariant (`chained ≤ PROMPT_MAX_BYTES//2`), water-fill semantics match the existing bounded-injector contract, byte-identity for in-budget runs and the D1 per-call-budget correction are both verified live, and the omission path is fail-closed in the right direction (toward omission, never toward the gate). No O(n²), N+1, unbounded-resource, or scale-blindness defects exist in the design. One Medium (M1, truncation direction inverts the decision chain's recency ordering — cheap fix, does not violate any AC) and two Low findings should be resolved or explicitly declined before implementation.

VERDICT: PASS - bounded chain algorithm verified live (62.6/62.3 KiB chained « 122880 gate; thresholds 13.11/14.72/29.49 KiB confirmed; water-fill/omission/byte-identity/multibyte all exact); no complexity, N+1, or scale defects; M1 recency-inversion (head-preserving truncation drops newest decision content first in stages 07/09) and L1/L2 should be addressed with the cited one-line fixes - markers already preserve full artifacts on disk.
