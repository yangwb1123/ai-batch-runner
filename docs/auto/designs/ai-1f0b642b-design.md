# Design — Bound chained stage outputs before the prompt-size gate

| Field | Value |
|---|---|
| Module | `ai` |
| Direction id | `1f0b642b` |
| Source | `docs/auto/requirements/ai-1f0b642b-requirements.md` (evidence-verified) |
| Status | Design (independently verified) |
| Change scope | `ai/run-review.py` + `tests/test_run_review.py` only |

---

## 1. Verification of the evidence (untrusted claims → checked)

Every citation was re-verified against the live repository, including a
byte-exact simulation through the real `_stage_prompt` path.

| Evidence claim | Verdict | Notes |
|---|---|---|
| `chain_variables` at run-review.py:373, unbounded paste | ✅ **Confirmed** | Lines 373–391; `parts.append(f"--- Stage {src} output ---\n{text}")` pastes full text; no sum bound. |
| `CHAIN_SOURCES` at run-review.py:358; stage 07 = 8 entries, stage 09 = 9 sources | ✅ **Confirmed with refinement** | Stage 07 = 7 (`CRITICAL_HIGH_FINDINGS` 00–06) + 1 (`ARCHITECTURE_OUTPUT` 01) = 8 chained entries; stage 09 = 9 sources. The direction's 7/8 were undercounts; worst case is 9 × 2 MiB = 18 MiB. |
| `_prompt_size_error` at run-review.py:507, hard-rejects > 122880 pre-spawn | ✅ **Confirmed** | Lines 507–512; invoked at line 549 inside `run_stage` (line 541) before spawn; stage marked failed, no output file. |
| `_DEFAULT_STAGE_VARS` at run-review.py:85 (fallback; sdlc.yaml matches) | ✅ **Confirmed** | `_load_stage_schema` at line 180 replaces fallback; `ai/sdlc.yaml` stage-06 `PRIOR_FINDINGS: "(paste Critical/High findings from Stages 02-05, or 'N/A')"` matches exactly. |
| `pbatch/evidence.py:combine_outputs` (paths in → `<evidence>` fences out; `_fair_allowances` water-fill at line 69; marker contracts) | ✅ **Confirmed** | Lines 12–29, 64–67, 69–87, 89–104. String-based chain with `--- Stage NN output ---` format cannot reuse it directly — reuse budget semantics + `_fair_allowances` + marker contract only. |
| `pi-batch.yaml prompt.max_bytes: 122880`, `evidence.max_bytes: 65536`, `max_sources: 64` | ✅ **Confirmed** | pi-batch.yaml lines 87–94. |
| `pbatch/config.py` live values 122880 / 65536 / 2097152 | ✅ **Confirmed** | `PROMPT_MAX_BYTES` line 289 (min 0), `EVIDENCE_MAX_BYTES` line 283 (min 1), `EVIDENCE_MAX_SOURCES` line 285, `OUTPUT_MAX_BYTES` line 272. |
| Current code fails stages 06/07/09 at 30 KiB sources: "124.9/247.9/278.4 KiB" | ⚠️ **Confirmed with numeric refinement** | Live simulation through real `_stage_prompt`: **122.1 / 242.3 / 272.0 KiB** — all three > 122880, gate fires. Spec's figures are overstated by ~2.8–6.4 KiB (≈715 B per chained entry, likely per-entry overhead counted twice), but the material claim (all three decision stages fail at 30 KiB outputs) holds exactly. |
| Failure thresholds ≈ 13.3 / 15 / 30 KiB per source | ✅ **Confirmed (measured: 13.12 / 14.72 / 29.51 KiB)** | Binary search over S through real `_stage_prompt`: stage 09 dies at S ≥ 13433 B, stage 07 at S ≥ 15076 B, stage 06 at S ≥ 30215 B. Spec's "≈13 KiB kills stage 09" is right. |
| Budget math: filled prompt ≈ 66–68 KiB « 122880 | ✅ **Confirmed** | 61440 (chained) + 9×24 B prefixes + 9×~130 B markers + template 1832–2057 B fixed ≈ **66–68 KiB**. Water-fill on 9×30 KiB under 61440 → [6827×6, 6826×3], sum 61440. |
| Existing e2e test at tests/test_run_review.py:223 passes only because fake agent emits ~1-byte outputs | ✅ **Confirmed** | Marker-counting fake agent; assertions `stage-07.out.md == "8"`, `stage-09.out.md == "9"`. In-budget outputs must stay byte-identical or AC2 breaks. |
| Unit tests at tests/test_run_review.py:57–84 call `chain_variables` positionally | ✅ **Confirmed** | Three tests (57, 69, 80) use 4 positional args → new params must be keyword-only. |
| Monkeypatch idiom `monkeypatch.setattr(mod.batch_config, ...)` at tests/test_run_review.py:184–190 | ✅ **Confirmed** | `test_run_stage_rejects_oversized_prompt` mutates `mod.batch_config.PROMPT_MAX_BYTES` at runtime. |

