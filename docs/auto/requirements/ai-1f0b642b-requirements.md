# Requirements Specification — Bound chained stage outputs before the prompt-size gate so `--all` decision stages cannot fail on realistic outputs

| Field | Value |
|---|---|
| Module | `ai` |
| Direction id | `1f0b642b` |
| Source analysis | `docs/auto/analyses/ai-1f0b642b.json` (direction #1) |
| Status | Proposal (evidence-verified) |
| Scope | This direction only — no expansion |

---

## 1. Summary

`ai/run-review.py` chains **complete** prior stage outputs into downstream
prompts: `chain_variables` (run-review.py:373) pastes each source's full text
(each up to `OUTPUT_MAX_BYTES` = 2 MiB) as `--- Stage NN output ---\n{text}`
into paste-style variables. `--all` then hard-rejects any filled prompt over
`PROMPT_MAX_BYTES` (122880, from `pi-batch.yaml prompt.max_bytes`) in
`_prompt_size_error` (run-review.py:507) **before** the agent is spawned, so
the decision stages — 06 (4 sources), 07 (8 chained entries across 2
variables), 09 (9 sources) — fail on any run whose prior reviews total more
than ~120 KiB. Every stage's agent call before that point is wasted, `--all`
exits nonzero, and no CTO decision (stage 09) is ever produced.

Concretely: for per-stage outputs of size *S*, stage 09's prompt is ≈ 9·S +
template (~2 KiB); any *S* ≳ 13 KiB triggers the gate. "Realistic outputs"
per the direction's own framing — a review longer than 13 KiB — suffices.

`pbatch` already ships a fair-budget bounded injector
(`pbatch/evidence.py:combine_outputs`, budgeted by `evidence.max_bytes` =
65536 in `pi-batch.yaml`) that this runner does not reuse. The fix: bound the
total chained **source-text bytes** per stage to a config-derived budget
(reusing `evidence.max_bytes` semantics and the water-fill fair split), keep
the existing `--- Stage NN output ---` chain format for untruncated sources,
and append explicit truncation/omission markers pointing at the full outputs
on disk. The prompt-size gate stays as the final safety net for non-chained
overflow (huge context files, etc.) — unchanged.

---

## 2. Evidence verification

Every citation in the direction was re-verified against the current repository
(all line numbers verified live).

| Cited evidence | Verification result |
|---|---|
| `ai/run-review.py:chain_variables` | ✅ **Confirmed.** `def chain_variables(prior_outputs, stage, variables, schema_defaults)` at **line 373** (body 373–391). No bound of any kind: `parts.append(f"--- Stage {src} output ---\n{text}")` pastes the full `prior_outputs[src]` text; joined with `"\n\n"`. |
| `ai/run-review.py:CHAIN_SOURCES` | ✅ **Confirmed, with numeric refinement.** Module constant at **lines 358–371**. Chain inventory (Appendix A): stage 06 → 4 sources; stage 07 → **8 chained entries** (7 for `CRITICAL_HIGH_FINDINGS` [00–06] + 1 for `ARCHITECTURE_OUTPUT` [01]) — the direction's "7" counts only the first variable; stage 09 → **9 sources** (00–08) — the direction's "8" is a minor undercount. The problem is therefore *understated*, not overstated: worst case is 9 × 2 MiB = 18 MiB into one prompt. |
| `ai/run-review.py:_prompt_size_error` | ✅ **Confirmed.** `def _prompt_size_error(prompt)` at **lines 507–512**: `size = len(prompt.encode("utf-8", errors="replace"))`, `limit = batch_config.PROMPT_MAX_BYTES`; returns a rejection message when `size > limit`. Called from `run_stage` (line ~566) **before** the agent is spawned; the stage is rejected (rc 1, no output file). |
| `ai/run-review.py:_DEFAULT_STAGE_VARS` | ✅ **Confirmed.** Module constant at **line 85** (fallback schema; `ai/sdlc.yaml` replaces it when present — sdlc.yaml stage vars match the paste-style defaults, e.g. `PRIOR_FINDINGS: "(paste Critical/High findings from Stages 02-05, or 'N/A')"` for stage 06). |
| `pbatch/evidence.py:combine_outputs` | ✅ **Confirmed, with adaptation note.** `def combine_outputs(outputs, maximum=0)` at **line 12**: budget = `maximum or config.EVIDENCE_MAX_BYTES` (line 19); per-source fair allowances via `_fair_allowances` (**lines 69–87**, water-fill: small sources first, then equal split); head-preserving truncation fence `[evidence truncated: showing X of Y bytes; full artifact: {path}]` (`_fenced`, lines 89–104); omission marker `<evidence-sources-omitted count="N" max-sources="M" />` (`_omitted_marker`, lines 64–67). **Note:** `combine_outputs` takes *file paths* and emits `<evidence source=...>` fences; run-review chains in-memory strings with the `--- Stage NN output ---` format that tests depend on (AC2). Reuse = same budget knob + same water-fill/marker contract, adapted to the string-based chain format (D2/D3). |
| `pi-batch.yaml:prompt.max_bytes` | ✅ **Confirmed.** `prompt: max_bytes: 122880` (line 93). Also `evidence: max_bytes: 65536, max_sources: 64` (lines 87–90). |
| `pbatch/config.py:PROMPT_MAX_BYTES` | ✅ **Confirmed.** `PROMPT_MAX_BYTES = _int_setting(_PROMPT_CFG, "max_bytes", 120 * 1024, 0)` at **line 289**; `EVIDENCE_MAX_BYTES = _int_setting(_EVIDENCE_CFG, "max_bytes", 64 * 1024, 1)` at **line 282**; `EVIDENCE_MAX_SOURCES` at line 286. Live values in this repo: `PROMPT_MAX_BYTES = 122880`, `EVIDENCE_MAX_BYTES = 65536`, `OUTPUT_MAX_BYTES = 2097152`. |

Supporting verification (live, this repository):

```
$ python3 -c "import sys; sys.path.insert(0,'.'); from pbatch import config; \
print(config.PROMPT_MAX_BYTES, config.EVIDENCE_MAX_BYTES, config.OUTPUT_MAX_BYTES)"
122880 65536 2097152

# Templates are ~1.9–2.2 KiB each (00–09); ai/sdlc.yaml is 3.4 KiB.
# So the fixed prompt overhead is ≈ 2–4 KiB — budget arithmetic in D1 holds.
$ wc -c ai/prompts/09-cto-review.md ai/prompts/06-production-readiness.md ai/sdlc.yaml
1917 2034 3393
```

Failure threshold arithmetic (current code): stage 09 filled prompt ≈ 9·S +
~2 KiB template + fixed vars; with `PROMPT_MAX_BYTES` = 122880 the gate fires
for any per-stage output *S* ≳ (122880 − ~3 KiB) / 9 ≈ **13.3 KiB**. Stage 07
fails at *S* ≳ ~15 KiB; stage 06 at *S* ≳ ~30 KiB. A run whose reviews are
each longer than ~13 KiB cannot reach a CTO decision today — confirmed by the
existing e2e test only passing because its fake agent emits ~1-byte outputs
(`test_all_chains_stage_outputs_end_to_end`, tests/test_run_review.py:223).

---

## 3. Root-cause analysis

One unbounded path with one late, binary gate:

**The chain path is unbounded.** `chain_variables` (run-review.py:373–391)
concatenates complete prior outputs. Each source is individually bounded at
read time — `_chain_stage_output` (line 738) and `_resume_prior` both read via
`read_text_bounded(..., OUTPUT_MAX_BYTES)` — but **nothing bounds their sum**.
`CHAIN_SOURCES` (line 358) deliberately fans out (06: 4 sources, 07: 8, 09: 9)
so the sum is structurally large: up to 4–9 × 2 MiB.

**The gate is late and binary.** `_prompt_size_error` (line 507) runs in
`run_stage` on the *fully assembled* prompt. There is no degradation path —
a prompt over 122880 bytes is hard-rejected, the stage is marked failed, and
in `--all` mode the run continues only to fail every later stage identically
(`_run_stage_loop`, line 752: `if rc != 0: failures.append(stage); if not
args.all: sys.exit(rc); continue`). Agent calls spent on stages 00–05 (or
00–08) produce artifacts that can never be consumed by the decision stages;
`--all` exits nonzero; no CTO decision exists.

**The bounded machinery already exists two directories away.**
`pbatch/evidence.py` implements exactly the missing semantics — fair byte
budget (water-fill), head-preserving truncation with explicit markers, and
omission markers — driven by `evidence.max_bytes` (65536) in the same
`pi-batch.yaml` that sets `prompt.max_bytes`. `run-review.py` already imports
from `pbatch` (`from pbatch import config as batch_config`, line 29) but
never uses the evidence layer. The 65536 budget plus ~2–4 KiB fixed prompt
overhead lands at ≈ 66–68 KiB — comfortably under 122880 (D1 arithmetic).

---

## 4. Scope boundary

**In scope:**
- A bounded chaining step inside `ai/run-review.py` (single chokepoint:
  `chain_variables`, called from `_stage_prompt` line 797; both `--all` and
  `--resume` flows pass through it).
- Per-stage total chained-source-text budget derived from `evidence.max_bytes`
  (`EVIDENCE_MAX_BYTES`) and `prompt.max_bytes` (`PROMPT_MAX_BYTES`), with
  explicit truncation and omission markers (D2/D3).
- Regression tests in `tests/test_run_review.py` (AC1–AC3); `make ci` green.

**Out of scope (explicitly not part of this direction):**
- Direction #2 in the same analysis file: extracting Critical/High counts and
  grades from prior outputs into stage-09 variables — separate direction.
- Direction #3 in the same analysis file: delegating `_AGENT_ERROR_PATTERNS`
  to `pbatch/config.agent_error_patterns()` — separate direction.
- Changing `PROMPT_MAX_BYTES`, `OUTPUT_MAX_BYTES`, `evidence.max_bytes`, or
  `pi-batch.yaml` in any way (the budget is config-driven; no new YAML keys).
- Adding new CLI flags (e.g. `--chain-budget`) — the knob already exists as
  `evidence.max_bytes`.
- Changing the output-side rejection bound (a single stage output over
  `OUTPUT_MAX_BYTES` still rejects the stage, unchanged).
- Rewriting the chain format to `<evidence>` fences — AC2 pins the existing
  `--- Stage NN output ---` format for untruncated sources.
- Behavior when `PROMPT_MAX_BYTES` is exceeded by *non-chained* content
  (huge context files / stage_09 overrides): `_prompt_size_error` remains the
  final safety net, unchanged.

---

## 5. Requirements

**R1 (bound) — `chain_variables` bounds total chained source-text bytes per
stage.**
The chained content injected by `chain_variables` for one stage must respect
a per-stage byte budget applied to the **sum of source-text bytes** (the text
after each `--- Stage NN output ---\n` prefix). Budget derivation (D1):
`CHAIN_BUDGET = EVIDENCE_MAX_BYTES` when `PROMPT_MAX_BYTES <= 0` (unbounded
prompt), else `min(EVIDENCE_MAX_BYTES, PROMPT_MAX_BYTES // 2)`. When a stage
chains more than one variable (stage 07), the budget is split equally across
the stage's chained variables; within a variable, sources share the variable
budget via `pbatch/evidence._fair_allowances` (water-fill: small sources
first, then equal split — D2). Truncation is head-preserving: each source's
text is cut to its allowance, keeping the beginning (where the review's
context lives). Behavior for in-budget runs is byte-identical to today:
sources whose text fits their allowance are pasted complete with the existing
`--- Stage NN output ---` prefix, preserving AC2.

**R2 (markers) — truncation and omission are explicit and greppable.**
For each source whose full text exceeds its allowance, the chained value must
append a truncation marker carrying the stage id and shown/total bytes,
e.g. `[stage 05 output truncated: showing 6826 of 30720 bytes; full output:
<path>]`; when the output path is not known (pure unit calls), the path
portion is omitted but the stage id and byte counts stay. When a source's
fair allowance is 0 — possible only when the budget is smaller than the
number of sources — that source must be **omitted** from the chain and a
single omission marker appended, e.g. `<chained-sources-omitted count="6"
/>`. Marker formats are pinned by the AC tests (AC1b/AC3). The full outputs
always remain on disk (`<out_dir>/stage-NN.out.md`), and the marker points
at them; no review content is destroyed, only bounded at injection.

**R3 (guarantee) — a bounded run can no longer hit the prompt gate through
chaining.**
With the R1 budget, the filled prompt must satisfy
`len(prompt.encode("utf-8")) <= PROMPT_MAX_BYTES` whenever the only oversized
component is chained content. This must hold for every stage of `--all`
including the 8-entry stage 07 and the 9-source stage 09 (D1 arithmetic:
budget + per-source prefixes/markers overhead + template + fixed vars ≈
66–68 KiB « 122880). `_prompt_size_error` stays as-is: it still rejects
genuinely oversized prompts (e.g. operator-provided context) — the fix is
that normal chained runs never get there.

**R4 (compatibility) — existing behavior and tests unchanged.**
`chain_variables`'s positional signature `(prior_outputs, stage, variables,
schema_defaults)` keeps working for the existing three unit tests
(tests/test_run_review.py:57–84); the new parameters are keyword-only with
defaults (`budget=None` → config-derived; `paths=None` → markers without
paths). Explicit context values still win over chaining (existing check
`current != schema_defaults.get(var)` — unchanged). Missing sources are
still skipped. `test_all_chains_stage_outputs_end_to_end`
(tests/test_run_review.py:223) must pass **unmodified** (its ~1-byte outputs
never hit the budget, so every chained marker count is preserved).

**R5 (regression tests) — `tests/test_run_review.py` gains the tests
specified in AC1–AC3** (test plan in §7), following the file's existing
idioms (`load_runner()`, `fake_agent`-style shell fixtures, subprocess
`--all` runs with `--output-dir`, `monkeypatch.setattr(mod.batch_config,
...)` for budget overrides as in tests/test_run_review.py:184–190).

**R6 (optional, non-blocking) — documentation.** Add one line to
`ai/README.md` (near line 43–47, the chaining paragraph) noting that chained
stage outputs are bounded by `evidence.max_bytes` with truncation markers
pointing at the full files, so decision-stage prompts stay under
`prompt.max_bytes`.

---

## 6. Acceptance criteria (supplied, made testable)

The three supplied acceptance checks are preserved; each is pinned to
deterministic assertions automatable in `tests/test_run_review.py`.

**AC1 — new pytest: oversized chained outputs still render stage 09 with a
bounded prompt and markers.**
> New pytest: fake agent emits stage outputs whose combined size exceeds
> prompt.max_bytes; `--all` still renders stage 09 and asserts the filled
> prompt length <= PROMPT_MAX_BYTES with truncation/omission markers present

Testable form (one e2e test, `test_all_bounds_chained_outputs_before_prompt_gate`):

- Fixture: a fake agent that (a) writes the rendered prompt (`$2`) it
  receives to `prompt-stage-NN.txt` in the output dir, and (b) emits
  ≥ 30 KiB of inert padding (no failure signatures) per stage — 9 stages ⇒
  combined chained content ≈ 270 KiB > 122880. (Current code fails stage 06
  already at ~30 KiB × 4 sources + template ≈ 124.9 KiB > 122880; post-fix
  all stages pass.)
- Run: `python ai/run-review.py --all --context ctx.yaml --agent-bin
  fake-agent.sh --output-dir <dir> --repo <tmp>` (same shape as the existing
  e2e test at tests/test_run_review.py:223).
- Assert (AC1 leg):
  ```python
  prompt09 = (out_dir / "prompt-stage-09.txt").read_text(encoding="utf-8")
  assert len(prompt09.encode("utf-8")) <= mod.batch_config.PROMPT_MAX_BYTES
  assert "truncated" in prompt09            # truncation marker present
  ```
  and (AC1b, omission-marker leg, unit — see test plan T3):
  ```python
  monkeypatch.setattr(mod.batch_config, "EVIDENCE_MAX_BYTES", 3)   # < 9 sources
  chained = mod.chain_variables(prior_9_sources, "09", variables, defaults)
  assert "<chained-sources-omitted count=" in chained["ALL_PRIOR_FINDINGS_SUMMARY"]
  ```

**AC2 — existing end-to-end chaining test still passes.**
> tests/test_run_review.py::test_all_chains_stage_outputs_end_to_end still
> passes

Pinned: the test file's `test_all_chains_stage_outputs_end_to_end` (line 223)
runs **unmodified** and passes (`--all` with the marker-counting fake agent;
all 10 stage files present; stage-09 output `"9"`). This pins the chain
format and the no-truncation-for-small-outputs property.

**AC3 — e2e: `--all` with >120 KiB combined outputs completes the decision
stages.**
> End-to-end `--all` run with outputs exceeding 120KiB completes stages
> 06/07/09 with exit 0

Testable form (same test as AC1, second assertion group):
```python
assert result.returncode == 0, result.stderr           # no stage rejected
for stage in ("06", "07", "09"):
    assert (out_dir / f"stage-{stage}.out.md").exists()   # decision stages completed
