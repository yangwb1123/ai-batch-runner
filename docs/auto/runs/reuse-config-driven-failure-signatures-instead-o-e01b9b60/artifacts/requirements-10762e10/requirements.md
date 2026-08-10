Requirements specification produced at `docs/auto/runs/reuse-config-driven-failure-signatures-instead-o-e01b9b60/artifacts/requirements-d4455a35/requirements.md`.

**Evidence verification results** (all cited symbols confirmed):

| Citation | Verified |
|---|---|
| `ai/run-review.py:405` | `_AGENT_ERROR_PATTERNS` — exactly 39 hard-coded regexes (the only `re.compile` occurrences in the file) |
| `ai/run-review.py:451` | private `agent_failure_reason()`; consumed at `:533` in `_invoke_review_agent` → rejects stage, no artifact |
| `pbatch/runner.py:89` | canonical version, delegates to `config.agent_error_patterns()` at `:100` |
| `pbatch/config.py:206` | `agent_error_patterns(bin_name="")` — per-agent > global > built-in defaults; reads `AGENT_BIN` at call time; empty list disables |
| `pi-batch.yaml:25,35` | commented global and per-agent `error_patterns` knobs — honored only by `pbatch`, never by the review runner |
| Drift | Counts match today (39 = 39) but diverge by construction; repo precedent `check-no-refusal.py` elimination (commit `1d4e78a`) |

**Key findings baked into the spec:**
- `ai/run-review.py` already imports `pbatch.config` (`:58`) and threads `agent_bin` through `_invoke_review_agent` (`:485, :561, :695`) — delegation is a small, dependency-free change.
- `pbatch/cli.py:363` (`config.AGENT_BIN = args.agent_bin`) is the established in-repo pattern for per-agent resolution; the spec offers it or explicit `bin_name` passing as the two acceptable implementations.
- Acceptance checks preserved 1:1 and made testable: per-agent override honored end-to-end (REQ-6 with isolated config injection via `_AGENTS_CFG` monkeypatch or `PBATCH_SCRIPT_DIR`, plus positive control), zero local copies enforced by grep-able checks, and the new `tests/test_run_review.py` case.
- Non-goals fence the scope: no changes to config semantics, default pattern set, or the review runner's session/validation machinery.

The spec contains 6 functional requirements, a verification evidence table, implementation guidance, risks (exact-name matching, test isolation), and a definition-of-done checklist.