### 1.1 One flaw found in the requirements spec (must be corrected)

**D1 says the budget is "a single module-level constant `CHAIN_BUDGET` computed
from `batch_config` at import time".** This contradicts the spec's own test
plan: T1/T3 monkeypatch `mod.batch_config.EVIDENCE_MAX_BYTES` **after import**
and expect the bound to change. An import-time constant would freeze at
`min(65536, 61440) = 61440` and both unit tests would fail.

**Design decision: compute the budget per call** in a tiny helper
`_chain_budget()` (two attribute reads + one `min`), invoked inside
`chain_variables` when `budget=None`. Cost is negligible; it keeps the
existing monkeypatch idiom working and stays correct if config is reloaded
(`_int_setting` reads the YAML at import, but tests mutate the module
attribute, which per-call reads honor).

---

## 2. Design

### 2.1 Goals / non-goals

**Goals**
- Bound the *sum* of chained source-text bytes per stage so a normal `--all`
  run can never hit `_prompt_size_error` through chaining.
- Byte-identical behavior for in-budget runs (small reviews) — AC2.
- Explicit, greppable truncation/omission markers pointing at full outputs on
  disk — no review content destroyed.
- Reuse the existing bounded-injector semantics (`_fair_allowances`,
  `evidence.max_bytes` knob) instead of inventing new machinery.

**Non-goals** (unchanged from requirements §4)
- No new YAML keys, no new CLI flags, no changes to `PROMPT_MAX_BYTES` /
  `OUTPUT_MAX_BYTES` / `evidence.*` / `pi-batch.yaml` / `pbatch/*` /
  `ai/sdlc.yaml`.
- `_prompt_size_error` stays as the final safety net for *non-chained*
  overflow.
- Output-side rejection (single output > `OUTPUT_MAX_BYTES`) unchanged.
- Directions #2 (stage-09 count/grade extraction) and #3
  (`_AGENT_ERROR_PATTERNS` delegation) out of scope.

### 2.2 API changes (`ai/run-review.py` only)

1. **`chain_variables` signature** — extend with keyword-only parameters:

   ```python
   def chain_variables(prior_outputs: dict, stage: str, variables: dict,
                       schema_defaults: dict, *,
                       budget: Optional[int] = None,
                       paths: Optional[dict] = None) -> dict:
   ```

   - `budget=None` → `_chain_budget()` per-call (see 2.3).
   - `paths=None` → markers omit the path portion (unit-call compatible);
     `paths={"00": Path(...), ...}` (source stage id → output file) enables
     `full output: <path>` in markers.
   - The four positional parameters and all existing semantics (explicit
     value wins, missing sources skipped) are untouched → the three unit
     tests at tests/test_run_review.py:57–84 stay byte-compatible.

2. **New module helper `_chain_budget() -> int`**:

   ```python
   def _chain_budget() -> int:
       if batch_config.PROMPT_MAX_BYTES <= 0:      # unbounded prompt
           return batch_config.EVIDENCE_MAX_BYTES
       return min(batch_config.EVIDENCE_MAX_BYTES,
                  batch_config.PROMPT_MAX_BYTES // 2)
   ```

3. **New module helper `_truncate_text(text: str, allowance: int) -> str`** —
   byte-safe head truncation:

   ```python
   def _truncate_text(text: str, allowance: int) -> str:
       if allowance <= 0 or len(text.encode("utf-8", errors="replace")) <= allowance:
           return text
       return text.encode("utf-8", errors="replace")[:allowance].decode(
           "utf-8", errors="ignore")
   ```

   The byte-roundtrip is required because chaining operates on in-memory
   *strings* (unlike `evidence.py`'s `_fenced`, which reads raw bytes from
   disk). Truncating at a byte boundary mid-multibyte-character would corrupt
   the last character; `errors="ignore"` drops the partial tail byte. Byte
   count of the result is then `<= allowance` by construction — the R3
   invariant.