```
(The fake agent's per-stage output is written and read back through the real
`--all` loop, so this exercises the actual chain path, not a unit stub.)

---

## 7. Test plan (`tests/test_run_review.py`)

| # | Test | Assertion | Models |
|---|---|---|---|
| T1 | `test_chain_variables_bounds_total_chained_bytes` (unit, AC1 unit leg) | monkeypatch `EVIDENCE_MAX_BYTES=4096`, `PROMPT_MAX_BYTES=122880`; prior outputs = 9 × 40 KiB strings; `chain_variables(..., "09", ...)` returns `ALL_PRIOR_FINDINGS_SUMMARY` whose *source-text* sum ≤ 4096; all 9 `--- Stage NN output ---` prefixes still present; ≥1 `truncated` marker with the source stage id and shown/total bytes; every source's shown text is a head prefix of the original | `test_chain_variables_fills_schema_placeholder` (line 57) for call shape; `_fair_allowances` unit semantics from pbatch/evidence.py:69 |
| T2 | `test_all_bounds_chained_outputs_before_prompt_gate` (e2e, AC1 + AC3) | ≥30 KiB per stage output; exit 0; `stage-06/07/09.out.md` exist; saved `prompt-stage-09.txt` ≤ `PROMPT_MAX_BYTES` bytes and contains `truncated`; saved `prompt-stage-07.txt` also ≤ limit (8-entry stage is the tightest) | `test_all_chains_stage_outputs_end_to_end` (line 223) for subprocess shape; fake agent saves `$2` per stage |
| T3 | `test_chained_omission_marker_when_budget_below_source_count` (unit, AC1b) | monkeypatch `EVIDENCE_MAX_BYTES=3` with 9 × 40 KiB sources → exactly 3 sources get 1 byte, 6 are omitted; `<chained-sources-omitted count="6"` present; omitted sources contribute no text | `_omitted_marker` contract (evidence.py:64) |
| T4 | `test_all_chains_stage_outputs_end_to_end` (existing, line 223) | passes **unmodified** (AC2) | — |
| T5 | `test_run_stage_rejects_bounded_oversized_output`, `test_run_stage_rejects_*` (existing) | unchanged (output-side and failure-signature bounds untouched — direction #3 out of scope) | — |

Also run: `python -m pytest tests/test_run_review.py -q`, `make ci`,
`python quality.py --strict ai/run-review.py`.

---

## 8. Design decisions

**D1 — budget = `min(EVIDENCE_MAX_BYTES, PROMPT_MAX_BYTES // 2)`, per stage.**
Reuses the direction's stated knob (`evidence.max_bytes` = 65536) while
guaranteeing the bound itself can never re-trigger the gate: even if an
operator raises `evidence.max_bytes` above `prompt.max_bytes`, chained text
alone stays ≤ `PROMPT_MAX_BYTES // 2` (61440 here). When
`PROMPT_MAX_BYTES <= 0` (unbounded prompt, config allows 0), fall back to
`EVIDENCE_MAX_BYTES`. Headroom arithmetic for this repo: 61440 (chained text)
+ ~0.9 KiB (9 × prefix ~21 B + marker ~80 B) + ~2–4 KiB (template + fixed
vars) ≈ **66–68 KiB « 122880**. A single module-level constant
`CHAIN_BUDGET` computed from `batch_config` at import time (same pattern as
`AGENT_BIN`/`VALIDATORS`), so tests can monkeypatch
`mod.batch_config.EVIDENCE_MAX_BYTES` / `PROMPT_MAX_BYTES` per the existing
idiom (tests/test_run_review.py:184–190).

**D2 — per-stage budget split equally across chained variables; water-fill
within a variable; reuse `pbatch/evidence._fair_allowances`.**
Stage 07 chains two variables; giving each the full per-stage budget would
allow 2 × 65536 > 122880 and re-trigger the gate (the exact class of bug
being fixed) — hence the equal split. Within one variable, sources share the
variable's budget by `_fair_allowances` (small sources first, then equal
split), imported from `pbatch/evidence.py` so the fair-split logic stays a
single source of truth (`from pbatch.evidence import _fair_allowances`).
`combine_outputs` itself is not called: it reads files and emits `<evidence>`
fences, while chaining is string-based and AC2 pins the
`--- Stage NN output ---` format; the budget constant, the water-fill
algorithm, and the marker contract are the reused parts.

