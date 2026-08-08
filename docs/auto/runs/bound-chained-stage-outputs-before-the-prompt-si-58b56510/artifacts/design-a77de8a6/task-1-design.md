Design complete. **`docs/auto/designs/ai-1f0b642b-design.md`**

## Verification results (evidence treated as untrusted)

All 7 cited symbols confirmed live at claimed locations (`chain_variables` :373, `CHAIN_SOURCES` :358, `_prompt_size_error` :507, `_DEFAULT_STAGE_VARS` :85, `combine_outputs`/`_fair_allowances` evidence.py:12/69, pi-batch.yaml:87–94, config.py:283/289). Three refinements found:

1. **Byte figures overstated**: spec's "124.9/247.9/278.4 KiB" vs measured **122.1/242.3/272.0 KiB** through the real `_stage_prompt` (~715 B/entry double-counted). Material claim holds — all three decision stages fail at 30 KiB outputs.
2. **Thresholds verified by binary search**: stage 09 dies at ≥ 13.12 KiB/source (spec ~13.3), stage 07 at 14.72, stage 06 at 29.51.
3. **Real flaw in the spec**: D1's "import-time `CHAIN_BUDGET` constant" contradicts its own tests T1/T3, which monkeypatch `batch_config` post-import. Corrected to a per-call `_chain_budget()` helper.

## Design summary

- **API**: `chain_variables(prior_outputs, stage, variables, schema_defaults, *, budget=None, paths=None)` — keyword-only additions, positional shape intact (3 existing unit tests stay byte-compatible). New helpers `_chain_budget()` (`min(EVIDENCE_MAX_BYTES, PROMPT_MAX_BYTES//2)` = 61440) and `_truncate_text()` (byte-roundtrip, `errors="ignore"` for multibyte safety). `_stage_prompt` passes `paths` so markers name the full file on disk.
- **Algorithm**: per-stage budget → equal split across chained variables (stage 07: 30720 each — prevents 2×65536 re-triggering the gate) → `_fair_allowances` water-fill within a variable → head-preserving truncation + `[stage NN output truncated: showing X of Y bytes; full output: …]` markers, one `<chained-sources-omitted count="N" />` when allowance = 0.
- **Compatibility**: in-budget runs byte-identical (AC2 pinned); explicit values still win; missing sources skipped; no YAML/CLI/pbatch changes; `_prompt_size_error` untouched as final safety net.
- **Failure modes**: budget < source count → omission (fail-closed, never unbounded paste); `PROMPT_MAX_BYTES=1` → budget 0, all omitted; operator raising `evidence.max_bytes` above gate → capped at `prompt//2`; multibyte boundary → clean decode; truncated tails → markers point at full artifacts on disk.
- **Migration**: single-commit change to `ai/run-review.py` + 3 new tests; revert = revert commit; no data/config migration.
- **Acceptance mapping**: AC1 → `test_all_bounds_chained_outputs_before_prompt_gate` (e2e, ≤122880 B prompt, `"truncated"` present, exit 0, stages 06/07/09 exist); AC1b → omission unit test; AC2 → existing test unmodified; AC3 → same e2e second assertion group; each requirement R1–R6 mapped to concrete code + assertions.

The algorithm was **prototyped and executed live**: stage 09 chains 62.5 KiB with 9 markers, stage 07 totals 62.3 KiB, budget=3 yields exactly `count="6"`, small-output runs byte-identical, no mojibake. Feasibility confirmed end-to-end.
