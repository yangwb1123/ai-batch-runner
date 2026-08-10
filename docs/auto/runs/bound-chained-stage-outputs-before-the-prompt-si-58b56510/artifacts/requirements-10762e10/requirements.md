Specification complete and verified. All arithmetic and symbol citations check out.

## Deliverable

**`docs/auto/requirements/ai-1f0b642b-requirements.md`** (431 lines) — requirements specification for direction #1 of module `ai`, following the established format of `docs/auto/requirements/agent-engineering-36bd39dc-requirements.md`.

## Evidence verification results

All 7 cited symbols confirmed live, with two numeric refinements:

| Citation | Verdict |
|---|---|
| `chain_variables` (run-review.py:373) | ✅ unbounded paste, no bound anywhere |
| `CHAIN_SOURCES` (run-review.py:358) | ✅ confirmed; **refined**: stage 07 chains 8 entries (7+1), stage 09 chains **9** sources (direction said 7/8 — problem understated, worst case is 18 MiB) |
| `_prompt_size_error` (run-review.py:507) | ✅ hard-rejects > 122880 bytes pre-spawn |
| `_DEFAULT_STAGE_VARS` (run-review.py:85) | ✅ confirmed (fallback schema; sdlc.yaml matches) |
| `pbatch/evidence.py:combine_outputs` | ✅ confirmed; adapted — it takes file paths/emits `<evidence>` fences, so the spec pins reuse of budget + `_fair_allowances` + marker contract while AC2 forces keeping the `--- Stage NN output ---` format |
| `pi-batch.yaml prompt.max_bytes: 122880` | ✅ confirmed (evidence.max_bytes 65536 / max_sources 64) |
| `pbatch/config.py:PROMPT_MAX_BYTES` | ✅ confirmed; live values 122880/65536/2097152 |

## Spec contents

- **Root cause**: unbounded chain fan-out × late binary gate; verified failure threshold ≈ 13 KiB per stage output kills stage 09 today
- **5 requirements + 1 optional doc line**, scoped strictly to this direction (directions #2/#3 explicitly out of scope)
- **3 supplied acceptance checks preserved and pinned**: AC1 (new pytest: 30 KiB × 9 outputs, `--all` renders stage 09, saved prompt ≤ 122880 bytes with `truncated` marker; omission marker via tiny-budget unit leg), AC2 (`test_all_chains_stage_outputs_end_to_end` passes unmodified), AC3 (exit 0 + stages 06/07/09 complete)
- **Key design pins**: budget = `min(EVIDENCE_MAX_BYTES, PROMPT_MAX_BYTES // 2)` = 61440 per stage (equal split across stage 07's two variables — prevents 2×65536 re-triggering the gate), water-fill reuse from `pbatch/evidence.py`, head-preserving truncation with stage-id/byte-count markers, backward-compatible keyword-only kwargs
- **Arithmetic verified**: filled prompt ≈ 64.5 KiB « 122880; current code fails stages 06/07/09 at 30 KiB sources (124.9/247.9/278.4 KiB)
