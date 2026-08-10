# Architecture Review — backendquality gate fail-closed on backend-only repos

**Evidence-conflict note (README rule 5):** The injected `task-1-design` evidence matches this campaign's staged design (verified via `pipeline.yaml` and artifact paths). The `devops_engineer` evidence reviews a *different* campaign's design (close-wheel CI gap) and correctly flags the conflict itself; I use only its verified environment facts (lock-holding CI failures, dirty tree), not its verdict on the other design. `docs/architecture/DIRECTORY_MAP.md` (referenced by this prompt) **does not exist** in this repo — canonical navigation is `docs/ARCHITECTURE.md`; no map content was consulted.

---

## 1. Scope, assumptions, verified architecture summary

**Scope:** the manifest-aware fail-closed rework of `scripts/check-backend-quality.py` `--dir` mode, its `pi-batch.yaml` registry entry, `.gitignore`, and regression tests — reviewed without implementing code. All findings below are **Verified** against live reproductions at worktree HEAD `75a7a27` + uncommitted implementation (499 lines).

**Layer map (composition / interfaces / infrastructure / governance):**

| Layer | Component | Role |
|---|---|---|
| Shared tooling (infrastructure) | `scripts/check-backend-quality.py` | Single-file stdlib checker; portable-copy pattern shared by 10 checkers (no shared code except `ui_tokens`) |
| Contract | CLI + JSON report | Exit codes {0,1}; additive fields `manifest`/`status`/`scan_error` only; data = fixed `MANIFEST_SPECS` table, no abstraction |
| Validation | `pi-batch.yaml:181` `backendquality` validator | Registry invocation `--dir {cwd} --strict --json -o {cwd}/docs/backend/backend-quality.json` |
| Domain/orchestration | `pbatch/advance.py` (`be_errors`/`be_architecture` dims), `pbatch/org.py` gates, `pbatch/health.py` probe | Subprocess consumers of the report |
| Governance | `agent-engineering/{architecture,data,developer}.yaml` → `--dir {code}` | Per-module gate execution, exit-code-only adjudication |

**Verified architecture properties:**
- **Dependency direction**: all callers reach the script only via subprocess (process boundary; no import coupling); script imports stdlib only. No cycles. Ownership: script owns its contract; report schema additive-only. ✓
- **Fail-closed matrix (live)**: missing dir → 1 (`scan error: directory not found`); regular file as dir → 1 (`not a directory` — QA F2 resolved); manifest + 0 files → 1; no manifest → 0 + `no backend manifest detected; gate skipped`; `--manifest` known → forced language, 0 files → 1; unknown → union exts, still fail-closed; **`--manifest` as last arg → no crash, exit 1** (F5 crash risk resolved — falls through to "missing target" violation). ✓
- **Relative `--dir .`**: walk-up bug (Path(".") parent identity) fixed via `resolve()` before detection; regression test present. ✓
- **Budgets**: `quality.py --strict scripts/check-backend-quality.py` OK; 56/56 tests pass incl. 17 manifest/dir cases. ✓
- **Baseline contrast**: OLD code at HEAD scanned this repo `--dir . --strict` = 106 files / **22 violations** (design evidence said 21 — off by one, immaterial); HEAD registry was `--dir {cwd}/src` (missing dir → the vacuous pass being fixed). NEW code on this repo: **skipped, 0 files, exit 0**.
- **Environment hazard confirmed**: stray `/tmp/go.mod` (present on this dev box) false-closes every `--dir` under `/tmp` — my "no-manifest" control run exited 1, not 0. The test suite works around it via a repo-scratch fixture (documented at `tests/test_backend_specs.py:24-28`); the design's "no stray manifests in any ancestor" claim holds only for the repo root's ancestors.
- **Process finding (F1) confirmed**: the design artifact is 17 lines; the 8 failure modes / 6 migration steps / 10 regression tests / AC bindings it references exist nowhere in the artifact. The implementation does match the summary, so this is a documentation-completeness gap, not a correctness defect.

---

## 2. Findings table (severity-ordered)

