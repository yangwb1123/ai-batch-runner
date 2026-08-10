All verification complete. Writing the gate verdict.

---

# Gate Review — `checks` scan-scope exclusion (d4b833b0)

I re-verified every material claim live against the repo. **Timeline-critical observation**: commit `7280ec0` (03:00:41 UTC) contains 41 tests; at 03:03:56 UTC — mid-review, from a parallel process — `checks/test_config.py` (7 tests: F-1 negative semantics, F-3 ordering, F-5 edges) and the F-2 explicit-list test in `test_filesize.py` landed **uncommitted** in the working tree. My first live run saw 41 passed; the suite is now **49 passed**. I reviewed and ran both states.

## 1. VERDICT

```
VERDICT: PASS - all direction-scoped gates verified live (pytest checks/ 49 passed,
cli.py check exit 0 with 0 build/dist paths + 17 WARN, check-test exit 0, quality.py
--strict exit 0); R2 segment semantics prototype-verified exact; scan_files(repo_root())
= 189 files, 0 leaks, sorted, absolute; 0 tracked symlinks; os.walk followlinks=False
prune-before-descent confirmed in code. QA-lead F-1/F-2/F-3/F-5 gaps now closed in-tree
with non-vacuous, hermetic tests. make ci red (19/953) is verified-environmental: all
failures are .pi-batch.lock contention in tests/test_pi_batch.py + test_local_fixes.py,
lock held by pid 1684832 = this campaign itself; zero checks.* involvement.
CONDITION: the 8 uncommitted test additions (test_config.py + F-2 test) must be
committed with the direction or the PASS is void; make ci must be re-run green once
the live campaign releases the lock (pre-release exit criterion, not a direction defect).
```

## 2. Findings table

| # | Sev | Defect pattern | Missing test → exact case that catches it | Status |
|---|---|---|---|---|
| F-1 | Medium→**Closed** | Regression to substring/`startswith`/case-insensitive matching (the exact historical bug this change fixes — `build/lib/pbatch/pipeline.py` matched by substring accident) passes all 41 committed tests | `TestScanFiles.test_segment_matching_not_substring` (now in `checks/test_config.py`): tree with `src/build-tools/x.py`, `mylogs/x.py`, `subproject/build/x.py`, `Build/x.py`, `build.py`, `a/x.py`, `b/x.py`; assert exact `["Build/x.py","a/x.py","b/x.py","build.py","mylogs/x.py","src/build-tools/x.py"]` — substring impl excludes `mylogs/x.py`+`build-tools`, startswith impl loses `build.py`, case-insensitive loses `Build/` | ✅ in tree, 49/49 pass; **uncommitted** |
| F-2 | Medium→**Closed** | Future scope-tightening of explicit `files` lists silently filters excluded-dir paths (R3 compat contract) | `test_explicit_files_under_excluded_dir_unchanged` (`checks/test_filesize.py`): 600-line `build/big.py` passed explicitly; `run([big])` must `== 1` — filtering would make it 0 | ✅ in tree (uncommitted diff vs 7280ec0) |
| F-3 | Low→**Closed** | Ordering regression (rglob byte-identity contract) untested | `test_sorted_ordering_across_dirs`: `b/y.py,a/x.py,dist/z.py,c/w.py,c/z.py` → exact `["a/x.py","b/y.py","c/w.py","c/z.py"]` | ✅ in tree |
| F-5 | Low→**Closed** | Helper param/edge coverage (`suffix=`, empty/nonexistent root) | `test_suffix_parameter`, `test_empty_root_returns_empty`, `test_nonexistent_root_returns_empty` | ✅ in tree |
| F-0 | High (env) | `make ci` red — 19 lock-contention failures | Reproduce: `make ci` → assert all 19 in `test_pi_batch.py`/`test_local_fixes.py` with `Another pi-batch instance is running (pid=1684832…)`; none import `checks.*` | Confirmed live; pre-existing, not a direction defect |
| F-4 | Low | Design §2.3 mislocation: `is_exempt` lives in `checks/filesize.py`, not `config.py` | Doc-only; no test | Open (doc-only, non-blocking) |
| NEW | Low | Symlink-escape (FM2) pinned by code inspection + 0-tracked-symlink inventory only; no committed fixture creates a real symlink | `scan_files` over a root containing `link -> ../outside/` must not descend (assert `outside/x.py` absent) | Open — additive, low risk (`followlinks=False` verified in code) |
| NEW | Info | 8 new tests uncommitted (mtime 03:03:56, post-commit 03:00:41) — loss risk | `git diff 7280ec0 --stat checks/` shows untracked `test_config.py` + modified `test_filesize.py` | **PASS condition** |