4. **`_stage_prompt` (line 797) passes `paths`** — the only caller in both
   `--all` and `--resume` flows (chain call at line 816):

   ```python
   paths = {src: stage_out_dir(args) / f"stage-{src}.out.md"
            for src in CHAIN_SOURCES.get(stage, {}) for _ in ...}
   variables.update(chain_variables(prior_outputs, stage, variables,
                                    defaults, paths=paths))
   ```

   (Flatten the variable→sources mapping; missing files are harmless — the
   marker only references the path string.)

5. **Import `_fair_allowances`** from `pbatch.evidence` (`from pbatch.evidence
   import _fair_allowances`). It is a private function; acceptable within the
   same repository, and pinning the shared water-fill as single source of
   truth is worth the mild coupling. Alternative (only if reviewers object):
   add a public alias `fair_allowances = _fair_allowances` in evidence.py —
   a one-line, behavior-free addition; strictly optional and not required.

### 2.3 Algorithm (`chain_variables` bounded path)

For each stage, per invocation:

1. `budget = _chain_budget()` (when `budget` kwarg is None).
2. Determine the stage's chained variables from `CHAIN_SOURCES[stage]`
   (existing fan-out unchanged).
3. **Split the budget equally across chained variables** (stage 07 has 2):
   `var_budget = budget // nvars`. Rationale: giving each variable the full
   per-stage budget would allow 2 × 65536 > 122880 — re-triggering the very
   gate being fixed. (This is why the per-stage budget is
   `PROMPT_MAX_BYTES // 2`, not `PROMPT_MAX_BYTES`.)
4. Within a variable: for each source, compute `size` = UTF-8 byte length of
   the source text (the text after the `--- Stage NN output ---\n` prefix —
   prefixes/separators are counted in R3's overhead arithmetic, not the
   budget), then `allowances = _fair_allowances(sizes, var_budget)`.
5. Build each part with the **existing** `--- Stage NN output ---\n` prefix:
   - full text if `len(bytes) <= allowance` (byte-identical to today);
   - else `_truncate_text(text, allowance)` + marker
     `[stage NN output truncated: showing {shown} of {total} bytes; full output: {path}]`
     (path part omitted when `paths` is None);
   - allowance 0 → source omitted entirely (counted in step 6).
6. If any source was omitted, append one trailing marker
   `<chained-sources-omitted count="N" />` (mirrors evidence.py's
   `<evidence-sources-omitted .../>` contract).
7. Join with `"\n\n"` as today.

Marker format (pinned, greppable; `"truncated"` substring is asserted by
AC1):

```
[stage 05 output truncated: showing 6826 of 30720 bytes; full output: /…/stage-05.out.md]
```

### 2.4 Compatibility constraints

| Constraint | Enforcement |
|---|---|
| Positional call shape intact | New params keyword-only with defaults (R4) |
| In-budget runs byte-identical | `_truncate_text` returns original text when within allowance; prefixes/joins unchanged |
| Explicit context values win | Existing `current != schema_defaults.get(var)` check untouched, runs before any budget logic |
| Missing sources skipped | Existing `if text:` check untouched |
| `--- Stage NN output ---` format preserved | AC2 pins it (marker-counting e2e test reads prefixes via `grep`-style counting in fake agent) |
| No config/YAML/CLI surface changes | Budget derives from existing `evidence.max_bytes` / `prompt.max_bytes` |
| `_prompt_size_error` unchanged | Remains the last line of defense for non-chained overflow |
| `pbatch/evidence.py` unchanged | `_fair_allowances` imported read-only |

### 2.5 Failure modes and mitigations

