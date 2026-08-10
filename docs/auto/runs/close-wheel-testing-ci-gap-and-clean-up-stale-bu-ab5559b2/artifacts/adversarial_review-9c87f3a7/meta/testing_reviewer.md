All verification complete. Here is my QA review.

---

# QA Lead Review — "close wheel testing CI gap and clean up stale build artifacts"

**Scope note:** This review targets the design-stage deliverable (implement stage not yet run; repo state = design as validated: wheel 0.47.0/build 1.5.0 installed in dev env, `build/` already removed, `ai_batch_runner.egg-info/` + `tests/test_packaging.py` still untracked). All evidence claims were independently re-executed where possible.

## 1. Verdict line

**VERDICT: FAIL - the wheel test's silent-skip defect can return: `pytest.importorskip("wheel")` (tests/test_packaging.py:214) still skips with green `make ci` if `wheel` ever leaves dev extras (pip ignores unknown/missing extras with exit 0); the design's "AC-1 guard" is an observation at implementation time, not a persistent fail-closed test — the fix must prove non-recurrence (backend-specs/testing.md §3), and it currently cannot.**

## 2. Findings table

| Sev | Defect pattern | Missing test | Exact test case that would catch it |
|---|---|---|---|
| **High (blocking)** | Silent-skip recurrence (the very defect this direction exists to close). Design explicitly chose "no test-code changes needed"; AC-1 ("fresh-env 7/0, make ci no-skip") is a one-time observation, not a regression guard. If `wheel` is removed from `dev` extras, or the extras section is renamed/typo'd, `pip install -e ".[dev]"` warns (exit 0) and `importorskip` silently skips — `make ci` stays green | A fail-closed wheel-presence guard | Replace line 214 with a hard `import wheel` (message: "wheel dev extra missing — pip install -e '.[dev]'"), or add a static test mirroring `test_package_data_declaration_covers_loader_refs`: parse `[project.optional-dependencies] dev` and assert `"wheel>=0.42"` is declared. Deterministic, no timing control needed. Run `make ci` in an env without wheel: currently 6 passed/1 skipped + green; with the guard → FAIL with an actionable message |
| Medium | Dependency not load-bearing: `build>=1.0` added to dev extras, but nothing in tests/, Makefile, .github/, scripts/, or pbatch/ invokes `python -m build` or imports `build` | — | **Already empirically disproven by my experiment**: fresh venv with only `pytest setuptools wheel` (no `build`) → `7 passed in 0.65s`. Either justify `build` as release tooling or drop it from the diff; if kept, scope guardrail "no expansion beyond the direction" is stretched |
| Low | No automated install-and-import E2E for the built artifacts — wheel test checks zip *contents*, sdist tests stop at tar members; `pip install .` + `pi-batch classify` smoke is manual AC-4 only | Install-and-import test | Subprocess: `pip install --no-deps --target <tmp> <wheel>` then `python -c "import pbatch; from pbatch import relevance"` and assert `relevance._load_keywords()` resolves `role_keywords.yaml`. Non-blocking (runtime deps untouched, risk small) |
| Info | Regression suite the fix depends on is itself **untracked**: `tests/test_packaging.py` (`??`) — if the branch were pushed before implement-stage commit, CI would run zero packaging tests | — | `git ls-files tests/test_packaging.py` must return the file after implementation; commit includes both manifest edits and the suite |
| Info | Stale cosmetic claim in pyproject.toml:16 (`"regression suite in tests/ (222 cases)"` — suite is now 387) | — | Out of direction scope; noted for the maintainers |

## 3. Risk-coverage matrix (packaging/CI change — concurrency items N/A)

