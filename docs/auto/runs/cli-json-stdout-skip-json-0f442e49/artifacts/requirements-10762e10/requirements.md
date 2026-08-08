Done. The requirements spec is at `docs/auto/requirements/scripts-8f645734-requirements.md` (18.5KB), following the repo's established convention (same structure as `checks-d4b833b0-requirements.md`).

## What was verified (all live, this repo)

**All 7 citations checked** — 5 exact, 2 with shifted line numbers (`check-backend-quality.py:323/371` point to different functions than named, but the file-level claims they support — hand-written `parse_argv` at :396 with no `--skip`, summaries at :464/:466 printing to **stdout** — are fully confirmed).

**Both pollution paths live-reproduced** with `json.loads` failing:
- backend `--dir . --json` → trailing `BACKEND-QUALITY: no backend manifest detected; gate skipped` (skipped path)
- backend manifest fixture → trailing `BACKEND-QUALITY: OK (1 file(s) scanned)` (OK path)
- frontend `--dir examples --json` → trailing `UI-QUALITY: OK (0 file(s) scanned)`

**New evidence found** (strengthens the direction): unknown flags are silently mis-parsed as *file targets* — `--skip scratch` today yields `files_scanned: 4` with `'--skip'`/`'scratch'` counted as "missing target" files, neither rejected nor honored. Also confirmed `advance.py:97-108` is a third consumer silently degraded by the same contract defect (`json.loads` → `{}` on this repo).

**Family convention confirmed**: `check-design-intelligence.py`/`check-knowledge-freshness.py` already support `--skip` (used by `pi-batch.yaml:131`), and `check-ui-spec.py:244-253` is the correct stdout contract — proving this is fixable inconsistency, not design.

## The spec

- **R1** `--skip` (comma-separated path fragments, union with `DEFAULT_EXCLUDES`, segment-match semantics per the design-intelligence convention) for both checkers
- **R2** `--json` stdout contract: exactly one JSON document; all summaries → stderr (aligned to ui-spec's pattern); non-JSON mode byte-identical
- **R3** `health.py` patch removal (`raw_decode` → `json.loads`, uniform `--skip SKIP`), report shape pinned to the captured baseline (`backendquality: 0`)
- **R4** hermetic regression tests; **R5** optional doc note
- **All 5 supplied acceptance checks preserved**, each made testable with concrete fixtures — including the nuance that `generated,migrations`/`e2e` are already in `DEFAULT_EXCLUDES`, so tests verify the *mechanism* (non-default dir like `scratch/`) plus supplied-invocation idempotence, and a negative-control guard against "replace-not-union" implementations
- Test plan maps each AC to a specific test file (existing `tests/` conventions), with repo-internal fixtures to dodge the stray `/tmp/go.mod` on dev boxes (the exact trap documented in `test_backend_specs.py`)

No scope expansion: ui-spec CLI, other checkers, `advance.py`, and `pi-batch.yaml` validators are explicitly out of scope.
