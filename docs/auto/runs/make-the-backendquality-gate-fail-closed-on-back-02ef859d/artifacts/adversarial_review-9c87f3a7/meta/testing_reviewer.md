All verification complete. Here is my QA lead review.

---

# QA Lead Review — backendquality fail-closed gate

**Revision reviewed**: HEAD `75a7a27` + uncommitted worktree diff (5 files, +434/−39). Lock held by `campaign-r2` (pid 1547473) — `make ci` test phase runs impossible in-tree (H-3, independently confirmed by devops).

**What I actually ran** (all live, this session): `test_backend_specs.py` (56 passed), `test_validator_registry.py` (7 passed), `test_context_eval.py` + `test_capabilities.py` (85 total for the 4 documented files), `checks/` (49 passed), `quality.py --strict` on script and repo (OK), `cli.py check` (exit 0), `pi-batch check` (9 gates), `pi-batch health` (100.0), plus 12 live repros: AC-1, AC-2, AC-5, regular-file-dir, `--manifest` dangling, walk-up at 6 and 7 levels, registry command on this repo, `git check-ignore`, and `advance.scan_project` against a synthetic Go repo.

## VERDICT: FAIL — blocking test gaps

The headline defects (missing dir → exit 0; manifest + 0 files → exit 0) are genuinely fixed and their regression tests prove the defect cannot return. But the QA round's own High finding F2 was fixed **without a regression test**, the completion report misclassifies a run-and-failed phase as `not_executed` (completion-evidence §2 violation), and the fail-closed semantics' boundary/permission/argument-error paths are untested. Per the review checklist: normal-path-only coverage + reproduce-first violations + honesty misclassification = FAIL.

## Findings table

| # | Sev | Defect pattern | Missing test | Test that would catch it |
|---|---|---|---|---|
| B1 | **High** | F2 (prior QA round, High): `--dir <regular file>` vacuous-passed; code now returns `scan_error: not a directory` (verified exit 1) — but **no test pins it**; a future refactor can silently restore the bypass | `test_backend_checker_regular_file_dir_fails_closed` | `_run("--dir", str(afile), "--strict")` → returncode 1, `"not a directory"` in stderr |
| B2 | **High** | completion-evidence §2: "已运行但失败 / 未运行——必须区分" violated — `make ci (test phase)` classified `not_executed` while its own reason describes observed failures ("21 CLI-spawning tests fail…every failure carries that message"); the labeled command `make ci (check, self-test, quality, org)` does not exist (`Makefile:47` — `ci: check self-test test quality org`, one target, no partial invocation) | Honesty, not code: relabel to `failed (environmental: lock)` + drop fabricated command label; completion validator (`check-completion-report.py` only checks enum + reason presence) is too weak to enforce §2 | Spec conformance is manual; validator change is out of scope but must be noted |
| B3a | Medium | Walk-up boundary (≤6) has no boundary test; a depth-7 manifest silently skips (verified exit 0) — correct per design but unproven; nearest-level precedence only tested at same level, cross-level "nearest wins" untested | `test_walkup_boundary_6_vs_7` (manifest at level 6 → found; level 7 → skipped); `test_precedence_nearest_level_wins` | Scratch dir tree, go.mod at exactly 6 and 7 parents |
| B3b | Medium | Unreadable dir/subdir: `scan_directory` uses bare `os.walk` (errors silently skipped) — semantics undefined; a root manifest + unreadable-only subdir → misleading "0 backend files scanned" | `test_unreadable_subdir_reports_scan_error` | `chmod 000` subdir with the only `.go` file; assert stderr distinguishes "unreadable" from "0 files" |
| B3c | Medium | `--manifest` as last arg silently misparses into a positional target `--manifest` → `missing target` violation, exit 1 (fail-closed but wrong message, no arg-error path) — QA round F5 guard never landed as a real error | `test_manifest_flag_without_value` | `_run("--dir", repo, "--manifest")` → expect explicit `missing value for --manifest`, exit 2 |
| B3d | Medium | Caller contract untested: `advance.py:97 _run_checker` does plain `json.loads(stdout)` — verified live: works on violation stdout (pure JSON) but **breaks on the OK/skipped trailing line** (pre-existing for OK; new `skipped` line extends it) → advance's be dimension silently empty on clean/skip; no test covers advance/health/org consuming the new output (F4 unresolved) | `test_advance_consumes_skipped_and_clean_stdout` | `advance.scan_project()` on a clean Go repo → `be_errors` count 0, not empty-dict fallback; assert stdout channel contract |
| B4 | Medium | Stray-ancestor-manifest hazard **dodged, not fixed**: `/tmp/go.mod` (`module sqltest`) lives on this box — I reproduced `--dir /tmp/<scratch>` → false FAIL "manifest go.mod found but 0 backend files scanned"; the suite passes only because `clean_repo` fixture relocates scratch dirs under the repo root; the implementer's "bug found and fixed" is a test relocation | `test_ancestor_stray_manifest_does_not_false_close` (needs a control: report *which* manifest level closed the gate, or ignore manifests above repo boundary) | Assert stderr names the manifest path; currently it cannot distinguish a 2-level stray from a real repo manifest |
| B5 | Medium | F6 conflict resolved in the worst direction: whole `docs/backend/` gitignored — verified `git check-ignore` matches both `docs/backend/backend-quality.json` **and** `docs/backend/inventory.md`; example pipelines (`backend-engineering-review.yaml:30-74`, `backend-implementation-pipeline.yaml:45-92`) write deliverables there with `git_commit: true` → deliverables silently uncommitted | `test_example_pipeline_outputs_not_gitignored` (or narrow the ignore to the report file) | `git check-ignore docs/backend/inventory.md` must exit 1 |
| B6 | Low | `_scrub_string_literals` DDL-behavior change rides in this diff with **zero regression tests** (no `DROP TABLE` test at HEAD or now); completion report doesn't mention it; attribution murky under H-4 (47 modified files) | `test_ddl_string_literal_not_flagged` / `test_ddl_real_ddl_still_flagged` | File containing `x = "DROP TABLE users"` → pass; real `DROP TABLE` → fail |
| B7 | Low | "135 targeted tests passed" vs the documented command (`pytest test_backend_specs.py test_validator_registry.py test_context_eval.py test_capabilities.py`) = **85 passed**; 135 is unsupported | — | — |
| — | Info | `residual_risks: []` while B4/B5/F3/F7 are known and verified; F3 (org gates `--dir {code}` lose enforcement on manifest-less Python/SQL repos — was union-scanned before) acknowledged in assumptions but never surfaced in the example yaml, which now overpromises "自动定位后端源码根" | `test_org_gate_python_repo_behavior` documenting skip semantics | — |