| Failure mode | Behavior | Mitigation / outcome |
|---|---|---|
| Budget < number of sources (T3: `EVIDENCE_MAX_BYTES=3`, 9 sources) | Water-fill gives 1 byte to first 3 sources, 0 to 6 → 6 omitted, `<chained-sources-omitted count="6" />` | Deterministic, pinned by T3; never an unbounded paste (fail closed toward omission, never toward gate hit) |
| Degenerate `PROMPT_MAX_BYTES=1` → `_chain_budget() = min(E, 0) = 0` | All sources omitted; prompt shrinks to template + fixed vars (~2 KiB) | Gate cannot fire; omission marker self-documents |
| Operator raises `evidence.max_bytes` above `prompt.max_bytes` | `min(..., PROMPT_MAX_BYTES // 2)` caps chained text at half the gate | The bound itself can never re-trigger the gate — the exact class of bug being fixed |
| `PROMPT_MAX_BYTES = 0` (unbounded prompt, config allows) | Budget falls back to `EVIDENCE_MAX_BYTES` | Chaining still bounded by the evidence knob |
| Multibyte characters at truncation boundary | `_truncate_text` byte-roundtrips with `errors="ignore"` | No mojibake; shown bytes ≤ allowance by construction |
| Marker path unknown (pure unit calls) | Path portion omitted; stage id + byte counts stay | AC1b-compatible; markers still greppable |
| Truncated head hides tail findings (e.g. stage-09 CTO prompt) | Every marker names the full file on disk | Accepted contract of the bounded injector (same as evidence.py); full review never destroyed |
| `paths` contains a stage whose output file was later deleted | Marker references a stale path | Cosmetic only; text is already bounded and injected; no crash |
| Non-chained overflow (huge context files / `stage_09` overrides) | `_prompt_size_error` still rejects | Unchanged fail-closed safety net — out of scope by design |
| Chained text legitimately near the budget AND prompt near gate (adversarial config: `prompt.max_bytes=1000`) | Chained ≤ 500 B, overhead pushes prompt over 1000 B → gate fires | Correct: the gate is the final arbiter; the budget guarantees *normal* runs never get there, not that the gate becomes unreachable |

### 2.6 Migration steps

No data migration, no config migration, no behavior migration for existing
artifacts (outputs stay byte-identical; `--resume` flows pass through the
same bounded `_stage_prompt`).

1. **Implement** (single commit, `ai/run-review.py` only):
   - add `_chain_budget()`, `_truncate_text()`, extend `chain_variables`
     (keyword-only `budget`/`paths`), import `_fair_allowances`;
   - update `_stage_prompt` call site (line 816) to pass `paths`.
2. **Add tests** in `tests/test_run_review.py` (T1–T3 below), following the
   file's idioms (`load_runner()` at line 20, `monkeypatch.setattr(mod.batch_config, ...)`
   at lines 184–190, subprocess `--all` shape at line 223).
3. **Run gates**: `python -m pytest tests/test_run_review.py -q` →
   `make ci` → `python quality.py --strict ai/run-review.py`.
4. **Verify AC2 manually** if desired: `pytest tests/test_run_review.py::test_all_chains_stage_outputs_end_to_end` must pass unmodified.
5. **Optional doc line (R6)**: add one sentence to `ai/README.md` (~line 43–47)
   noting chained outputs are bounded by `evidence.max_bytes` with markers.
6. **Rollback**: revert the single commit; tests T1–T3 removed with it.
   No state files, no config, no schema migrations exist for this change.

---

## 3. Testable acceptance mapping