| Risky path | Covered? | Evidence |
|---|---|---|
| Wheel test silent-skip in CI (the defect) | ✗ **not fail-closed** | `importorskip` at :214; guard only observational (F1) |
| Dep floor not propagating (unknown/missing extras, pip exit 0) | ✗ | No test asserts `wheel` in dev extras (part of F1) |
| Data-file manifest drift (loader `with_name` refs vs package-data) | ✅ | `test_package_data_declaration_covers_loader_refs` (static) + `test_loader_referenced_data_files_ship` (artifact) — both fail closed |
| Live-tree leak into artifacts (stale egg-info SOURCES.txt, tests/) | ✅ | `test_sdist_isolated_from_live_tree_state` + all builds from minimal tmp_path copy, subprocess `cwd=copy` |
| Wrong package-data key (underscore typo) | ✅ | `test_wrong_package_data_key_fails_closed` (fails closed on empty result) |
| Content drift (shipped ≠ repo) | ✅ | `test_sdist_data_files_byte_identical_to_repo` |
| Wheel artifact divergence from sdist | ⚠️ | `test_wheel_ships_data_files` asserts exact member names — but only runs when wheel present (F1) |
| Unanchored gitignore over-ignore | ⚠️ | Design anchors `/build/`; verified no tracked `build/` dirs exist (I checked `git ls-files`); covered by manual AC only, no automated test (Low) |
| `build/` dir shadowing installed `build` package | ✅ | Verified-non-issue by experiment, documented (same precedence on CI 3.11) |
| Fresh-venv install + console-script E2E | ⚠️ | Manual AC-4 only (F3) |
| Races/duplicates/timeouts/409/permissions/unmount/rollback/pagination/tz/partial-success | N/A | No such logic in scope — change is 2 manifest edits + 2 deletions |

## 4. Honesty audit (completion-evidence)

**Re-executed and confirmed by me this session:**

| Claimed | My result |
|---|---|
| Test at 207–229, `importorskip("wheel")` at 214 | ✅ exact (line 214) |
| Docstring at 27–29 | ✅ exact ("The wheel test skips when the `wheel` package is absent..." at 27–29) |
| dev extras = pytest/pytest-cov only; `Makefile:37` `test:`, `ci:` at 47 | ✅ exact |
| `.gitignore` has `*.egg-info/` + `dist/`, no `build/`; `check-ignore` rc=1 | ✅ exact |
| SOURCES.txt 168 lines, 55 `tests/` entries; egg-info untracked | ✅ exact |
| CI = `pip install -e ".[dev]"` → `make test` (zero workflow changes) | ✅ exact (ci.yml) |
| Fix validated: after `wheel>=0.42 build>=1.0` → **7 passed, 0 skipped, no test-code changes** | ✅ **reproduced**: 7 passed in 0.76s with wheel 0.47.0/build 1.5.0; my wheel-only venv also 7 passed (F2) |

**Chain-consistent, not re-verifiable now** (env mutated by the validation installs; `build/` deleted): the pre-fix "6 passed/1 skipped, `import wheel` → ModuleNotFoundError, `?? build/`" state. Documented in the requirements artifact with matching error output; the skip mechanism is visible in code. **No fabrication indicators**: no "理论上应该通过" phrasing anywhere; the shadowing hypothesis is explicitly labeled verified-non-issue with its experiment described; manual steps (AC-2/AC-4) are labeled manual; the only deferred verification (post-merge CI observation) is a genuine residual — the implement-stage completion report must list it as `not_executed` until the CI run is actually observed.

**Test isolation**: exemplary — tmp_path minimal copies, module-scoped single sdist build, subprocess `cwd=copy` isolation, no sleeps, no shared mutable state, deterministic order-independence. Reproduce-first: satisfied at design level (defect reproduced before fix, fix validated empirically). Assertion quality: all 7 tests assert business results (exact member names, byte-identical content, fail-closed discrimination), never presence-only.

**Bottom line:** with one one-line guard change at `tests/test_packaging.py:214` (hard `import wheel` or a static dev-extras assertion), this becomes a PASS — everything else verified green.

VERDICT: FAIL - blocking test gap: wheel-presence guard is observational only; `importorskip("wheel")` lets the silent-skip defect return with green CI (backend-specs/testing.md §3 non-recurrence proof unmet) — add a hard wheel import or a static dev-extras assertion; non-blocking: drop or justify `build>=1.0` (proven unnecessary by experiment), and commit the untracked `tests/test_packaging.py` with the fix.
