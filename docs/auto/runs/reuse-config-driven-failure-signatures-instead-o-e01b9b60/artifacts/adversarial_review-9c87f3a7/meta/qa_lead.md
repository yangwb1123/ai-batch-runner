QA lead review complete — written to `docs/auto/runs/reuse-config-driven-failure-signatures-instead-o-e01b9b60/artifacts/adversarial_review-9c87f3a7/meta/qa_lead.md`.

## Summary

**Verdict: design sound, proceed with Option A.** Every citation re-measured on HEAD `12568ca`: line numbers (405–448/451–463/533, runner 89/100, config 206, cli 363), 39=39 pattern equivalence (semantic probe over 16 strings — 0 mismatches; only raw delta is inline `(?im)` on the 2 banner patterns, cosmetic), single-caller fact for `agent_failure_reason`, and lru-cache monkeypatch safety all held.

**Commands actually run:** `tests/test_run_review.py` → 21 passed (21 + 4 `TestPerAgentErrorPatterns` = 25, matching the design claim); `test_local_fixes.py` → 93 passed, 8 failed (all lock-contention from the running campaign, environmental); `make check`, `cli.py check-test` (49 OK), `quality.py` green; measured coverage ai/run-review.py 53%.

**Key findings:**
- **F1 Medium** — invalid regex in configured `error_patterns` raises `re.error` (probe-verified). Runner contains it via `except Exception` (runner.py:234); run-review does **not** — delegation makes this crash newly reachable in the review runner. Fix in one place: catch `re.error` in `agent_error_patterns` → warn + defaults.
- **F2 Medium** — global `error_patterns: []` now silently disables review rejection (FM2); require saved-artifact assertion + runtime warning + doc line.
- **F3 Medium** — the designed AC1 positive control *cannot catch the most likely bug* (wrapper forgetting `bin_name` → falls back to `config.AGENT_BIN`). Added a decoy control: patch `AGENT_BIN="pi"` with `error_patterns: []` so a fallback bug fails the test. Also verified Option A is **required**, not just preferred — run-review never calls `_wire_agent`, so `config.AGENT_BIN` never reflects `--agent-bin`.
- **F6 Low** — AC2 grep guard should be mandatory: a `checks/test_run_review_no_local_patterns.py` unittest is auto-wired into `make ci` via `cli.py:70` discovery (precedent confirmed).
- Plus E6 replacement-semantics assertion (override replaces, not unions, the 39) and F9: full `make ci` green is blocked by the campaign's own lock — AC3 must be judged with that caveat.
