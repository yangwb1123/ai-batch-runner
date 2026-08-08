All claims independently verified. Here is my QA review.

---

# QA Review — check-no-refusal: Regression Coverage + Duplicate Elimination

**Scope reviewed**: design `docs/requirements/scripts-check-no-refusal-design.md` + spec `scripts-check-no-refusal-regression.md`, against the live tree. I independently re-ran every evidence claim (diff, grep, yaml count, `--steps` invocations, gate smoke runs, runpy extraction, source reads of `runner.py`/`pipeline.py`/`config.py`/`selfcheck.py`/`test_org.py`).

## 1. Verdict

```
VERDICT: PASS - every acceptance (A1.1–A5.1, FR-4) maps to a concrete runnable
check with exact pass criteria; all 14 evidence claims + 4 FINDs independently
re-verified true; test-first migration with explicit red states; fail-closed
selfcheck step; correct layer choice (stateless deterministic CLI — the risky
paths are drift/registry/pipeline-coupling, all covered). Two findings are
recommended before implementation: marker *rewrite* drift is undetectable
(FM2's claim is wrong) and the empty `--steps` filter still no-ops (D2
incomplete).
```

## 2. Findings

| Sev | Defect pattern | Missing test | Exact test case |
|---|---|---|---|
| **High** | **Marker *rewrite*/typo undetectable — the design's FM2 claim is false.** `_markers()` derives from the script via `runpy` (D4), so a rewritten marker (e.g. `"无法继续"` → `"无法继续执行"`) is still embedded in its own parametrized fixture → gate exits 1 → suite stays green. Count guard (A1.6) catches only *purges*. Spec §2 names "a typo in the marker list" as problem #1 — the design catches 2 of 3 named classes. FM2's "that marker's parametrized case exits 0" is a reasoning error. | Exact-list identity pin | `def test_marker_list_exact(): assert set(_markers()) == {"输入缺失","未指定","输入未提供","无法继续","FAILED — 输入","FAILED — 输入缺失","input missing","no input provided","cannot proceed","上游能力未映射"}` (list from spec F3). 3 lines; subsumes A1.6. |
| **Medium** | **A4.3 "never a no-op" incomplete (D2): empty/whitespace `--steps` filter still exits 0 with 0 gates.** Verified live: `--steps ","` → `CHECK: all 0 gates passed`, exit 0 (same for `--steps "   "`). `wanted=∅` → no unknown → `steps=[]`. | Empty-filter rejection | `test_check_rejects_empty_step_filter`: `subprocess.run([sys.executable,"pi-batch.py","check","--steps",",","--skip-eval"], cwd=ROOT, timeout=120)` → rc **2** (or runs default set). |
| **Medium** | **Smoke wrong-exit branch of `_run_refusal_step` unpinned.** Unit tests cover missing registry/script only; the `ok = r.returncode == want` False-branch (core of fail-closed) is exercised only indirectly via the live happy path. | Stub-script fail-closed | `def test_refusal_step_smoke_fails_on_wrong_exit(tmp_path): stub = tmp_path/"gate.py"; stub.write_text("import sys\nsys.exit(0)\n"); assert selfcheck._run_refusal_step(validators={"refusalcheck":"x"}, script=stub) is False` (controllable: stub, no timing). |
| **Low** | **Unreadable artifact untested; fails closed *by accident*.** `path.read_text()` raises `PermissionError` → traceback + rc 1 (fail-closed but ugly, unpinned). NG1 forbids script edits now — a test pins intent. | Permission edge | `def test_unreadable_artifact_refuses(tmp_path): p = tmp_path/"x.md"; p.write_text("clean"); p.chmod(0); rc = _gate(p)[0]; p.chmod(0o644); assert rc == 1` (skipif root; restore mode in finally). |
| **Low** | **Binary/non-UTF-8 artifact behavior unpinned.** `errors="replace"` keeps ASCII marker bytes → rc 1 (fail-closed), but FM11 says "tests unaffected" — nothing documents the intent; UTF-16-encoded markers evade via NUL bytes (pre-existing semantics, NG1). | Binary-with-marker-bytes | `def test_binary_artifact_with_marker_bytes_refuses(tmp_path): p.write_bytes(b"\x00\xffPK cannot proceed \x00\x01"); assert rc == 1 and "failing closed" in out`. |
| **Info** | Fixture claim "prose + exactly one marker" is imprecise for 3/10 markers: `"FAILED — 输入缺失"` ⊃ `"FAILED — 输入"` + `"输入缺失"`; the smoke fixture `"无法继续：上游输入缺失"` hits 2 markers. Harmless (assertions don't rely on single-hit) but the table overstates. | — (doc precision) | — |
| **Info** | Stale-count drift is systemic: AGENTS.md "23 断言用例" vs "25 eval 用例" vs PLATFORM_MAINTENANCE_GUIDE "27 用例/441 测试"; and AGENTS.md §2.3 code-fence comment `（6 个已注册）` also drifts to 24 — the optional A5.1 regex `(\d+) 个验证器` will *not* catch that comment. | — | Update §2.3 comment in the same commit as line 70; optionally widen A5.1 regex to `(\d+) 个(已注册|验证器)`. |

**Strengths verified**: reproduce-first spirit (migration step 1 lands tests with explicit red states A2.1/A3.1/A4.2 before code); every assertion checks rc *and* output substring (business result, not presence); no sleeps anywhere (timeout=120/30 are deadlines; smoke uses `TemporaryDirectory`; no shared mutable state — `test_registered_in_validators` reads import-time `VALIDATORS` snapshot, safe); layer choice correct (unit = in-process fail-closed, contract = A2.2/A3.1 registry+validate_cmd pins, E2E = A4.2 subprocess, non-functional = bounded subprocess + fail-closed); NG1 (zero gate edits) is the right freeze and DoD step 7 enforces it with `git diff`; `make ci` genuinely does not run `pi-batch check` (Makefile verified) so the smoke adds no CI wall-time.

## 3. Risk-coverage matrix

| Risky path | Covered by | Status |
|---|---|---|
| Marker purged (list shrinks) | A1.6 (≥10) + A1.1 parametrization | ✅ |
| Marker **rewritten**/typo'd (count unchanged) | — | ❌ **not covered** (FM2 claim false; exact-list pin needed) |
| Marker substring overlap (double-hit) | A1.1 (boolean semantics) | ✅ (harmless, verified) |
| Empty / whitespace-only artifact | A1.2 | ✅ |
| Missing artifact / directory-as-path | A1.3 (same `is_file()` branch) | ✅ (dir case implicit) |
| Clean artifact | A1.4 | ✅ |
| No-arg usage | A1.5 (rc 2) | ✅ |
| Diagnostics contract (`REFUSAL-CHECK`, `failing closed`) | A1.7 folded into A1.x | ✅ |
| Duplicate copy reintroduced | A2.1 | ✅ |
| `validate_cmd` drift (relative/examples/ path, wrong form) | A2.2 (exactly 8, strict regex) | ✅ |
| Stray file under `examples/` | A2.3 tree walk | ✅ |
| Registry entry removed | A3.1 + A4.2 + A4.3 unit + step itself | ✅ |
| Registry path drifts to `examples/` | A3.2 | ✅ |
| Step removed/renamed in `_default_steps` | FR-4 structural + D2 (unknown → exit 2) | ✅ |
| Unknown `--steps` name | D2 (verified rc 2 design) | ✅ |
| Empty/whitespace `--steps` filter | — | ❌ **not covered** (verified live: exit 0, 0 gates) |
| Smoke wrong-exit branch | — | ❌ not covered (stub test needed) |
| Subprocess hang | timeout=120 (tests) / 30 (smoke) | ✅ (deadline, not sleep) |
| Permission-denied artifact | — | ❌ (fails closed by accident; pin rc 1) |
| Binary/non-UTF-8 artifact | — | ❌ (ASCII-marker case cheap to pin) |
| Partial success (marker mid-document) | A1.1 fixtures embed marker in prose | ✅ |
| Concurrency (parallel validators) | N/A — script is read-only/stateless | ✅ by construction |
| Async reorder/timeout-uncertain/409/TZ | N/A — no state, no clock in artifact | ✅ N/A |
| AGENTS.md count drift | A5.1 (optional test) / DOC-1 | ✅ (see Info finding for §2.3 comment) |
| `python` vs `python3` interpreter | A2.2 regex accepts both; §6.2 optional | ✅ (pinned as acceptable) |

## 4. Honesty audit (completion-evidence spec)

| Claim | My verification | Verdict |
|---|---|---|
| E1 exit 0/1/2, 10 markers, `REFUSAL-CHECK` | Ran gate: clean→0, marker→1, no-arg→2; counted 10; double-hit confirmed | ✅ **Verified** |
| E2 byte-identical 1414 B, git-tracked | `diff` IDENTICAL; `git ls-files` shows both | ✅ **Verified** |
| E3 zero test coverage | `grep` exit 1 | ✅ **Verified** |
| E4 23 validators, no refusal entry | `yaml.safe_load` → 23, no `refusalcheck` | ✅ **Verified** |
| E5 8 refs / 4 pipelines, exact lines | grep confirms 5+1+1+1, `requirement-pipeline.yaml:44` uses `python` | ✅ **Verified** |
| E6 `{cwd}`→workdir→canonical | `runner.py:487` `shlex.quote(task.workdir())`; `pipeline.py:604` `stage.cwd or os.getcwd()` | ✅ **Verified** |
| F1–F6 (selfcheck steps, VALIDATORS, runpy no-`main()`, interpreter, README repo-root invocation, suite conventions) | source reads + `runpy.run_path` (10 markers, no usage output → `main()` not executed) + `test_entrypoints.py` timeout=120 + pyproject testpaths + README 快速开始 | ✅ **Verified** |
| FIND-1 AGENTS.md:70 "6 个验证器" vs 23 | Read + yaml count | ✅ **Verified** |
| FIND-2 `--steps unknown` silent exit 0 | **Reproduced live**: `all 0 gates passed`, exit 0 | ✅ **Verified** |
| FIND-3 test_org.py pins (`_run_step` direct call, `--steps org,capabilities,tools` → "all 3 gates passed") | Read lines 404–437; semantics preserved by D2 design | ✅ **Verified** |
| FIND-4 no conftest | `ls tests/conftest.py` → absent | ✅ **Verified** |

**Executed vs claimed**: no command in the design's verification plan requires execution I couldn't reproduce; all file/line citations resolve (minor drift: spec says `pi-batch.yaml:122-180`, design says 134-180 — both bound the validators block; immaterial). **Inaccurate claim**: FM2's detection mechanism ("rewritten marker → parametrized case exits 0") is a reasoning error, not fabrication — flagged in Finding 1. **Honest labeling**: optional A5.1 test and §6.2 normalization are explicitly marked optional; no "理论上应该通过" phrasing; DoD includes the NG1 byte-identity guard (`git diff` empty) and an explicit XOR statement for the deletion/identity branches. **not_executed items**: none claimed as run that weren't — the evidence ledger distinguishes read-source (E1) from command-output (E2/E3) verification methods.

**Bottom line**: implement as designed; add Finding 1's exact-list pin and Finding 2's empty-filter rejection in the same change (3+3 lines) — they close the two residual holes in the feature's core promise (marker drift detection) and its stated systemic fix (never a no-op).