| # | Sev | Finding | Evidence (all live) | Impact | Recommendation |
|---|---|---|---|---|---|
| A-1 | **High** | **Language pinning silently disables the data-loss DDL check.** `.sql` is in `DEFAULT_EXTS` but in **no** manifest's extension set; manifest mode therefore never scans `.sql`. | `go.mod` + `main.go` + `schema.sql` containing `DROP TABLE users;` → `OK (1 file(s) scanned)`, exit 0. Pre-change union scan flagged it as an error. | The gate's most safety-critical check (dangerous DDL outside migrations, `_ddl_warnings`) becomes dead code for every Go/Java/Rust/Node repo — precisely where enforcement now applies. Fail-closed covers "0 files", not "wrong-language files". | Restore a fixed **safety-subset scan** (`.sql` + the DDL/persistence patterns) in manifest mode regardless of pinning (Option B). |
| A-2 | **High** | **Monorepo subdirs false-close or mis-pin via ancestor walk-up.** `agent-engineering/*.yaml` org gates run `--dir {code}` per module; walk-up finds the root manifest and pins the language. | Python service dir inside a Go monorepo (root `go.mod`) → `manifest go.mod found but 0 backend files scanned`, exit 1. Go root pins `.go` only. | Legitimate mixed-language modules fail the gate with no declarative escape (mitigation is per-invocation `--manifest`); conversely a Java subtree under a Go root silently escapes if any `.go` file is present. QA F7's silence is removed, but the *mechanism* now over-blocks. | Per-module manifest or a bounded `--manifest`/no-walk-up escape wired into org gate yamls; document precedence semantics (Milestone 5). |
| A-3 | **High** | **Caller semantics degrade while exit codes stay compatible — the "zero caller changes" claim is exit-code-true, semantics-false.** | (a) `pi-batch health` probe on this repo: `status: skipped, files_scanned: 0` reported as `门禁 backendquality: 0 违规` (verified live; `health.py` uses `raw_decode` but ignores `status`). (b) `advance.py:_run_checker` does `json.loads(stdout)` — breaks on the trailing skip/OK line (verified `JSONDecodeError: Extra data`); backend dims silently `{}` for every manifest-less repo (pre-existing for OK lines, now universal). (c) `test_validator_registry.py:93` asserts exit 0 on this repo — the tool's own registered `backendquality` validator is now a self-skip, and a test codifies it (comment stale: "scan {cwd}/src"). | "Skipped" is indistinguishable from "scanned clean" in health; `advance` backend dimensions never work; the tool's own gate is vacuous on itself. QA F3 (org gates lose enforcement on Python/SQL/PHP repos) is confirmed — including error-level patterns (status assignment, DDL) that union mode previously applied to `.py`/`.sql`. | Surface `status` in health (skipped → unknown, not 0); fix advance parsing with `raw_decode` (same as health); update the registry test to assert explicit skip semantics (Milestones 3–4). |
| A-4 | Medium | **`docs/backend/` gitignore collision (F6).** | `.gitignore:10` now ignores `docs/backend/`, but `examples/backend-engineering-review.yaml` and `examples/backend-implementation-pipeline.yaml` write their intentional deliverables (`inventory.md`, `findings.md`, `plan.md`, `implementation.md`, `gate.md`) there. | Pipeline outputs silently invisible to git (and to `git_commit: true` stages); user-facing deliverables lost from history. | Narrow the ignore to `docs/backend/backend-quality.json`, or move the gate report into `.pi-batch/reports/` (Milestone 2). |
| A-5 | Medium | **stdout channel for diagnostics unspecified (F4).** Skip/OK messages print to stdout (after JSON in `--json` mode); errors to stderr. | Verified `JSONDecodeError` on `--json` stdout; health.py survives via `raw_decode`, `advance.py` does not. | Naive JSON consumers (including `advance.py`) misbehave; machine channel not pure. | Keep stdout JSON-only when `--json`; move diagnostics to stderr (Milestone 3, small delta). |
| A-6 | Medium | **Design artifact thinness (F1).** | Artifact is 17 lines; 8 failure modes / 6 migration steps / 10 regression tests / AC bindings referenced but absent; gate passed anyway. | Claims untestable as delivered; gate accepted an unverifiable design. | Design-stage output standard: full failure-mode + acceptance mapping must be inside the artifact; gate fails on reference-only claims. |
| A-7 | Low | **Environment hazard, bounded walk-up.** Stray ancestor manifests false-close unrelated dirs. | `/tmp/go.mod` → `--dir /tmp/arch-review-nomanifest` exit 1 instead of skip (verified; fixture workaround documents it). | Operational surprise for any project tree under a manifest-bearing ancestor. | Document in `docs/SPEC_SYSTEM.md`/script docstring; consider `--manifest none` to force skip (Milestone 5). |
| A-8 | Info | Design evidence off-by-one (21 vs 22 violations) and HEAD registry path drift (`pi-batch.yaml:176` vs `:181`). | `OLD --dir . --strict` = 22 violations, 106 files. | None. | — |

---

## 3. Decision options

**Option A — Ship as-is** (current implementation). Closes the vacuous-pass defect for Go/Java/Rust/Node repos; smallest diff; Python/SQL coverage loss is a documented non-goal. **Costs:** A-1 (DDL data-loss check dead in default mode) and A-2/A-3 remain live — the gate's *reason to exist* is partly neutralized exactly where it now applies, and callers misreport.

**Option B — A + safety-subset union scan** (preferred). In manifest mode, additionally scan a fixed `SAFETY_EXTS = (".sql",)` (and the DDL/persistence/status-assignment error patterns) regardless of pinning; keep fail-closed for the pinned language. **Trade-offs:** ~30-line delta, script-only (no caller API change), restores the data-loss net without reopening the union-scan blast radius that would break `test_validator_registry.py`; hybrid repos regain error-level checks (pre-change behavior for that subset). Risk: repos deliberately keeping DDL outside migrations now fail — that is the check's purpose, and `--manifest`/excludes offer escape. **Build-vs-buy:** N/A — no external tool does manifest-scoped heuristic checks with this exit-code contract; extending in place is cheapest.

