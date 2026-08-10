All verification complete. The implementation was committed (`7280ec0`) during the review, so this report covers the **implemented, committed state** — which I re-verified live.

---

# QA Lead Review — `checks` scan-scope exclusion (d4b833b0)

**Scope note:** the design I was asked to review was implemented and committed (`7280ec0`) mid-review. I reviewed the committed implementation against the design, not just the plan.

## 1. Test inventory and commands actually run (this revision)

| Command | Result |
|---|---|
| `python cli.py check` | exit 0; **0** `build/`/`dist/` paths in output; exactly **17 WARN lines** — same set as pre-change baseline (byte-identical) |
| `python -m pytest checks/ -q` | **41 passed** (37 baseline + 4 new AC1/AC2) |
| `python cli.py check-test` (unittest discover) | exit 0 |
| `python quality.py --strict .` | exit 0 |
| `make ci` | **FAIL — 19 failures** in `tests/test_pi_batch.py` + `tests/test_local_fixes.py` (see §3, finding F-0) |
| `scan_files(repo_root())` (live) | **189** paths, **0** under any excluded segment, sorted |
| `grep -n rglob checks/{filesize,complexity,style}.py` | **0** remaining rglob call sites |
| R2 semantics prototype (temp tree) | `subproject/build/x.py` excluded; `src/build-tools/x.py`, `mylogs/x.py`, root file `build.py` kept; `vendor/pkg.egg-info/deep/x.py` excluded at depth; `Build/` (case variant) kept — all as specified |
| Substring-accident | `is_ignored("build/lib/pbatch/pipeline.py", ["pbatch/pipeline.py"])` → True (baseline accident confirmed) |
| `git ls-files -s \| grep ^120000` | 0 symlinked dirs (FM2 verified moot) |
| `git check-ignore build` | exit 1 → `build/` **not** gitignored (requirements finding confirmed) |
| Pristine-HEAD worktree | `test_cli_validate_named_gofmt` **passes** at HEAD → make-ci failures are tree-dirtiness, not this direction |

## 2. Requirement-to-test matrix

| Req | Test / evidence | Status |
|---|---|---|
| R1 scan scope | `scan_files()` helper + 3 wired gates; AC2 tests ×3 (`test_run_scan_excludes_generated_dirs` in filesize/complexity/style) asserting `run()==0` and `scan_files(tmp)==[src/ok.py]`; live inventory 189, zero leak | **Verified** |
| R2 determinism/ordering + segment semantics | `sorted()` implementation; AC2 asserts singleton list only. Negative semantics (`build-tools`, `mylogs`, depth, case-variant) prototype-validated (and by the campaign evidence) but **not pinned by committed tests** | **Partial** |
| R3 compatibility | 37 pre-existing tests unmodified + `test_run_counts_failures` (explicit list); WARN output byte-identity (17/17) verified; explicit-list-under-excluded-dir behavior documented but **untested** | **Partial** |
| R4 regression tests | AC1 `test_build_dir_excluded_from_run` (600-line `build/` ⇒ 0; `src/` ⇒ 1, negative control) + AC2 ×3; hermetic (temp dirs, module-namespace patch, `try/finally` restore); run under pytest and unittest discover | **Verified** |
| R5 optional doc | `engineering.yaml` header comment added in commit (M6 applied) | **Done** (optional, non-blocking) |
| AC3 repo gates green | `cli.py check` ✓, `pytest checks/` 41 ✓, `check-test` ✓, `quality --strict` ✓; `make ci` ✗ for pre-existing unrelated reasons (F-0) | **Partial** |

## 3. Findings (sorted by severity)

**F-0 — High (environment, not this direction): `make ci` is red in the current tree.** 19 failures, all in `tests/test_pi_batch.py` / `tests/test_local_fixes.py` — zero `checks.` imports, files unmodified vs HEAD, and `test_cli_validate_named_gofmt` **passes in a pristine HEAD worktree**. Cause: live-campaign lock contention / other dirty `pbatch/` modifications (872+/1182− lines in 21 files), not this commit. Impact: AC3's "make ci green" claim is currently unverifiable in-tree. Action: re-run `make ci` after the other working-tree changes settle; the commit message's attribution ("verified failing on baseline with changes stashed") is consistent with my measurement. **Exit criterion for release, not a blocker for this direction.**

**F-1 — Medium: R2 false-exclusion semantics have zero committed test coverage.** The AC2 fixture contains only the six excluded kinds + `src/ok.py`. The exact bug class this direction fixes — *substring* matching wrongly excluding `src/build-tools/x.py` / `mylogs/x.py`, or missing depth pruning — would pass all 41 tests. Test to add (in `checks/test_filesize.py`, mirrors AC2):
```python
def test_scan_segment_matching_not_substring(self):
    tmp = Path(tempfile.mkdtemp())
    for rel in ("src/build-tools/x.py", "mylogs/x.py", "subproject/build/x.py",
                "Build/x.py", "build.py", "a/x.py", "b/x.py"):
        p = tmp / rel; p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n", encoding="utf-8")
    got = sorted(str(p.relative_to(tmp)) for p in filesize.scan_files(tmp))
    self.assertEqual(got, ["Build/x.py", "a/x.py", "b/x.py", "build.py",
                           "mylogs/x.py", "src/build-tools/x.py"])
```
Acceptance assertion: `subproject/build/x.py` absent; `src/build-tools/x.py`, `mylogs/x.py`, root `build.py`, `Build/` present (exact set above).

