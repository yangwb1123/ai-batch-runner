# Handoff Report

## 1. Outcome and behavioral changes

The implementation for **"exclude generated/artifact directories from gate scan scope"** was already committed (`7280ec0`). This change lands the QA follow-up regression tests (`de8c1b2`), closing findings **F-1, F-2, F-3, F-5**; F-4 (design §2.3 `is_exempt` mislocation) has no actionable target — the on-disk design artifact is a 12-line summary with no such claim, so nothing canonical to fix. **No production-code behavior changes** — `scan_files()` semantics (exact-segment matching, pre-descent pruning, sorted absolute paths, discovery-only exclusion) are now durably pinned by tests so the exact bug class (substring false-exclusion of `src/build-tools/`/`mylogs/`, missing depth pruning, explicit-list scope drift) fails CI.

## 2. Modified files

| File | Purpose |
|---|---|
| `checks/test_config.py` (new) | `TestScanFiles` unit suite for the shared helper: R2 exact-segment vs substring semantics with negative controls (`Build/` case variant, root `build.py`, `src/build-tools/x.py`, `mylogs/x.py` kept; `subproject/build/x.py` pruned at depth), depth pruning incl. `*.egg-info/` suffix, byte-wise sorted ordering (F-3), absolute+deterministic results, `suffix=` parameter, empty/nonexistent-root edge cases (F-5) |
| `checks/test_filesize.py` | `test_explicit_files_under_excluded_dir_unchanged` (F-2): explicit `files` list containing `build/big.py` is still checked (`run()==1`), pinning R3 "exclusion applies to discovery only" |

## 3. Tests and checks actually run

| Check | Result |
|---|---|
| `python -m pytest checks/` | ✅ **49 passed** (41 + 8 new) |
| `python cli.py check-test` (unittest discover) | ✅ exit 0 |
| `python cli.py check` | ✅ exit 0; **no findings** in `checks/test_config.py`/`test_filesize.py` (style gate clean) |
| `python quality.py --strict .` | ✅ exit 0 |
| `make ci` | ⚠️ 19 failed, **all pre-existing and unrelated** (F-0): `tests/test_pi_batch.py` + `tests/test_local_fixes.py` lock-contention subprocess tests. Live campaigns (incl. `/home/u1/iris-ui`, pid 204258) hold `.pi-batch.lock` right now; zero `checks.` imports in the failing set — matches QA's baseline reproduction exactly |

## 4. Remaining limitations, risks, unverified assumptions

- **`make ci` green remains a release exit criterion, unverifiable in-tree** until the live campaigns release `.pi-batch.lock` (F-0). Not attributable to this change.
- **F-1/F-3 tests live at helper level** (`test_config.py`) rather than duplicated per gate suite, per QA's own consolidation suggestion — the gate-level AC2 fixtures already assert `scan_files(tmp) == [src/ok.py]` in all three suites, so semantics are pinned once and wiring is pinned three times.
- **F-6 tripwire is one-time** (189-count inventory was a migration-step command, not CI) — accepted as-is per QA; AC2 durably pins the mechanism.
- **F-7 commit hygiene** (unrelated dirty-tree changes swept into `7280ec0`'s `engineering.yaml` diff) and **M7 `.gitignore` for `build/`** remain out of scope per design, recorded as follow-up.
- Assumption: `scan_files` on nonexistent root returning `[]` (same as today's `rglob`) is the intended contract — now pinned by `test_nonexistent_root_returns_empty`.