**Option C — B + status-aware callers** (fast-follow after B). health.py reports skipped as unknown; advance.py switches to `raw_decode`; registry test asserts explicit skip. Breaks the "zero caller changes" claim but restores observability honesty. Small, low-risk, does not block B.

**Preferred: B now, C as Milestone 3–4.** Rationale: B is the smallest change that prevents a *silent safety regression*; C fixes two pre-existing caller defects that the manifest semantics newly make consequential.

---

## 4. Prioritized implementation sequence

| # | Milestone | Scope | Compatibility plan | Risks | Executable acceptance check |
|---|---|---|---|---|---|
| 1 | **P0 — Safety subset (A-1)** | Script: `SAFETY_EXTS` scan of `.sql` in manifest mode; tests | Script-only; exit codes {0,1}, JSON additive unchanged; no caller change | DDL outside migrations in legit repos → new failures (by design; `--manifest` + excludes escape) | `python scripts/check-backend-quality.py --dir <go-repo-with-ddl-sql> --strict` exits 1 with `dangerous DDL`; new regression test; `pytest tests/test_backend_specs.py` 56+ green |
| 2 | **P1 — Report path (A-4)** | Narrow `.gitignore` to `docs/backend/backend-quality.json` or move report to `.pi-batch/reports/`; update registry + test comment | No runtime change | — | `git check-ignore docs/backend/inventory.md` → unignored; `git check-ignore docs/backend/backend-quality.json` → ignored |
| 3 | **P1 — JSON purity + health honesty (A-3a, A-5)** | `--json` → diagnostics to stderr; health.py maps `status != "ok"` to unknown (−1); display `未知` | health display only; `healthy` computation unchanged | Probe count semantic flip could alert operators (desired) | `pi-batch health` shows `门禁 backendquality: 未知` on a manifest-less repo; `--json` stdout parses with bare `json.loads` |
| 4 | **P2 — advance.py parser (A-3b)** | `_run_checker` → `raw_decode` (align with health.py) | Same subprocess contract | Re-enables `be_errors`/`be_architecture` dims that were always empty — behavior change is a fix, not a regression | `advance` scan of a Go fixture reports `be_errors` counts > 0 |
| 5 | **P2 — Monorepo guidance (A-2, A-7)** | Document walk-up precedence + `--manifest` escape in `agent-engineering/` yamls/README; optional `--manifest none` skip | Doc + example only | — | `pi-batch org gate developer --task … --code <py-svc-in-go-monorepo>` shows documented outcome; example updated |
| 6 | **P3 — Gate run** | Commit this campaign's implementation first (see risks); run `make ci` in a **lock-free** environment (CI or scratch worktree — the live campaign lock makes in-tree `make ci` fail 21 tests, verified; implement artifact honestly recorded `not_executed` for the test phase) | — | Dirty tree (47 M/169 ?/19 D) mixes campaigns — `check-backend-quality.py` is touched by multiple concurrent implementations; snapshot baseline failures before judging the diff | Clean-worktree `make ci` + `quality.py --strict .` + `pi-batch health` + `pi-batch check` all green; `git status --porcelain` clean at gate time |

**Migration cost overall:** low — 5 files touched by this campaign, script-local logic, no schema/exit-code break. **Technical debt carried:** advance.py's pre-existing `json.loads` break (now Milestone 4 instead of out-of-scope); F1 design-artifact standard.

## 5. Unknowns needing owner/product decisions

1. **Python/SQL/PHP enforcement surface** — the design pins these out (opt-in `--manifest` only). This repo *is* Python; its own `backendquality` validator now self-skips (test-codified). Is the tool expected to gate Python/SQL backends? If yes, union fallback for error-level patterns (or a `.py` manifest entry) needs a decision — Option B partially answers for `.sql`.
2. **Monorepo precedence semantics** — should ancestor manifests pin subdirs (current, false-closes mixed modules), or should walk-up require explicit `--manifest` (weaker default, no false-close)? The fail-closed vs over-block trade-off needs an owner call.
3. **Gate-report ownership** — which dir owns machine reports vs pipeline deliverables (`docs/backend/` collision, A-4); commit-or-ignore policy for validator JSON.
4. **DDL resurrection scope** — safety-subset = `.sql` only, or also error-level `.py` patterns in manifest mode (extends A-1 fix to F3's Python loss)?
5. **Baseline gate expectations** — `test_project_scan_validators_ok_on_empty_dir` asserts exit 0 for a *skipped* scan; should it instead assert `status: skipped` so the tool's self-gate expectation is explicit rather than accidental?

**Advisory verdict:** the design and implementation are sound for their stated goal (fail-closed for Go/Java/Rust/Node repos), correctly minimal (data table over abstraction; no speculative axis), and the exit-code/JSON compat invariants hold as verified. The two material architecture defects are A-1 (safety checks die under language pinning — the strongest argument for Option B) and A-3 (callers misreport skip as clean). Neither blocks the campaign; both should be addressed before this gate is relied upon for production repos.