**D3 — head-preserving truncation with per-source markers.**
Cut each source to its allowance from the *start* (evidence.py's `_fenced`
semantics: `shown = handle.read(allowance)`); append
`[stage NN output truncated: showing X of Y bytes; full output: <path>]` with
`path` = `stage_out_dir(args) / f"stage-{src}.out.md"` when known (R4's
`paths` kwarg, populated by `_stage_prompt`). Sources with allowance 0 are
omitted entirely and counted in one trailing `<chained-sources-omitted
count="N" />` marker (mirrors evidence.py's `<evidence-sources-omitted .../>`
— D2b). Markers are greppable and carry stage ids so a truncated CTO prompt
self-documents what to read in full from disk. Omission cannot occur in
normal runs (9 sources « 65536); it is exercised only by T3's tiny budget,
but the contract is pinned so the acceptance's "omission markers present"
is deterministic.

**D4 — bound lives in `chain_variables` (single chokepoint), not in
`_prompt_size_error`.**
`_stage_prompt` (line 797) is the only caller in both `--all` and `--resume`
flows, and `chain_variables` is the only builder of chained content, so the
bound there covers every path (fresh run, resume, dry-run). `_prompt_size_error`
stays as the unchanged last line of defense for non-chained overflow. The
new parameters are keyword-only (`budget=None`, `paths=None`) so the three
existing positional unit calls (tests/test_run_review.py:57–84) are
byte-compatible (R4).

