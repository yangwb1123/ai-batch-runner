All evidence gathered. Here is the QA lead review.

---

# QA Lead Review — Design `ai-1f0b642b` (bound chained stage outputs before prompt-size gate)

**Scope note:** The design is not yet implemented — no commit exists. This review evaluates the *test plan* (T1–T5) against the live code and the design's own claims, with measurements reproduced. Review is advisory; nothing was modified.

## 1. Test inventory and commands actually run (this revision)

| Command | Result | Meaning |
|---|---|---|
| `python -m pytest tests/test_run_review.py -q` | **21 passed** in 3.59 s | Baseline green: existing chaining e2e (AC2/T4), all rejection-signature tests, resume tests |
| `python -m pytest tests/test_run_review.py tests/test_entrypoints.py -q` | **27 passed** in 7.39 s | Adjacent entrypoint coverage |
| `make ci` (default CI: check + self-test + test + quality + org) | 926 passed, **19 failed** in 118.7 s | All 19 failures are lock-refusal (exit 5: *"Another pi-batch instance is running (pid=1684832…) Refusing to start"*) — a live 7×24 campaign in this repo holds `.pi-batch.lock` (Verified by re-running 2 failures in isolation, same exit-5 message). **Environmental, not a regression**; none touch `ai/run-review.py` |
| `python quality.py --strict ai/run-review.py` | **OK** | File 896 lines < 1000 budget; new helpers must stay ≤ 50-line function budget |
| Live measurement harness (`/tmp/qa_harness.py`, through real `_stage_prompt` + `_fair_allowances`) | see §2 | Reproduces every numeric claim; also disproves one (F1) |

**Suite taxonomy:** Default CI (`make ci`) runs *all* of `tests/`; there is no `-m` exclusion, so tagged suites are not separate. Tags present: `@pytest.mark.chaos` (tests/test_hypergraph_fabric.py:200,239), `@pytest.mark.contract`/`@pytest.mark.scenario` (test_org.py), `@pytest.mark.skipif` (test_entrypoints.py:41). **None apply to `ai/run-review.py`**; no chaos/load/benchmark/conformance suite covers the chaining path — all proposed coverage lives in default CI, which is appropriate for a deterministic runner change.

## 2. Design claims — verified / refuted (measured this revision)

