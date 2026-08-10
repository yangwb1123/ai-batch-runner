All verification complete. Here is the re-review of the current tree state (post-implementation, pre-commit).

---

## 1. Verdict line

```
VERDICT: FAIL - the primary blocker F1 (JSON-mode fail-open) is fixed and tested, but the
second blocking gap F2 is NOT: tests/test_validator_registry.py — the standing suite that
let the -o argparse bug survive since 081ac29 — still has zero uigeometry coverage in all
three of its tests (expected-names list, python-validators dry-run, empty-dir project-scan
loop). Additionally F4 remains: border-radius: 8.5px silently passes the gate (verified,
exit 0), so radius-family enforcement — the campaign's core purpose — is bypassable by
non-integer px. F3 (doc-drift pin test), F6 (exit-code contract), F7 (README "eval 回归 40
例") are also still open.
```

## 2. Findings table

| # | Sev | Prior status → now | Evidence (executed) | Missing test |
|---|---|---|---|---|
| F1 | **BLOCKING→✅ FIXED** | JSON fail-open under `--json` | Registered-command shape (`--dir tmp --all --json -o tmp/geometry.json`) on 6px fixture → **exit 1**, report file written, dynamic `{4,8,12}` message. `test_json_mode_fails_closed_on_violations` + `test_report_output_flag` + compliant e2e all pass (24 passed in geometry+registry files) | — |
| F2 | **BLOCKING→❌ OPEN** | Registered-validator drift undetected — recurrence guard absent | `test_validator_registry.py`: names list (l.21) = `...uicheck, uiquality, backendquality...` — **no uigeometry**; `test_python_validators_run_dry` runs only uispacing/uicolor/uistyle; `test_project_scan_validators_ok_on_empty_dir` (l.97) loop = `("uicheck","uiquality","backendquality")` — no uigeometry. `grep -rln uigeometry tests/` → only test_ui_geometry.py + test_classifier.py (name-resolution), **not the registry suite** | Add `"uigeometry"` to the names list; add to the empty-dir loop (this repo has no `src/` → exit 0 expected); add a violating-fixture variant through the **exact pi-batch.yaml command** (`--dir tmp --all --json -o` on a 6px fixture → assert `== 1` + report written). This 3-line change is what would have caught `081ac29`; its absence is the exact recurrence hole |
| F4 | High→❌ OPEN | Precision edge: `int("8.5")` → `ValueError: continue` in `check_radius` | `.a { border-radius: 8.5px; }` → **exit 0**, `"violations": {}` in JSON mode and PASS in non-JSON (verified) | `.a { border-radius: 8.5px; }` → assert exit != 0 (or explicit documented acceptance + pin). Also `8PX` casing. The flip makes this *more* salient: an 8pt-world typo evades the gate being fixed |
| F3 | High→⚠️ HALF | Docs fixed; regression pin absent | geometry.md §4 `radius.sm 4 / md 8 / lg 12` ✅; ui_designer.md:25 `{4,8,12}` ✅; README:596 `{4,8,12}` ✅; error message dynamic via `family_label()` ✅; `grep -rn "6,10,16\|radius.sm 6" README.md ui-specs/ prompts/ scripts/` → **clean** (build/lib copy also now clean — tree mutated at 05:43:48 after qa_lead's 05:41 reading) | Design's F3 pin test absent: assert no `6,10,16` / `radius.sm 6` literals remain in geometry.md / ui_designer.md / README, and message contains the **derived** family not a literal |
| F5 | Medium→⚠️ PARTIAL | Loader: fail-closed implemented; degenerate cases untested | Missing→exit 2 + dynamic msg (tested ✅); unparseable→exit 2 (tested ✅); **empty `radius: {}` → exit 2** (verified, untested); exemption-collision tokens (`0px/999px` in family → family `{0,999}`, `8px` rejected; no semantic break, untested); A1 reads tokens.json at runtime + behavioral flips ✅ (assertion still hardcodes `{4,8,12}` — acceptable as pin since flips derive from live family) | Unit tests for `{}` family → exit 2; tokens containing `0`/`999px` → defined precedence |
| F6 | Low→❌ OPEN | Exit-code contract (0/1/2) claimed "unchanged", unpinned | 0/1/2 paths all behave correctly in execution; usage-error→2 and missing-`--dir`→2 come from argparse (untested); report JSON shapes differ between gates (geometry `{files,violations}` vs spec `{mode,directory,files_scanned,violations,total}`) — parity test absent | Pin `usage→2`, `missing --dir→2`, `JSON+violations→1`; parity fixture through both scripts |
| F7 | Info→❌ OPEN | README:600 "eval 回归 40 例" attached to uigeometry | Verified stale: `evals/` contains assess/classifier/rules only, **no geometry case**; 42 evals are rule-system regressions, not gate executions | Fold into B2 doc correction (still unfixed); optionally an `evals/` entry running the registered command on a fixture |
| NEW-A | Medium | Retry-loop feedback: count-only | Runner `_cap_feedback(v.stderr or "")` feeds **stderr only**; geometry JSON mode prints violation details to stdout, stderr gets `uigeometry: FAIL — N violation(s)` count only. **But**: check-ui-spec JSON mode is identical (verified — details to stdout, summary to stderr) → **systemic, pre-existing, not a regression**; still undermines B1's fail-closed loop (agent must guess the fix from a count) | Emit per-violation lines to stderr in JSON mode (parity with non-JSON path); test asserting stderr contains `border-radius` detail through the registered command |
| NEW-B | Low-Medium | cwd-tokens fallback untested | `_candidates()` = script-root first, cwd second — foreign repo's `ui-specs/tokens.json` only governs if the tool's own copy is missing; implemented but no test | `UI_TOKENS_PATH` + cwd-copy precedence test (cheap, per qa_lead) |

**Environment (not this change)**: A4 `make ci` remains unachievable — `pbatch/cli_batch.py` is 501 lines vs Makefile `check-filesize` 500 budget (verified), from another campaign's commit; plus lock-contention test failures while pid 1684832 holds the campaign lock (qa_lead reproduced on pristine tree).

## 3. Risk-coverage matrix

| Risky path | Covered | Evidence |
|---|---|---|
| Family resolves wrong direction | ✅ | A1 + flipped fixtures (8px pass / 10px reject verified) |
| Enforcement reversal proven | ✅ | Fixture flips; step-2 red intent preserved in history |
| tokens.json missing / unparseable | ✅ | exit 2 + dynamic msg, tested |
| tokens.json empty / degenerate | ⚠️ | Implemented fail-closed (verified exit 2), **untested** |
| Exemption precedence vs tokens | ⚠️ | Behavior coherent, **untested** |
| **JSON-mode fail-open (F1)** | ✅ | exit 1 + report written, tested |
| `-o` writes report | ✅ | clean + violation branches tested |
| **Registered command recurrence (F2)** | ❌ | **registry suite still omits uigeometry** |
| No-frontend repo passes | ✅ | exit 0 verified (missing dir); but not in empty-dir registry loop |
| Stale doc copies (F3) | ⚠️ | Docs fixed (grep clean), **no pin test** |
| Dynamic error message | ✅ | `family_label()` derived; message verified `{4,8,12}` |
| Spacing drift | ✅ | A2 three-way pin (tokens == SPACING_TOKENS == spacing.md §4) |
| **Precision edge 8.5px (F4)** | ❌ | **silent pass, untested** |
| Exit-code contract 0/1/2 (F6) | ⚠️ | Behavior correct, unpinned |
| Retry-loop actionable feedback | ❌ | count-only stderr (systemic, uicheck same) |
| quality.py dup-body (ui_tokens.py) | ✅ | `quality.py --strict .` → OK |
| Async/races/timeouts/duplicates/409/permissions/unmount/rollback/pagination | N/A | Deterministic CLI; no async surface. Partial-success analog = multi-file `--dir` aggregation, existing coverage |

## 4. Honesty audit

| Claim | Verified? | Result |
|---|---|---|
| 17 tests in test_ui_geometry.py, 24 pass with registry file | ✅ | 17 test fns; my run: 24 passed |
| A1/A2/A3/B1/B1-ext implemented | ✅ | All five test families present, behavior re-executed |
| JSON-mode fail-open fixed (deviation #1, real) | ✅ | exit 1 verified through exact command shape |
| `-o` added to pbatch/ui_geometry.py (deviation #2, real) | ✅ | Code + docstring verified |
| `UI_TOKENS_PATH` env override (deviation #3, real) | ✅ | Used by fail-closed tests; verified |
| quality.py --strict OK | ✅ | `quality.py --strict .` → OK |
| eval 42 passed | ⚠️ | Not re-run (lock contention); README says "40 例" — count drift itself is F7 |
| 823 passed / 21 lock-failures | ⚠️ | Corroborated by qa_lead pristine-tree reproduction; not re-run |
| Registered e2e exit 0 + geometry.json written | ✅ | Verified; `docs/ui/refactor/geometry.json` now possible |
| **"exit codes unchanged" (design claim)** | ⚠️ | Deviation #1 honestly *discloses* the 0→1 change — good; but F6 (no pin) and F2 (no recurrence guard) are omitted from the change report entirely |
| README:600 "eval 回归 40 例" | ❌ | Still stale — change report claims README corrected but this line remains |
| build/lib stale `{6,10,16}` (qa_lead 05:41) | ✅ resolved | Tree mutated at 05:43:48; grep clean now — but this is the *third* doc copy of the family that no test pins |
| not_executed / skipped tests | ✅ | No skips in geometry suite; no "理论上应该通过" phrasing; every claim names commands I re-ran |

**Bottom line for implement/acceptance**: before acceptance can pass, (1) add uigeometry to all three tests in `tests/test_validator_registry.py` (names list + empty-dir loop + violating-fixture variant through the exact registered command) — written red against a hypothetical re-broken `pi-batch.yaml:165`; (2) decide and pin the non-integer-px contract (`8.5px` currently bypasses radius enforcement); (3) fold the still-stale README:600 eval claim into B2; (4) A4 must be re-run after `cli_batch.py`'s 501-line overflow is resolved by whoever owns it. F1's fix itself — fail-closed JSON mode, dynamic family message, additive `-o` with report-on-violation parity — is solid, verified, and honestly documented.
