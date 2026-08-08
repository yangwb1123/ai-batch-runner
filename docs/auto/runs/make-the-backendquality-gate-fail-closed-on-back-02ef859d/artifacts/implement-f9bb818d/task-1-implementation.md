# Implementation: Make the backendquality gate fail closed on backend repos

Direction: *Make the backendquality gate fail closed on backend repos instead
of silently passing 0 scanned files (hardcoded `--dir {cwd}/src`)*

## 1. Change report

### Files changed

| File | Change |
|---|---|
| `scripts/check-backend-quality.py` | `--dir` mode becomes manifest-aware and fail-closed (see §2); JSON gains additive `manifest`/`scan_error`/`status` fields only |
| `pi-batch.yaml` | `backendquality` validator: `--dir {cwd}` (root, not `{cwd}/src`) + `-o {cwd}/docs/backend/backend-quality.json` (out of `docs/ui/refactor/`) + comment |
| `tests/test_backend_specs.py` | Updated empty-dir test (AC-5 signal) + 12 new regression tests (AC-1…AC-5 + language pinning/walk-up/precedence/JSON) |
| `examples/backend-engineering-review.yaml` | Comment: no more manual `--dir` edit; sample command shows manifest-aware usage |
| `.gitignore` | `docs/backend/` (report dir, consistent with the gitignored `docs/ui/`) |

### New/changed symbols (script)

- `MANIFEST_SPECS` / `_MANIFEST_BY_NAME` / `_MANIFEST_ALIASES` / `MANIFEST_WALK_UP` — manifest table (go.mod→.go, Cargo.toml→.rs, pom.xml→.java/.kt, package.json→.ts/.tsx/.js), fixed precedence, walk-up ≤6 levels.
- `detect_manifest(directory)` — nearest manifest, level-first.
- `resolve_manifest(directory, forced)` — auto-detect vs `--manifest` assertion (known names; unknown → union exts, still fail-closed).
- `resolve_dir_scan(directory, forced)` — fail-closed state machine: `scan_error` / `empty` / `skipped` / `ok`.
- `parse_argv` — new `--manifest` flag (restructured to keep cognitive ≤20).
- `_scan_paths` / `_emit_report` / `_finish` — extracted from `main` (file ≤500 lines, func ≤50, complexity ≤15/20).

## 2. Behavior contract (—dir mode)

| Condition | Exit | stderr / stdout signal | JSON `status` |
|---|---|---|---|
| missing / not-a-dir target | 1 | `scan error: directory not found: X` | `scan_error` (+`scan_error` field) |
| manifest found (or forced) + 0 files scanned | 1 | `manifest go.mod found but 0 backend files scanned` | `empty` (+`scan_error` field) |
| manifest + files | 0/1 by violations | scans ONLY the manifest's language | `ok` (`manifest` field) |
| no manifest (auto) | 0 | `no backend manifest detected; gate skipped` | `skipped` |
| `--manifest NAME` unknown | — | union-extension scan, still fail-closed on missing dir / 0 files | — |

Positional-file mode, exit codes {0,1}, and all pre-existing JSON fields are
byte-compatible (additive fields only). Callers (`health.py`, `advance.py`,
agent-engineering org gates) needed zero code changes.

## 3. Acceptance mapping (commands actually run)

| AC | Check | Result |
|---|---|---|
| AC-1 | `python scripts/check-backend-quality.py --dir /nope --strict` | exit 1, `scan error: directory not found` (was exit 0 `OK (0 file(s) scanned)`) ✅ |
| AC-2 | dir with `go.mod`, 0 `.go` files | exit 1, `manifest go.mod found but 0 backend files scanned` ✅ |
| AC-3 | registry resolves root from layout: `--dir {cwd}` (manifest walk-up, no hardcoded `/src`) | registry command + comment updated; live: Go repo scanned via root, TS violations excluded ✅ |
| AC-4 | report outside `docs/ui/refactor/` | `-o {cwd}/docs/backend/backend-quality.json`; live report written with `status: skipped` for this repo ✅ |
| AC-5 | empty-repo passthrough requires explicit signal | `test_backend_checker_empty_dir_skips_with_signal` asserts `no backend manifest detected; gate skipped` (exit 0) ✅ |