| Claim | Verdict | Measured |
|---|---|---|
| 30 KiB/source kills stages 06/07/09 | ✅ Verified (matches design's *refined* numbers exactly) | 124,988 / 248,082 / 278,493 B (122.1/242.3/272.0 KiB) — all > 122,880 |
| Thresholds 13.12 / 14.72 / 29.51 KiB | ✅ Verified (stage 06: 29.49 vs design 29.51; 21 B delta from ctx setup — immaterial) | 13,430 / 15,070 / 30,194 B |
| Water-fill `[6827×6, 6826×3]` | ✅ Verified | `_fair_allowances([30720]*9, 61440)` exact |
| budget=3 → exactly 3×1 B, `count="6"` | ✅ Verified | prototype output matches T3 |
| Multibyte truncation, no mojibake | ✅ Verified | 999 B shown ≤ 1000 allowance, no U+FFFD |
| `PROMPT_MAX_BYTES=1` → budget 0, all omitted | ✅ Verified | chain = 37 B (marker only) |
| Fixed overhead ≈ 2 KiB | ✅ Verified | stage 09 fixed = 1,818 B |
| In-budget byte-identical (AC2) | ✅ Plausible by construction | truncation branch only when `size > allowance`; prefixes/joins untouched |
| **R3 guarantee: "normal runs can never hit the gate through chaining"** | ❌ **Refuted — see F1** | marker path unbounded; path_len ≈ 6,609 B re-triggers the gate (123,871 B) |

## 3. Findings (sorted by severity)

**F1 — Medium — Marker embeds unbounded path; R3 guarantee violated by operator path length.** The budget bounds *source text only*; each truncation marker appends `full output: <path>` where `<path>` is the full `--output-dir` path (design §2.2.4). With 9 truncated sources the prompt grows ≈ 9 × len(path) beyond the 61,440 B budget. Measured with the prototype: path_len 6,609 → total prompt 123,871 B > 122,880 → stage 09 rejected again — the *exact failure class the design claims to eliminate* (`_prompt_size_error` re-fires), reachable without any reviewer-content input. Fail-closed (no data loss), hence Medium. **Fix:** cap displayed path (e.g., last 120 bytes + `…`) or count marker bytes in the budget. **Exact test:** unit — `budget=4096`, one 40 KiB source, `paths={src: "x"*8000}`; acceptance assertion `assert len(value.encode("utf-8")) <= 4096 + 512` plus `"output truncated" in value`.

**F2 — Medium — Resume/recovery path untested for oversized outputs.** `--resume` shares `_stage_prompt` (single caller at run-review.py:816, verified), so the code path is identical — but nothing proves it. Regression risk: a future edit making resume chain from raw files instead of the bounded path would be silent. **Exact test:** first `--all` run with an agent that rate-limit-rejects stage 09 (idiom at test_run_review.py:264–283), then `--resume` with the big-output agent; acceptance: `result.returncode == 0` and `prompt-stage-09.txt` ≤ 122,880 B with `"truncated"` present.

**F3 — Medium — Two designed behaviors have zero planned tests.** (a) `_chain_budget()`'s unbounded-prompt branch (`PROMPT_MAX_BYTES <= 0 → return EVIDENCE_MAX_BYTES`) and the degenerate `PROMPT_MAX_BYTES=1 → 0` case are documented failure modes with no test. (b) The public `budget=` kwarg is **never exercised** — T1/T3 monkeypatch `batch_config` instead, so the kwarg exists untested. **Exact tests:** `monkeypatch.setattr(mod.batch_config, "PROMPT_MAX_BYTES", 0)` + `EVIDENCE_MAX_BYTES=4096` → `mod._chain_budget() == 4096`; and `chain_variables(prior, "09", vars, defaults, budget=512)` with 9×40 KiB → source-text sum ≤ 512 (no monkeypatch).

**F4 — Low — AC1 under-pins the marker contract and skips stage 06.** Only `"truncated" in prompt09` is asserted, and prompt-size bounds are asserted for stages 07/09 only — yet stage 06 has the *lowest* per-source threshold (29.49 KiB) and is one of the three stages that die today. The design calls the marker format "pinned" but no assertion pins it. **Add to AC1:** `re.search(r"\[stage 0[0-9] output truncated: showing \d+ of \d+ bytes; full output: .*stage-0[0-9]\.out\.md\]", prompt09)` (also proves `paths` reached the marker, which is otherwise untested) and `len(prompt06) <= PROMPT_MAX_BYTES`.

**F5 — Low — AC1 fake-agent spec has two correctness holes.** (a) `echo "$2"` is not byte-transparent across shells (dash/busybox backslash handling); the test measures the *echoed* prompt, not the true one. Use `printf '%s' "$2" > prompt-stage-NN.txt`. (b) The design never says how the agent learns NN — the existing idiom derives the stage from the prompt heading (`case "$2" in *# Stage 02"*)`, test_run_review.py:265). **Round-trip sanity:** assert the template heading (`# Stage 09`) exists in the captured prompt. Padding must avoid failure signatures (existing pattern) and stay < `OUTPUT_MAX_BYTES` (2 MiB default — 30 KiB is safe).

**F6 — Info — Marker-contract divergence.** `<chained-sources-omitted count="N" />` (design §2.3) omits the `max-sources`/budget attributes of the evidence.py contract (`<evidence-sources-omitted count="…" max-sources="…" />`, evidence.py:88). Cosmetic; consider `count` + budget for diagnostics. Also `_fair_allowances` is a private import — design already flags the optional public alias.

**F7 — Info — `make ci` is not hermetic under concurrent 7×24 campaigns.** 19 CLI e2e tests fail with lock exit 5 while the campaign holds `.pi-batch.lock` (measured). Unrelated to this design, but any "CI green" exit criterion must state *no other pi-batch instance running*. The proposed T2 e2e invokes `ai/run-review.py` directly (no lock), so it is unaffected — Verified.

**Not a finding (checked):** no credential/tenant/replay surface exists in this runner change (local agent binary, operator-controlled paths); markers are fixed-format and inert — a hostile output cannot extend its own injected bytes beyond the water-fill allowance, and marker text is never parsed downstream. `_fair_allowances` is deterministic despite `set` iteration (final branch iterates `sorted(pending)`; small-branch assigns per-index amounts — order-independent, Verified). No race surface: `--all` is strictly serial.

## 4. Requirement-to-test matrix

| Requirement | Planned test | Status | Evidence |
|---|---|---|---|
| R1 bound | T1 unit (`test_chain_variables_bounds_total_chained_bytes`) + T2 e2e (AC1) | Planned; algorithm verified live; **needs F1 fix + F3(b) kwarg test** | harness §2 |
| R2 markers | T3 omission unit; T2 `"truncated"` | Planned; **format not pinned (F4)**; `paths`-in-marker untested (F4) | harness (count=6 ✓) |
| R3 no-gate guarantee | T2 (exit 0, ≤122880) | **Refuted for long paths (F1)**; degenerate configs untested (F3a) | path sweep §2 |
| R4 compatibility | T4 existing e2e unmodified; 3 positional unit tests (lines 57–84) | Baseline green (21/21); positional shape intact (verified 4-arg calls) | pytest run |
| R5 tests | T1–T5 | Planned; gaps F2–F5 | — |
| R6 doc | manual | Missing (optional; ai/README.md:43–47 inspected, insertion point identified) | read |
| Non-chained overflow still rejected | existing `test_run_stage_rejects_timeout` PROMPT_MAX_BYTES=1 leg (line 184–190) | Covered, unchanged | pytest run |
| AC3 exit 0 + stages 06/07/09 exist | T2 second assertion group | Planned | — |

## 5. Prioritized scenario list (happy / boundary / error / recovery)

**P0 — acceptance-blocking:** ① `--all` with 9×30 KiB outputs → 10 outputs, exit 0, prompts 06/07/09 ≤ 122,880, `"truncated"` present (T2/AC1/AC3); ② existing small-output e2e unmodified (T4/AC2); ③ omission unit, `EVIDENCE_MAX_BYTES=3`, `count="6"`, 3×1 B (T3); ④ bound unit, budget 4096, 9×40 KiB, prefixes + marker + head-prefix property (T1).
**P1:** ⑤ stage-07 two-variable equal split — `budget=100`, 2 vars × 3 sources, each ≤ 50 (the invariant that prevents 2×65536 re-triggering — only weakly covered today); ⑥ F1 long-path marker bound; ⑦ F3 budget-kwarg + `PROMPT=0`/`PROMPT=1` branches; ⑧ F2 resume recovery with oversized outputs; ⑨ multibyte boundary unit (CJK at allowance, no U+FFFD, ≤ allowance); ⑩ explicit-value-wins combined with tiny budget.
**P2:** ⑪ missing sources skipped under budget; source exactly = allowance (no marker) vs +1 byte (marker, 1 B dropped); ⑫ adversarial `prompt.max_bytes=1000` → chain ≤ 500 and gate still rejects (documents §2.5 last row); ⑬ stage-06 size assertion + marker regex (F4); ⑭ stale marker path (cosmetic — document, skip test).

## 6. CI gaps, flake risks, fixtures, exit criteria

- **CI gaps:** `make ci` non-hermetic under live campaigns (F7). No chaos/load/conformance suite exists for run-review — not needed for this deterministic change; the new e2e belongs in default CI.
- **Flake risks:** low. e2e subprocesses are `tmp_path`-isolated, fake agents deterministic, 120 s timeouts (current suite: 3.59 s total). Risk concentrates in F5 (shell echo portability) — use `printf`.
- **Fixtures needed:** one new fake agent fixture (prompt-capturing, ≥30 KiB inert padding, stage id from prompt heading); reuse `fake_agent`/`load_runner`/`monkeypatch.setattr(mod.batch_config, …)` idioms. `--repo` must point at the capture dir (agent cwd = `args.repo`, run-review.py:598).
- **Exit criteria:** ① `python -m pytest tests/test_run_review.py -q` green with T1–T3 + F1–F5 additions; ② `python quality.py --strict ai/run-review.py` stays green (file ≤ 1000 lines, helpers ≤ 50 lines); ③ `make ci` green **with no concurrent pi-batch instance** (or the 19 lock tests excluded — maintainer call); ④ T4 byte-identical e2e unmodified; ⑤ (optional) one-sentence R6 doc line in ai/README.md; ⑥ F1 fix lands before merge — it is the only finding that breaks the design's own central guarantee.

**Bottom line:** The design is well-evidenced (every numeric claim reproduced), the test plan is sound in shape, and AC2's byte-identical pin is the right regression anchor. Before implementation: fix the unbounded marker path (F1), and add the four cheap missing tests (F2 resume, F3a/F3b budget branches, F4 marker-pin + stage-06 assertion) so the plan matches its own failure-mode table.