| Acceptance | Requirement | Test (in `tests/test_run_review.py`) | Assertions (deterministic) |
|---|---|---|---|
| **AC1** — new pytest: 30 KiB × 9 outputs; `--all` renders stage 09; saved prompt ≤ 122880 with `truncated` marker | R1, R2, R3 | `test_all_bounds_chained_outputs_before_prompt_gate` (e2e, T2) | Fake agent writes received prompt (`$2`) to `prompt-stage-NN.txt` and emits ≥ 30 KiB inert padding per stage (no failure signatures). Run `python ai/run-review.py --all --context ctx.yaml --agent-bin fake-agent.sh --output-dir <dir> --repo <tmp>` (shape of tests/test_run_review.py:223). Assert: `result.returncode == 0`; `prompt-stage-07.txt` and `prompt-stage-09.txt` each `len(encode("utf-8")) <= mod.batch_config.PROMPT_MAX_BYTES`; `"truncated" in prompt09`; all 10 `stage-NN.out.md` exist. |
| **AC1b** — omission-marker leg | R2 | `test_chained_omission_marker_when_budget_below_source_count` (unit, T3) | `monkeypatch.setattr(mod.batch_config, "EVIDENCE_MAX_BYTES", 3)` with 9 × 40 KiB sources → `chain_variables(..., "09", ...)` returns value with `<chained-sources-omitted count="6"`; omitted sources contribute zero text; 3 sources shown with 1 byte each. |
| **AC1 unit leg** | R1, R2 | `test_chain_variables_bounds_total_chained_bytes` (unit, T1) | `EVIDENCE_MAX_BYTES=4096`, `PROMPT_MAX_BYTES=122880`, 9 × 40 KiB sources: source-text sum ≤ 4096; all 9 `--- Stage NN output ---` prefixes present; ≥ 1 marker with stage id + shown/total bytes; each shown text is a head prefix of its original; byte-identical when all sources in-budget. |
| **AC2** — existing e2e passes unmodified | R4 | `test_all_chains_stage_outputs_end_to_end` (existing, line 223, T4) | Unmodified; marker counts `stage-07 == "8"`, `stage-09 == "9"` pin the chain format and the no-truncation-for-small-outputs property. |
| **AC3** — exit 0 + stages 06/07/09 complete | R1, R3 | Same test as AC1 (second assertion group, T2) | `result.returncode == 0`; `(out_dir / f"stage-{stage}.out.md").exists()` for stage in `("06","07","09")`. |
| Output-side / failure-signature bounds untouched | out-of-scope | Existing `test_run_stage_rejects_*` suite (T5) | Unchanged (direction #3 out of scope). |
| R6 (optional) | R6 | Manual review of `ai/README.md` line | One sentence noting the bound + markers. |

### 3.1 Requirement → implementation map

| Requirement | Implementation |
|---|---|
| R1 (bound) | `_chain_budget()` + per-variable equal split + `_fair_allowances` + `_truncate_text` inside `chain_variables` |
| R2 (markers) | Marker strings built in `chain_variables` (truncation per source, one omission marker per variable); `paths` kwarg from `_stage_prompt` |
| R3 (guarantee) | Budget ≤ `PROMPT_MAX_BYTES // 2` guarantees chained text + measured overhead (prefixes ~24 B × 9, markers ~130 B × 9, template + fixed vars ~2 KiB) ≈ 66–68 KiB « 122880; `_prompt_size_error` untouched |
| R4 (compatibility) | Keyword-only kwargs; existing checks untouched; AC2 pinned |
| R5 (tests) | T1–T3 new, T4 existing, T5 existing |
| R6 (doc, optional) | One line in `ai/README.md` |

### 3.2 Key numbers (verified live)

- `PROMPT_MAX_BYTES` = 122880; `EVIDENCE_MAX_BYTES` = 65536;
  `CHAIN_BUDGET` = `min(65536, 61440)` = **61440** per stage; stage 07 →
  30720 per variable.
- Stage 09 water-fill on 9 × 30 KiB: `[6827×6, 6826×3]`, sum 61440.
- Filled prompt ≈ 61440 + ~216 (prefixes) + ~1170 (markers) + ~2 KiB
  (template + fixed vars) ≈ **66–68 KiB** — 54–56 KiB headroom under the gate.
- Current-code failure thresholds (measured): stage 09 ≥ 13.12 KiB/source,
  stage 07 ≥ 14.72 KiB/source, stage 06 ≥ 29.51 KiB/source; at 30 KiB/source
  all three fail (122.1 / 242.3 / 272.0 KiB > 122880).

---

## 4. Design validation (prototype run, live)

The §2.2/§2.3 algorithm was prototyped and executed against the real
`CHAIN_SOURCES`, `STAGE_VARS`, and `_fair_allowances`:

| Check | Result |
|---|---|
| Stage 09, 9 × 30 KiB sources, budget 61440 | Chained value = 62,536 B; all 9 `--- Stage NN output ---` prefixes present; 9 truncation markers with stage id + shown/total + path (`showing 6827 of 30720 bytes`); 0 omission markers |
| Stage 07 (2 variables, 30720 each) | `CRITICAL_HIGH_FINDINGS` = 31,572 B, `ARCHITECTURE_OUTPUT` = 30,744 B (source text ≤ 30720 each; the excess is prefix/marker overhead, which is outside the source-text budget by design and counted in R3's ~2 KiB overhead arithmetic); total 62.3 KiB « gate |
| Budget = 3, 9 sources | Exactly 3 sources shown with 1 B each; `<chained-sources-omitted count="6" />` — matches T3 assertions |
| In-budget run (small outputs) | Output byte-identical to current `chain_variables` — AC2 property holds |
| Multibyte truncation (CJK/accents at boundary) | No `\ufffd` replacement chars; shown bytes ≤ allowance |

Feasibility confirmed: no iteration of the design re-triggers
`_prompt_size_error`; markers satisfy the AC1 `"truncated"` greppability
assertion; the only correction vs. the requirements spec is §1.1 (per-call
budget instead of import-time constant).
