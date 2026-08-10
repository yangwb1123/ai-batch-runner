All implementation and gates complete. Final change report:

---

# Change Report — Unify UI token single-source-of-truth (radius 4/8/12)

## Decision upheld
`ui-specs/tokens.json` wins → radius family **{4,8,12}** (4pt-grid aligned; `component-spec.md` already binds to tokens.json). Typography/component enforcement gates remain out of scope.

## Files changed

| File | Change |
|---|---|
| **`scripts/ui_tokens.py`** (new) | Shared fail-closed loader: `load_tokens()` (search: script repo root → cwd; `UI_TOKENS_PATH` env override for tests/portability), `radius_family()`, `spacing_tokens()`. Missing/unparseable/non-px values → `TokenLoadError` with dynamic path/value in message. |
| **`scripts/check-ui-geometry.py`** | `RADIUS_FAMILY` → `get_radius_family()` (lazy, derived from tokens.json; import side-effect free); `family_label()` renders dynamic `{4,8,12}` in violation messages; **additive `-o`** flag (JSON report, mkdir parents, written even when clean — parity with check-ui-spec); main split into `_collect_targets/_scan/_emit` for quality budgets; fail-closed `exit 2` on token load failure. |
| **`scripts/check-ui-spec.py`** | `SPACING_TOKENS = ui_tokens.spacing_tokens()` (value-identical today: `{0,4,8,12,16,20,24,32,40,48,64}`); missing/invalid tokens → `exit 2` at startup. |
| **`ui-specs/geometry.md`** | §4: `radius.sm 6/md 10/lg 16` → `4/8/12`（tokens.json 单一事实源）; §6 反模式 updated. |
| **`prompts/ui_designer.md:25`** | `{6,10,16}` → `{4,8,12}`（tokens.json radius 派生）— removes the internally contradictory fourth copy. |
| **`pbatch/ui_geometry.py`** | Docstring family update + `-o` passthrough (prevents the same argparse bug via `pi-batch ui-geometry`). |
| **`README.md`** | `{6,10,16}` → `{4,8,12}`; pipeline-mount claim corrected: ui-refactor carries only `uicheck` (verified `examples/ui-refactor-pipeline.yaml:51`), only frontend-implementation/ui-generation carry uigeometry. |
| **`tests/test_ui_geometry.py`** | Fixture flips + 5 new tests (below). |

## Tests (17 pass, `tests/test_ui_geometry.py`)
- **A1** `test_radius_family_matches_tokens_json` — family == tokens.json `{4,8,12}`; 8px passes / 10px rejected (flip proves enforcement reversed)
- **A2** `test_spacing_tokens_match_tokens_json` — `check-ui-spec.SPACING_TOKENS` == tokens.json spacing == spacing.md §4 (three-way pin)
- **A3** `test_registered_command_8px_fixture_passes` — exact pi-batch.yaml command (`--dir --all --json -o`) on 8px fixture → exit 0 + `geometry.json` written
- **B1** `test_report_output_flag` — `-o` writes report for clean AND violation runs (exit 0/1)
- **B1-ext** `test_json_mode_fails_closed_on_violations`, `test_missing_tokens_fail_closed` (exit 2 + dynamic msg), `test_invalid_tokens_fail_closed`
- Flipped: `test_old_radius_family_rejected` (6/16px now rejected), `test_radius_family_accepted` (4/8/12/999px), TSX fixtures (10px → violation, 8px → compliant)

## Command results
- `pytest tests/test_ui_geometry.py test_ui_check.py test_classifier.py` → **60 passed**
- `pytest tests/` (minus lock-blocked `test_pi_batch.py`/`test_local_fixes.py`) → **823 passed, 1 skipped**; the 21 failures are the running campaign's own lock (`Another pi-batch instance is running (pid=1684832...)`) — **reproduced identically on the pristine tree** (stash-verified), not caused by this change
- `quality.py --strict .` → **OK** (incl. new ui_tokens.py — no duplicate-body trip)
- `python -m pbatch eval` → **all 42 passed**
- `cli.py check` → one pre-existing FAIL (`pbatch/cli_batch.py` 501 lines, zero diff from me, fails on pristine tree — another campaign's commit)
- Registered uigeometry command e2e → exit 0, `docs/ui/refactor/geometry.json` now written (parity with `report.json`/`quality.json`; previously impossible)
- `pi-batch assess` → L0_direct workflow, 6-rule demo prescription — all prescribed specs (geometry/spacing/component-spec) honored

## Evidence-backed deviations from design
1. **Fixed pre-existing fail-open**: original `--json` mode returned 0 even with violations → the registered `--json -o` command could never reject. Design said "exit codes unchanged"; the 0/1/2 scheme is preserved but the json branch now returns 1 (check-ui-spec parity) — without this, B1/A3/A4's fail-closed purpose cannot hold.
2. **`-o` added to `pbatch/ui_geometry.py`** wrapper (design scoped `-o` to the script only) — same argparse-bug class via the subcommand; 5 lines.
3. **`UI_TOKENS_PATH` env override** in ui_tokens.py — makes the missing/invalid-tokens fail-closed path e2e-testable (design failure-mode register item 1) and aids portability.

No commit made (per implement contract; pipeline `git_commit: true` handles artifacts).