**D5 — no new config keys, no new CLI flags.**
The budget derives from existing `evidence.max_bytes` / `prompt.max_bytes`;
operators tune chaining exactly like aggregate/meta evidence. This matches
the direction's framing ("pbatch already ships a fair-budget bounded
injector … that this runner does not reuse") and keeps `pi-batch.yaml`
untouched.

---

## 9. Impact and risks

- **Blast radius:** `ai/run-review.py` only (`chain_variables` + new
  `CHAIN_BUDGET` constant + `_stage_prompt` passing `paths`), plus new tests
  in `tests/test_run_review.py`. No change to `pbatch/evidence.py`,
  `pbatch/config.py`, `pi-batch.yaml`, or `ai/sdlc.yaml`.
- **Behavior change:** runs whose chained outputs fit the budget (small
  reviews, the current test suite) are byte-identical. Oversized runs
  previously hard-failed at stages 06/07/09; they now complete with
  head-truncated findings and explicit markers — the same trade-off
  `evidence.py` already applies to aggregate/meta prompts, documented in
  pi-batch.yaml's `evidence` comment ("Full artifacts remain on disk;
  truncation/omission markers make prompt reduction explicit").
- **Information loss:** the CTO/gatekeeper prompt receives only the head of
  each prior review; the *full* review stays on disk and every marker names
  the file. This is the accepted contract of the bounded injector; a
  reviewer needing the tail can read the artifact (the marker gives the
  path).