## Risk-coverage matrix

| Risky path (mapped) | Covered? | Evidence |
|---|---|---|
| Missing dir / empty dir (partial success) | ✅ | AC-1/AC-2 tests + live repros (exit 1, exact messages) |
| No-manifest skip signal | ⚠️ | Tested, but fixture dodges live `/tmp/go.mod` hazard (B4) |
| Language pinning (4 manifests) | ✅ | go/rust/java/node tests, hybrid-repo assertions |
| Manifest + 0 files (partial scan) | ✅ | AC-2 test + live repro |
| Boundary (walk-up 6 vs 7) | ❌ | Untested (B3a) |
| Precedence (same-level ✓ / nearest-level ✗) | ⚠️ | Same-level only (B3a) |
| Permissions change | ❌ | Untested; `os.walk` swallows errors (B3b) |
| Duplicate/concurrent invocation | ⚠️ | Out of scope (pi-batch lock); harness `timeout=120` ✓ |
| Timeout-uncertain | ⚠️ | Harness timeout ✓; no scale/perf test for huge repos |
| Duplicate submit / 409 / optimistic-rollback / timezone | N/A | Stateless single-process CLI; no state to corrupt |
| Argument errors (`--manifest` dangling) | ❌ | Misparses to `missing target` (B3c) |
| Report `-o` on scan_error/skipped paths | ❌ | Only `--json` path tested; `-o` file content untested |
| Caller contract (health/advance/org) | ❌ | health OK (verified live, raw_decode); advance breaks on clean/skip (B3d); org skip semantics untested |

## Honesty audit (completion-evidence §2)

| Claimed | Verdict | My run |
|---|---|---|
| `pytest <4 files>` passed | **Partial** | ✅ 85 passed — but "135 targeted" in narrative ≠ 85 (B7) |
| `python cli.py check` passed | ✅ Verified | exit 0 |
| `python quality.py --strict .` passed | ✅ Verified | OK on script and repo (devops's earlier "2 violations" was against a prior file state) |
| `make self-test` + `checks/` passed | ✅ Verified | checks/ 49 passed |
| `pi-batch health` passed | ✅ Verified | HEALTHY 100.0, backendquality 0 |
| `pi-batch check` passed | ✅ Verified | 9 gates passed |
| `make ci (check, self-test, quality, org)` passed | ❌ **Fabricated label** — no such command exists (`ci:` is atomic); sub-targets individually verified real, but the label is not a command that ran (B2) |
| `make ci (test phase)` not_executed | ❌ **Misclassified** — it ran and failed (21 lock-caused failures, confirmed by devops's independent run); per §2 must be `failed` (environmental), not `not_executed`; the reason text itself proves execution (B2) |
| "Two bugs found and fixed" | ⚠️ | Relative-dir bug: real fix + test ✓; `/tmp/go.mod`: a test relocation, not a fix (B4, live-reproduced) |
| `residual_risks: []` | ❌ | B4/B5/F3/F7 known and verified; unlisted (B2) |

**Bottom line**: the gate is functionally correct on its main contracts and the headline regression tests are strong and assertion-rich. Blocking before merge: (1) F2 regression test, (2) honest reclassification of the completion report + real residual risks, (3) tests for walk-up boundary, unreadable dirs, dangling `--manifest`, and the advance.py/org-gate consumer contract — plus a decision on the `docs/backend/` ignore collision. All are bounded; none invalidates the design.
