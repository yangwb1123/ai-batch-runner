All measurements complete. I have a full picture of the packaging test net, CI wiring, and the current measured state. Here is my QA Lead review.

---

# QA Lead Review — close-wheel-testing-ci-gap-and-clean-up-stale-bu-ab5559b2

Scope: risk-based test review of the design for closing the CI gap around `test_wheel_ships_data_files` (silent skip) and cleaning up stale build artifacts. Advisory only; no files modified.

## 1. Test inventory and commands actually run (this revision)

**Commands run by me (all measured, current working tree):**

| Command | Result | Notes |
|---|---|---|
| `python -m pytest tests/test_packaging.py -v` (local env) | **7 passed** | wheel 0.47.0 + build 1.5.0 installed locally |
| Fresh venv (setuptools+pytest only, **no wheel**) → `python -m pytest tests/test_packaging.py` | **6 passed, 1 skipped** | `test_wheel_ships_data_files` SKIPPED — exact CI-like repro of the gap |
| `python -m pytest tests/ -q` (×2) | 21 failed/995 passed; then 23 failed | **Environment-confounded** — all failures trace to `.pi-batch.lock` held by the running campaign coordinator (`pid=1547473 … Refusing to start (exit 5)`); `test_max_rounds_loop_reruns_failures` (FileNotFoundError on counter file) is the same root cause (spawned pi-batch never ran). 1016 tests collected. Not attributable to this design (no code changes yet) but the suite must be re-run in a quiescent repo before merge. |
| `git check-ignore build/` | rc=1 (not ignored) | confirmed |
| `git status --porcelain` | 229 entries; `build/` absent; `ai_batch_runner.egg-info/` present (ignored, SOURCES.txt = 168 lines, 55 `tests/` entries) | confirmed |
| `grep with_name pbatch/*.py` | exactly `classifier.py:136`, `relevance.py:51` | loader-audit regex scope confirmed |
| Read `.github/workflows/ci.yml`, `Makefile:37-38,47`, `.gitignore`, `pyproject.toml` | see §2/§5 | |

**Suite taxonomy (per prompts/README.md "distinguish default CI from tagged/manual suites"):** There is **no separate chaos/load/benchmark/conformance gate**. `make test` = `python -m pytest tests/` with **no marker filter**, so the 2 `@pytest.mark.chaos` tests (test_hypergraph_fabric.py:200,239 — in-memory fabric kill-switch/heartbeat drills) run in default CI alongside everything else. Markers are organizational only (`pytest.ini`, enforced by `test_org.py::test_pytest_markers_filter`). The rule-system conformance suite is `make eval` (`python -m pbatch eval`, 25 cases) and `make coverage` adds `checks/`. Chaos/load/benchmark suites beyond these do not exist in this repo — nothing to distinguish.

## 2. Requirement-to-test matrix

Requirements = design AC-1..AC-4 + failure-mode guards F1/F3/F4/F7 (evidence: `task-1-design.md`).

