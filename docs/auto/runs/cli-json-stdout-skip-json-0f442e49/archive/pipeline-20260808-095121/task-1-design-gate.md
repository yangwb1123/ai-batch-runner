Independent verification complete. Here is what I confirmed directly in the current tree:

**Resolved with evidence:**
- **F1 (the only Medium finding, the sole open contract question)** — verified live: `--skip --json` / `--skip --strict` → **exit 2** "expected one argument" on both checkers (was silent exit-0 human-mode false pass). Pinned by `test_backend_flaglike_value_rejected` (4 flag pairs + bare-dash legal) and `test_frontend_flaglike_value_rejected`. Adjudication recorded in `DECISIONS.md` (2026-08-08 17:45 entry) with the live family evidence that overturned "accept-and-document"; design doc F3 row updated.
- **stdout purity** — live: both checkers emit exactly one `json.loads`-clean document; summaries on stderr.
- **Trailing `--skip`** — live: exit 1, pure JSON with visible phantom `'--skip'` violation (non-silent per design F3).
- **health** — live: `healthy: true`, keys unchanged; `raw_decode`/`JSONDecoder` = 0 occurrences in `pbatch/health.py`.
- **4 affected test files: 102 passed** (91 baseline + 9 AC tests + 2 F1 tests).

**Explicitly rejected as non-blocking with evidence:**
- QA F2/F3/F4/F6 + trailing-skip test additions, AC2-vacuity fix, and health-test isolation are **still absent** from the tree — but each was adjudicated non-blocking: QA lead ("实现可接受，附补测建议"), api_designer ("coverage suggestions, not blocking"), and testing_reviewer verified every prescribed behavior **correct live** (explicit-file immunity, positional no-op, all-excluded asymmetry, scan_error purity), so the missing tests pin already-correct behavior. The replace-not-union trap remains caught by AC3's `--skip scratch` line. `test_health_main_json_shape`'s live-tree dependence is a documented settle-then-rerun flake risk, not a correctness defect.
- Full `make ci` re-run remains blocked only by the concurrent campaign's pi-batch lock (21 exit-5 failures, proven environmental) and pre-existing HEAD baseline style warnings — both documented, unrelated to this change.

No blocking findings remain open.

VERDICT: PASS - the only blocking finding (F1 silent flag-swallow false-pass) is resolved by the exit-2 hardening with tests, live reproductions, and a decision-log record; remaining QA items are explicitly adjudicated non-blocking coverage suggestions whose behaviors were independently verified correct; all 102 affected tests pass and the --json/--skip/health contracts are clean end-to-end.