**F-2 — Medium: R3 explicit-list-under-excluded-dir behavior untested.** Documented ("explicit lists unchanged, including for paths under excluded dirs") but nothing pins it — a future scope-tightening of explicit lists sails through CI. Test:
```python
def test_explicit_files_under_excluded_dir_unchanged(self):
    tmp = Path(tempfile.mkdtemp())
    big = tmp / "build" / "big.py"; big.parent.mkdir()
    big.write_text("".join(f"# {i}\n" for i in range(600)), encoding="utf-8")
    orig_root, orig_cfg = filesize.repo_root, filesize.get_config
    try:
        filesize.repo_root = lambda: tmp
        filesize.get_config = lambda: Config(filesize=FilesizeGate(max_lines=500))
        self.assertEqual(filesize.run([big]), 1)      # explicit list NOT filtered
    finally:
        filesize.repo_root, filesize.get_config = orig_root, orig_cfg
```
Acceptance assertion: `run([build/big.py]) == 1` (exclusion applies to discovery only).

**F-3 — Low: no multi-file ordering assertion.** AC2 asserts a singleton `[src/ok.py]`; R2's byte-identical-ordering compatibility constraint is untested. Add a fixture with `a/x.py`, `b/y.py`, `c/z.py` interleaved with an excluded dir; assert exact sorted order.

**F-4 — Low: design §2.3 mislocation.** `is_exempt` lives in `checks/filesize.py`, not `checks/config.py` (verified: `is_exempt.__module__ == "checks.filesize"`; complexity/style don't use it). Doc-only inaccuracy; no behavioral impact. Fix the design doc if it remains canonical.

**F-5 — Low: `scan_files` parameter/edge coverage missing.** No committed test for `suffix=`, empty root, or nonexistent root (behavior: empty list — same as today's rglob). One `TestScanFiles` class in a new `checks/test_config.py` would close this and F-3 together.

**F-6 — Info: FM1 tripwire is one-time.** M4's 189-count pin is a migration-step command, not CI; a future authored `dist/`-named dir goes silently green. Acceptable (AC2 pins the mechanism durably; a permanent count test would be brittle to normal repo growth). Note in the R5 comment.

**F-7 — Info: commit scope hygiene.** `7280ec0` sweeps in one unrelated `pytest.ini` allowed_files entry in `engineering.yaml` (pre-existing dirty-tree change; harmless — `pytest.ini` is a real root file). Also, M2's "output must contain no build/dist path" is not a regression tripwire: baseline output already had zero such paths (verified); the real tripwire is the 189 inventory.

**Positives verified:** tests use `try/finally` restoration (stronger than the design's plan); fixture files use `"x = 1\n"` (final newline — avoids the style-gate's missing-final-newline trap, which I confirmed is a hard failure); hermeticity is real (temp dirs, gate-module namespace patch, no real-repo or `build/` dependence, no order coupling); AC1's negative control correctly distinguishes exclusion from a broken budget.

## 4. Prioritized scenario list

| # | Path | Scenario | Covered? |
|---|---|---|---|
| 1 | Happy | Authored tree (189 files) scans identically; gates green; WARN output byte-identical | ✅ AC3 (live) |
| 2 | Happy | Explicit `files` list bypasses discovery; failures counted | ✅ `test_run_counts_failures` |
| 3 | Boundary | 500/501-line budget edge | ✅ existing tests |
| 4 | Boundary | Excluded dir at depth (`subproject/build/`, `vendor/pkg.egg-info/`) | ✅ AC2 fixture (depth for egg-info; `build` only at root) — **extend per F-1** |
| 5 | Boundary | Segment-vs-substring adjacency (`build-tools`, `mylogs`), root file `build.py`, case variant `Build/` | ❌ **F-1** (prototype only) |
| 6 | Boundary | Explicit list containing excluded-dir paths | ❌ **F-2** |
| 7 | Boundary | Multi-file sorted ordering | ❌ **F-3** |
| 8 | Error | Nonexistent root / empty root / `suffix=` non-default | ❌ **F-5** (behavior identical to today — low risk) |
| 9 | Race | Concurrent `build/` regeneration during scan | ✅ by design (read-only walk; same exposure as rglob; pruning only shrinks the window) |
| 10 | Recovery | M4 inventory re-run after tree growth; symlink tightening (FM2) | ✅ verified 0 tracked symlinks; walk `followlinks=False` |
| 11 | Security | Symlinked dir escaping repo | ✅ tightened (FM2); root resolved (FM8) |

## 5. CI/manual-suite gaps, flake risk, exit criteria

- **CI gap:** `make ci` red from unrelated dirty-tree changes (F-0). Direction-scoped gates all green. `make ci` green is the release exit criterion, not a direction defect.
- **Coverage gaps:** F-1 (R2 negative controls), F-2 (R3 explicit-list pin), F-3/F-5 (helper unit class). All are additive; none block the direction's acceptance.
- **Flake risk:** none identified — no timing, network, shared state, or real-repo dependence in the 4 new tests; unique tempdirs; module attrs restored in `finally`.
- **Fixtures needed:** none beyond current temp-dir fixtures; if F-1/F-3 land, a shared `_make_scan_tree()` helper in each test module would reduce duplication.
- **Exit criteria:** (a) F-1/F-2 tests added and passing; (b) `make ci` re-verified green once the tree settles; (c) optional F-4 doc fix, M7 `.gitignore` follow-up for `build/` remains out of scope as designed.

**Overall verdict:** the implementation is faithful to the design — helper semantics, wiring, test hermeticity, and all direction-scoped gates verified live (41 passed, `check`/`check-test`/`quality --strict` exit 0, inventory 189 with zero leaks, byte-identical WARN output). No Critical/High defects in the change itself. Two Medium gaps (F-1, F-2) are regression-pinning tests for semantics the requirements explicitly call out; recommend landing them with the direction or immediately after.