- **Gate remains fail-closed for genuine overflow:** non-chained oversized
  prompts (e.g. operator-supplied context files near `input.max_bytes`) are
  still rejected by `_prompt_size_error` — unchanged.
- **CI coupling:** AC2 pins the existing e2e marker-count test; R1 must not
  truncate in-budget chains or that test breaks. The e2e AC1/AC3 test runs
  ten real shell subprocess calls (~seconds) — same cost class as the
  existing e2e tests in the file.
- **Edge case:** `EVIDENCE_MAX_BYTES` has config minimum 1, so the bound is
  always ≥ 1 byte per source under water-fill; a budget smaller than the
  source count degrades to omission markers (T3), never to an uncaught
  unbounded paste.

---

## Appendix A — chain inventory (`ai/run-review.py:CHAIN_SOURCES`, lines 358–371)

| Stage | Variable | Sources | Entries | Worst case (2 MiB each) |
|---|---|---|---|---|
| 01 | PRODUCT_DISCOVERY_OUTPUT | 00 | 1 | 2 MiB |
| 02 | ARCHITECTURE_OUTPUT | 01 | 1 | 2 MiB |
| 03 | ARCHITECTURE_OUTPUT | 01 | 1 | 2 MiB |
| 04 | PRIOR_FINDINGS | 01, 02, 03 | 3 | 6 MiB |
| 06 | PRIOR_FINDINGS | 02, 03, 04, 05 | 4 | 8 MiB |
| 07 | CRITICAL_HIGH_FINDINGS | 00–06 | 7 | 14 MiB |
| 07 | ARCHITECTURE_OUTPUT | 01 | 1 | 2 MiB |
| 08 | COMMITTED_STORIES | 07 | 1 | 2 MiB |
| 09 | ALL_PRIOR_FINDINGS_SUMMARY | 00–08 | 9 | 18 MiB |

