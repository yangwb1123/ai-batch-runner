Design written to `docs/auto/runs/reuse-config-driven-failure-signatures-instead-o-e01b9b60/artifacts/design-a77de8a6/task-1-design.md`.

## Verification result: all claims held, one nuance found

Every citation was re-checked against the repo — line numbers (405/451/533, runner 89/100, config 206, cli 363), the 39=39 pattern counts, test line numbers 117/129/140/151/168/193, config discovery, and the `1d4e78a` precedent. The one nuance: the two CLI-banner patterns store inline `(?im)` flags in `config.py` vs. separate flags in run-review — **compiled behavior is identical** (verified with probe strings), but `pattern.pattern` in the rejection message will render `(?im)^fatal:` instead of `^fatal:` — exactly what `pbatch/runner.py` already prints, so the review runner becomes *more* consistent. Cosmetic; no test asserts it.

## Design highlights

**Approach (Option A):** explicit `bin_name` pass-through — `_invoke_review_agent` already receives `agent_bin` as a parameter, so the change is: delete `_AGENT_ERROR_PATTERNS` (405–448) + local matcher (451–463) + `import re` (provably unused), add an 11-line `_agent_failure_reason(rc, output, agent_bin)` wrapper delegating to `batch_config.agent_error_patterns(agent_bin)`, update the call at :533. Net ≈ −47 lines. Rejected B (mutate `config.AGENT_BIN`, implicit ordering coupling) and C (import runner's version, can't take `bin_name`).

**Key constraints/failure modes surfaced:**
- No-config behavior byte-identical (defaults semantically equal, matching order preserved)
- **FM2 — real behavior change:** global `error_patterns: []` now disables review-runner rejection (previously frozen 39 always applied) — intended capability, must be in the commit message
- FM1 exact-name mismatch falls back silently (same as `pi-batch.py`, no new hazard)
- Config discovery asymmetry (bin from repo root, patterns from `PBATCH_SCRIPT_DIR`) is a declared non-goal
- Test isolation is safe: `_compile_patterns` lru-cache is content-addressed, so monkeypatching `_AGENTS_CFG` can't serve stale compilations

**Acceptance mapping:** AC1 → new test `test_run_stage_honors_per_agent_error_patterns_override` (isolated `_AGENTS_CFG` injection + banner absent from the 39 + positive control with a different bin name, plus sub-cases for global override and empty-list disable); AC2 → grep checks (`_AGENT_ERROR_PATTERNS` absent, `re.compile` count 0) with optional permanent guard in `checks/` wired into `make ci` (precedent: `test_knowledge_freshness.py`); AC3 → all 25 existing tests unchanged + `make ci`.