| Req | Test / check | Status | Evidence |
|---|---|---|---|
| AC-1 sdist ships 3 data files | `test_sdist_ships_declared_data_files`, `test_loader_referenced_data_files_ship`, `test_package_data_declaration_covers_loader_refs` | ✅ Verified (runs everywhere) | 7 passed locally; 6 passed in fresh venv |
| AC-1 **wheel** ships 3 data files | `test_wheel_ships_data_files` (line 207–229, `importorskip("wheel")` line 214 — refs confirmed) | ⚠️ **Currently skipped in CI** — the gap the design closes; fix not yet applied (`pyproject.toml` dev extras still pytest/pytest-cov only) | fresh venv: 1 skipped |
| AC-1 regression after fix | dev extras `wheel>=0.42`, `build>=1.0` → test unskips in CI with **zero workflow changes** | ✅ Verified (by reading ci.yml: `pip install -e ".[dev]"` → `make test`); ⚠️ floors not install-tested (local 3.10 only; CI is 3.11) — Partial | local env with wheel 0.47: 7 passed |
| AC-2 fresh-venv smoke (`cd /tmp`, install, import) | **manual only — no automated test** | ❌ Gap (see Finding 2) | grep of tests/: no wheel-install test exists |
| AC-3 byte-identical data | `test_sdist_data_files_byte_identical_to_repo` (sdist only); wheel test asserts member names only | ⚠️ Partial (sdist half verified; wheel half weaker) | — |
| AC-4 sdist script (manual) | automated proxy: 5 sdist tests incl. `test_sdist_isolated_from_live_tree_state` | ✅ Verified | — |
| F1 silent-skip recurrence | **no guard exists** — the "AC-1 guard" only fires when wheel is installed; removing `wheel` from dev extras returns CI to silent skip with zero failing tests | ❌ Gap (Finding 1, High) | fresh-venv measurement proves skip is silent |
| F3 egg-info/live-tree leak | `test_sdist_isolated_from_live_tree_state` (sdist side) + one-time deletion | ✅ Verified (sdist side); deletion is manual op | SOURCES.txt 168 lines/55 tests entries confirmed |
| F4 unanchored gitignore over-ignore | `/build/` anchoring — no test; manual `git check-ignore` | ⚠️ Manual only | rc=1 now confirmed |
| F7 shadowing | experiment-documented non-issue | ⚠️ Partial — experiment was on the now-deleted `build/` dir; result plausible (regular pkg precedes namespace portion) but not independently reproducible post-deletion | — |

## 3. Findings (by severity)

**1. High — F1's guard is itself unguarded: silent-skip recurrence is not actually prevented.**
The design's AC-1 guard is the wheel test, which skips exactly when `wheel` is absent — i.e., the moment someone removes `wheel` from dev extras, CI silently loses wheel coverage and **nothing fails** (fresh-venv measurement proves the skip is silent: `6 passed, 1 skipped` is a green exit).
*Untested failure:* dev-extras declaration drift.
*Regression risk:* the exact bug this campaign exists to fix returns undetected.
*Exact test to add:* in `tests/test_packaging.py`, a static, env-independent test:
```python
def test_dev_extras_declare_wheel_and_build():
    """F1 guard: CI unskips the wheel test only while dev extras declare wheel."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    dev = re.search(r"dev = \[(.*?)\]", text, re.S).group(1)
    assert "wheel" in dev, "wheel missing from dev extras - wheel test silently skips in CI"
    assert "build" in dev, "build missing from dev extras - AC-2/AC-4 manual steps fail"
```
*Acceptance assertion:* test passes with fix applied; fails (in all envs, incl. no-wheel) after `wheel` is deleted from dev extras.

**2. Medium — wheel is built and content-checked but never installed or imported (AC-2 manual-only).**
Content-in-wheel ≠ runnable install; F1/F3 class bugs (missing file, wrong key) currently get caught only at the sdist level or by a human following AC-2.
*Exact test to add:* extend the wheel test (or add `test_wheel_install_smoke`): build wheel → `pip install --no-deps --target tmp/wheeltgt` → subprocess with `PYTHONPATH=tmp/wheeltgt` running `python -c "from pbatch.relevance import role_keywords_files; from pbatch.classifier import task_keywords_files; ..."`.
*Acceptance assertion:* the subprocess exits 0 and all three `EXPECTED_DATA_FILES` resolve to existing files under the install target.

**3. Medium — regression suite is unsafe to run concurrently with a campaign (lock collision).**
21–23/1016 failures observed purely from `.pi-batch.lock` contention; any parallel CI job or dev running `make test` during a campaign gets green→red noise with unhelpful messages. Not a defect in this design, but a gate-hygiene gap: document "run suite in quiescent repo" and consider `--no-lock` in tests that spawn pi-batch, or a per-campaign lock scope.
*Re-run required:* full suite in quiescent repo before merge — current run is not evidence of regression, but also not evidence of a green tree.

**4. Low — stale suite-count comments.** `Makefile:37` "387 cases", `pyproject.toml:16` "222 cases"; measured: 1016 collected. Update both when touching these files (the dev-extras edit is the natural moment).