## Appendix B — key code excerpts (current, line numbers verified)

```python
# ai/run-review.py:358-371 — unbounded fan-out (the defect's shape)
CHAIN_SOURCES = {
    "06": {"PRIOR_FINDINGS": ["02", "03", "04", "05"]},
    "07": {"CRITICAL_HIGH_FINDINGS": ["00","01","02","03","04","05","06"],
           "ARCHITECTURE_OUTPUT": ["01"]},
    "09": {"ALL_PRIOR_FINDINGS_SUMMARY": ["00","01","02","03","04","05","06","07","08"]},
}

# ai/run-review.py:373-391 — pastes COMPLETE source texts, no bound
def chain_variables(prior_outputs, stage, variables, schema_defaults):
    ...
    for src in sources:
        text = prior_outputs.get(src)
        if text:
            parts.append(f"--- Stage {src} output ---\n{text}")   # full text
    if parts:
        chained[var] = "\n\n".join(parts)                          # unbounded sum

# ai/run-review.py:507-512 — late, binary gate on the ASSEMBLED prompt
def _prompt_size_error(prompt):
    size = len((prompt or "").encode("utf-8", errors="replace"))
    limit = batch_config.PROMPT_MAX_BYTES
    return (f"prompt exceeds {limit} bytes ({size} bytes)"
            if limit > 0 and size > limit else "")

# pbatch/evidence.py:69-87 — the existing fair-split algorithm to reuse
def _fair_allowances(sizes, budget):
    """Water-fill small sources first, then divide the remainder equally."""
    ...
    return allowances

# pi-batch.yaml:87-93 — the budget knob and the gate this fix must stay under
evidence:
  max_bytes: 65536
  max_sources: 64
prompt:
  max_bytes: 122880
```