## 4. Regression tests (tests/test_backend_specs.py)

`test_backend_checker_missing_dir_fails_closed`, `manifest_zero_files_fails_closed`,
`no_manifest_dir_skipped_not_scanned`, `go_manifest_scans_only_go`,
`rust_manifest`, `java_manifest`, `node_manifest`, `manifest_walkup`,
`manifest_precedence`, `manifest_flag_known`, `manifest_flag_unknown_union`,
`relative_dir_resolves_for_walkup`, `dir_json_additive_fields`,
`dir_skipped_json_status`, `scan_error_json`, plus updated
`empty_dir_skips_with_signal`.

## 5. Deviations from the design (evidence-backed, all additive)

1. **Relative-path walk-up bug (found by live testing)**: `Path(".")` is its
   own parent, so a relative `--dir .` never walked up — it silently skipped
   while the absolute form failed closed. Fixed: `directory.resolve()` before
   detection; regression test added.
2. **Stray-manifest discovery**: this dev box has `/tmp/go.mod` (3 levels
   above pytest tmp dirs), which would fail-close the AC-5 skip tests. The
   no-manifest tests now use a scratch dir under the repo root (ancestors
   verified clean by the design stage); documented in the fixture.
3. **Forced-manifest label**: dropped the "is the file really present?"
   check for `--manifest` — forced is an assertion, deterministically labeled
   `manifest go.mod (--manifest) found but 0 backend files scanned`.
4. **`status` values**: `ok | skipped | scan_error | empty` (not enumerated
   by the design; consumers only read `total`/`files_scanned`/`violations`).

## 6. Verification (per agent-guardrails.md — executed, not claimed)

- `python -m pytest tests/test_backend_specs.py tests/test_validator_registry.py tests/test_context_eval.py tests/test_capabilities.py` → **135 passed** (incl. all new tests; non-CLI subset of test_pi_batch.py)
- `python cli.py check` → exit 0 (filesize ≤500, complexity ≤20, root policy, style — remaining WARNs are pre-existing at HEAD)
- `python quality.py --strict .` → OK
- `make self-test` → exit 0; `python -m pytest checks/` → 49 passed
- `python pi-batch.py health` → HEALTHY, score 100.0, `backendquality: 0 违规`
- `python pi-batch.py check` → all 9 gates passed (incl. registry schema)
- `make ci` → `check`/`self-test`/`quality`/`org` green; `test` phase **could not complete**: 21 failures in `test_pi_batch.py`/`test_local_fixes.py` are all `exit 5: Another pi-batch instance is running (pid=1547473, argv=pi-batch.py campaign …)` — the campaign pipeline holds the repo lock; these CLI-spawning tests cannot run concurrently. Unrelated to this change (verified via the error message in every failing test).

```yaml
completion_report:
  summary: backendquality --dir mode is now manifest-aware and fails closed
    (missing dir -> exit 1; manifest + 0 files -> exit 1; no manifest ->
    explicit skip signal); registry scans {cwd} and reports to docs/backend/.
  changed_files:
    - scripts/check-backend-quality.py
    - pi-batch.yaml
    - tests/test_backend_specs.py
    - examples/backend-engineering-review.yaml
    - .gitignore
  commands_executed:
    - command: pytest tests/test_backend_specs.py tests/test_validator_registry.py tests/test_context_eval.py tests/test_capabilities.py
      result: passed
    - command: python cli.py check
      result: passed
    - command: python quality.py --strict .
      result: passed
    - command: make self-test
      result: passed
    - command: python -m pytest checks/
      result: passed
    - command: python pi-batch.py health
      result: passed
    - command: python pi-batch.py check
      result: passed
    - command: make ci (check, self-test, quality, org)
      result: passed
    - command: make ci (test phase)
      result: not_executed
      reason: 21 CLI-spawning tests fail with the repo-lock guard (exit 5 =
        another pi-batch campaign instance, pid 1547473, is holding the
        lock); every failure carries that message, none touches the changed
        code paths
  residual_risks: []
  assumptions:
    - Python projects (pyproject.toml) are deliberately out of auto-detection
      scope per the design guardrail; opt-in via --manifest (union fallback)
```
