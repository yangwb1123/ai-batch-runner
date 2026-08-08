All claims verified. The requirements specification is complete at `docs/auto/requirements/ui-specs-672fd14a-requirements.md` (following the existing campaign convention `docs/auto/requirements/<module>-<id>-requirements.md`).

## Deliverable summary

**Evidence verification** — every citation checked against the live tree:
- `pi-batch.yaml:163/165/171` — exact hits: `uicheck`/`uigeometry`/`uiquality` all hardcode `--dir {cwd}/src` ✅
- `check-ui-spec.py:24/26` — exact: docstring documents "No frontend files found -> exit 0" ✅
- `docs/ui/refactor/report.json` — exact: `files_scanned: 0, total: 0`, and the repo has **no `src/`** — live-reproduced the false-green (all three gates exit 0 against the nonexistent dir) ✅
- **Stale claim flagged**: problem statement says "and backendquality" — verification shows `backendquality` already uses `--dir {cwd}` with manifest-aware fail-closed semantics; declared out of scope
- **Extra evidence beyond citations**: `geometry.json`/`quality.json` show the same zero-signal; the only placeholder expansion points are `runner.py:487/:698` (`{output}/{cwd}/{tool_root}` only, no var mechanism); config loading pattern at `config.py:26-55,419-430`; regression anchors at `test_ui_check.py:57-61,102-112,138-149`

**Requirements** (all four acceptance checks preserved, made testable as T1–T4 with 6 concrete new regression tests):
- **R1 (AC1)**: optional top-level `ui_scan_dir:` key + `{ui_scan_dir}` placeholder in the three validator commands, expanded at the existing runner sites; unset → `{cwd}/src` (backward compatible)
- **R2 (AC2)**: `uicheck` warns on stderr when `--dir` yields 0 files; exit code stays 0 (preserves the existing test's exit-0 + stdout-summary contract); `--json` stdout stays pure
- **R3 (AC3)**: `lib/` layout scans real files — script-level (exit 1 + `files_scanned == 1`) and wiring-level checks
- **R4 (AC4)**: `pytest tests/test_ui_check.py` + `make ci` as regression gates

**Non-goals** to prevent scope creep: no default scan-root change, no warning for uigeometry/uiquality, no `backendquality` changes, no scan-semantics/CLI changes. Design decision table documents the chosen mechanism vs. two rejected alternatives.