## 3. Risk-coverage matrix

| Risky path | Covered? | Evidence |
|---|---|---|
| Substring false-exclusion (`build-tools`, `mylogs`, root `build.py`) — the bug class that motivated the change | ✅ now | F-1 test (exact-set assert); live prototype matched |
| Excluded dir at depth (`subproject/build/`, `vendor/pkg.egg-info/deep/`) | ✅ | AC2 fixtures + `test_excluded_segments_pruned_at_any_depth` |
| Case-variant evasion (`Build/`) | ✅ now | F-1 test includes `Build/x.py` kept |
| Explicit-list bypass (R3 compat) | ✅ now | F-2 test |
| Sorted deterministic ordering (rglob byte-identity) | ✅ now | F-3 + `test_absolute_and_deterministic`; live 189-file inventory sorted/absolute |
| Empty / nonexistent root / `suffix=` | ✅ now | F-5 ×3 |
| 500/501-line budget edge, ignore/exemption patterns | ✅ | pre-existing suite (8 tests) |
| Symlinked dir escaping repo | ⚠️ partial | `followlinks=False` in code; 0 tracked symlinks live; no committed symlink fixture (NEW Low) |
| Concurrent `build/` regeneration during scan | ✅ by design | read-only walk; pruning strictly shrinks exposure vs rglob; no shared state |
| Permission-unreadable dirs | ⚠️ parity | `os.walk` default `onerror=None` skips — identical to prior rglob exposure; no committed test (low risk) |
| Async reorder / duplicate submit / 409 / timezone / partial success / pagination | N/A | change is a stateless pure filesystem walker — no concurrency, network, DB, or time semantics; no such surfaces exist to test |
| Five layers | N/A-appropriate | unit (helper) + wiring-level (`run()` AC1/AC2) is the correct layer for a discovery function; contract/property/fuzz tests have no applicable surface (deterministic enumeration → exact equality is stronger than property tests) |

## 4. Honesty audit

| Claim | Checked | Result |
|---|---|---|
| backend_engineer: commit `7280ec0`, 8 files | `git show --stat` | ✅ exact |
| 41 passed (37 baseline + 4 new) | live at review time | ✅ (later 49 with parallel additions) |
| `cli.py check` exit 0, 0 build/dist, 17 WARN | live | ✅ |
| `check-test` exit 0 | live | ✅ |
| `quality.py --strict` exit 0 | live | ✅ |
| inventory 189 / 0 leaks / sorted / absolute | live | ✅ |
| 0 symlinked tracked dirs | `git ls-files -s \| grep -c ^120000` | ✅ 0 |
| `build/` not gitignored (M7 out of scope, recorded) | `git check-ignore build` exit 1 | ✅ |
| R2 semantics (build-tools/mylogs kept, depth pruned) | live prototype | ✅ |
| `make ci` fail → "could not run / 19 pre-existing environmental failures, reproduced on baseline" | live | ✅ 19/953, all lock contention (pid 1684832 = live campaign), 0 `checks.*` involvement; honest disclosure, no "理论上应该通过" phrasing |
| qa_lead: F-1/F-2/F-3/F-5 gaps at 41-test state | live | ✅ accurate as of 03:00-03:03 UTC; gaps since closed in-tree |

**Honesty gaps found**: none material. No fabricated command results, no skipped-to-pass tests, no skipped tests at all (1 skip in full `tests/` run is pre-existing), no sleeps, no shared-state/order-dependent fixtures, no vacuous assertions. One process note: the 8 follow-up tests are uncommitted — recorded as a PASS condition rather than a defect, since they are verified present, correct, and green in the current tree.
