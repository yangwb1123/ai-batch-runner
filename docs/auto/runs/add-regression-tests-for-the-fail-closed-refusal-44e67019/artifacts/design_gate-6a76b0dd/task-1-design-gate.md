All evidence gathered. Summary of independent verification:

**Verified resolved (green):**
- Gate frozen byte-identical (NG1), duplicate deleted, `refusalcheck` registered (24 entries), AGENTS.md:70 = "24 个验证器"
- Unknown `--steps` name → exit 2 (`test_check_rejects_unknown_step` + live); `--steps refusal` → exit 0; 27 tests pass
- H-1 (lock contention / make ci red under live campaigns), devops H-1 (cli_batch.py), devops M-1 (absolute `completion_go` path): all **pre-existing, explicitly rejected as out-of-scope with evidence** (stash-baseline reproductions, separate directions)

**Blocking findings — neither resolved nor explicitly rejected:**
1. **Marker drift/identity (testing_reviewer HIGH; qa_lead M-1+I-1):** `test_marker_list_not_purged` is count-only (≥10); `test_marker_refuses` never parses the `markers: [...]` hit list. I reproduced the masking live: purging `FAILED — 输入缺失` still yields rc 1 via surviving `输入缺失`/`FAILED — 输入`, and a same-count rename evades both guards. No exact-list identity pin exists.
2. **Blank `--steps` filter no-op (qa_lead M-2; testing_reviewer Medium):** verified live — `--steps " "` and `--steps ","` both print `CHECK: all 0 gates passed`, exit 0. Contradicts the design's own A4.3 "never a no-op". No `test_check_rejects_blank_steps` exists.
3. **Wrong-exit smoke branch unpinned (testing_reviewer Medium):** `_run_refusal_step` implements it, but only missing-registry/missing-script tests exist — no stub-script wrong-exit test.

qa_lead made closure of M-1+M-2 an explicit condition of acceptance ("ship after closing M-1 and M-2"); the run has no post-review gate/decision rejecting them. Advisory items (M-3 near-miss boundary — behavior exists but unpinned; permission/binary tests) remain open but non-blocking.

VERDICT: FAIL - the implementation is otherwise verified green (gate byte-identical, registry+selfcheck wired, 27 tests pass, unknown-step rejection works), but two blocking findings from independent reviews remain unresolved with no rejection evidence: marker drift/identity is unpinned (count-only guard + masking demo reproduced, no hit-list/exact-list assertion) and blank `--steps " "/","` still exits 0 with "all 0 gates passed" (A4.3 "never a no-op" violated), plus the wrong-exit smoke branch has no regression pin — each is a small fix qa_lead and testing_reviewer explicitly made a condition of acceptance.
