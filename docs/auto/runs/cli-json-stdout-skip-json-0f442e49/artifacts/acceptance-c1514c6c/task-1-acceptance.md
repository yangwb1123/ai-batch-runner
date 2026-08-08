All verification complete. Summary of evidence:

## Acceptance checks — all verified live + by tests

| AC | Live probe | Test |
|---|---|---|
| **AC1** backend `--dir . --json` stdout = pure JSON | ✅ skipped/ok/violation paths all `json.loads`-clean; summaries on stderr | `test_backend_json_stdout_contract` PASSED |
| **AC2** backend `--skip` skips excluded dirs | ✅ `scratch` 2→1 (mechanism); `generated,migrations` → 2 (idempotent); `""` no-op | `test_backend_skip_flag` PASSED |
| **AC3** frontend `--skip e2e` works | ✅ default 2; `--skip scratch` → 1; `--skip e2e` → 2 (negative control: replace-impl would yield 3); `scratch,e2e` → 1 | `test_frontend_skip_flag` PASSED |
| **AC4** health raw_decode removable, `--json` unchanged | ✅ 0 `raw_decode` occurrences; uniform `--skip SKIP`; plain `json.loads` + fail-closed `-1` sentinel; shape `[checkers,governance,healthy,quality_violations,score,specs,tests]`, `backendquality: 0`, `healthy: True` — baseline-identical | `test_health_checker_plain_json_contract`, `test_health_main_json_shape` PASSED |
| **AC5** ui-spec `--json` no regression | ✅ clean path `json.loads` OK, total 0, summary on stderr | `test_json_clean_path_stdout_pure` PASSED; existing ui-check tests untouched/green |

**Beyond ACs**: R1 segment-exact matching live-confirmed (root `scratch.go` kept, `a/scratch/d.go` excluded); C1/C2 guards live (`-o`-only keeps human summary on stdout, report file parses; non-json byte-identical); F1 exit-2 hardening on `--skip --json`/`--skip --strict` (both checkers, live exit 2 + tests); trailing `--skip` → exit 1 with visible phantom violation, stdout still pure; explicit-file immunity confirmed. The uncommitted `check-ui-spec.py` diff is the **concurrent ai-1f0b642b campaign's** ui_tokens change — not this direction's scope, behavior verified non-regressed.

**Gates**: 4 affected files **109/109**; 15 AC-mapped tests all PASSED; `quality.py --strict` OK; eval 42/42; backend file 498 ≤ 500 lines (trim restored the filesize gate); `cli.py check` FAILs/WARNs byte-identical at HEAD (pre-existing, in unrelated `iris-ui`/`ui-geometry` files); full suite 1033 passed / 21 failed — every failure confirmed as the concurrent campaign's exit-5 lock (`Another pi-batch instance is running pid=1547473`), environmental.

VERDICT: PASS - all five acceptance checks and every R1-R5 requirement are satisfied with live reproductions and 109/109 passing tests in the affected files (15 AC-mapped tests by name), the health/json/skip contracts are clean end-to-end with zero `raw_decode` and baseline-identical `health --json` output, and the only full-suite failures are proven environmental (concurrent campaign lock, identical at HEAD baseline).
