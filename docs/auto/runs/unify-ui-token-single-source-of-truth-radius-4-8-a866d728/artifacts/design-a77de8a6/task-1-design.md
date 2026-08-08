Design written to `docs/auto/runs/unify-ui-token-single-source-of-truth-radius-4-8-a866d728/artifacts/design-a77de8a6/task-1-design.md`.

**Verification outcome** — all 9 evidence claims confirmed (two line-number corrections by the requirements stage were themselves correct; spacing.md rows off by one, immaterial). Crucially, execution testing surfaced **two defects the evidence missed**:

1. **`pi-batch.yaml:165` passes `-o` to `check-ui-geometry.py`, which has never supported it** → argparse exits 2 → the registered `uigeometry` validator has failed *every* run since commit `081ac29` (corroborated: `docs/ui/refactor/` has `report.json`/`quality.json` but no `geometry.json`). Folded into the design as B1 — without it, A3/A4 cannot hold.
2. **`README.md:595,600`** hardcode `{6,10,16}` and falsely claim the ui-refactor pipeline carries uigeometry (`examples/ui-refactor-pipeline.yaml:51` uses only `uicheck`) — doc-only corrections.

**Design core** — decision upheld: tokens.json wins, radius family resolves to **{4,8,12}** (4pt-grid aligned; component-spec.md:4 already binds to tokens.json):

- **API**: new `scripts/ui_tokens.py` (shared, fail-closed loader — avoids quality.py's duplicate-body detection); `check-ui-geometry.py` derives `RADIUS_FAMILY` lazily (exit 2 on missing/unparseable tokens.json, dynamic error message) + additive `-o` flag; `check-ui-spec.py` derives `SPACING_TOKENS` (value-identical today, zero behavior change). Validator names/yaml/exit codes unchanged.
- **Compatibility**: radius flip is the one breaking change (6/10/16 → violations, 4/8/12 → compliant, matching component-spec 8/12); exemptions (50%/999px/0/var()) and stroke family untouched; no-frontend repos still pass.
- **Failure modes**: 8-item register — fail-closed on missing/invalid tokens, drift caught by pinned tests, `-o` parity with check-ui-spec, pre-existing wheel-install limitation documented.
- **Migration**: 9 ordered test-first steps (step 2 deliberately red — the fixture flip *proves* enforcement reversed), rollback per commit-sized hunk.
- **Acceptance mapping**: A1→`test_radius_family_matches_tokens_json`, A2→`test_spacing_tokens_match_tokens_json`, A3→fixture flips + exact registered-command e2e on an 8px fixture (exit 0), A4→`make ci` + `quality.py --strict`, B1→`test_report_output_flag` + e2e. Typography/component gates (directions #2/#3) explicitly out of scope.