**5. Low — dead pytest config.** `pytest.ini` takes precedence; `[tool.pytest.ini_options]` in pyproject.toml is ignored with a warning on every run. Optional cleanup: delete one of the two.

**6. Info — CI matrix is single-Python (3.11).** `wheel>=0.42`/`build>=1.0` floors are unverified on 3.9/3.10/3.12; `requires-python >=3.9` claims compatibility. Recommend a quick `pip install 'wheel>=0.42'` on 3.9 in CI if a matrix is ever added; acceptable to defer.

## 4. Prioritized scenario list

1. **Happy:** fresh venv, `pip install -e ".[dev]"` → `make test` → 7/7 packaging pass (wheel unskipped) — the core acceptance.
2. **Happy:** `python -m build` (or `pip wheel .`) from repo root → `build/` recreated → `git status` clean (no `?? build/`, no egg-info) — F2/F3 regression.
3. **Boundary:** fresh venv without wheel → exactly 1 skip, suite green (documents the gap's failure shape for future triage).
4. **Boundary:** wrong package-data key (`ai_batch_runner`) — covered by `test_wrong_package_data_key_fails_closed`; extend to wheel half.
5. **Error:** wheel build failure → nonzero subprocess rc propagates as test failure with stderr (currently asserted; verify with a forced-bad pyproject in a follow-up scenario).
6. **Error:** a loader `with_name` ref added without package-data declaration → `test_loader_referenced_data_files_ship` fails closed (existing; the campaign's real regression net).
7. **Race:** concurrent `make test` + campaign (measured today) → lock-refusal failures; document quiescence requirement.
8. **Recovery:** migration re-run idempotence — second run with egg-info/build already deleted exits clean (design claims 8 idempotent steps; exercise steps 4–8 twice).
9. **Recovery:** post-fix wheel test on a machine that *has* wheel but where the first build fails → rerun after fixing pyproject (retry hygiene of the subprocess tests).

## 5. CI/manual-suite gaps, flake risks, fixtures, exit criteria

**CI gaps:**
- Wheel coverage absent in CI until dev extras fix lands (the campaign's raison d'être) — plus Finding 1 (no static guard) and Finding 2 (no install smoke).
- `build/` unignored → `?? build/` noise and F4 over-ignore risk until `/build/` lands.
- No chaos/load/benchmark separation: everything runs in `make test`; if chaos tests grow, consider a `-m "not chaos"` fast path (today: 2 fast in-memory tests — no action needed).

**Flake risks:** lock contention (Finding 3 — observed, 21–23 failures); `test_pytest_markers_filter` subprocess timing (pre-existing, not in failing set); fresh-venv sdist builds depend on setuptools≥61 floor — pinned via build-system requires, low risk.

**Fixtures needed:** one venv fixture (or `--target` install dir) for Finding 2's install smoke; nothing else — the module-scoped `sdist_file` and `_minimal_repo_copy` fixtures are sound (builds from minimal tmp copy, never the live tree — verified: no `/tests/` leak in sdist).

**Exit criteria (all must hold):**
1. Fresh venv with only `pip install -e ".[dev]"` from the fixed tree → `tests/test_packaging.py` = **7 passed, 0 skipped** (CI-equivalent).
2. Finding 1's static guard test added and passing.
3. (Recommended) wheel install-smoke test added and passing.
4. `git check-ignore build/` → rc=0; `git status --porcelain` shows no `build/`, no `ai_batch_runner.egg-info/` after a build.
5. Full `make ci` green in a quiescent repo (re-run required — today's 21–23 failures were lock-contamination, not regression evidence).
6. Stale count comments (Finding 4) corrected in the same change.

**Bottom line:** the design is sound and empirically grounded — its central claims (CI gap mechanism, zero-workflow-change fix, fix validated to unskip) all reproduced. It must not merge without the F1 static guard (Finding 1), which is the difference between "fixed" and "fix that can silently un-fix itself"; the install smoke (Finding 2) closes the largest remaining behavioral gap in the packaging net.
