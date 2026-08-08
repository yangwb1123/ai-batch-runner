QA lead review complete — written to the campaign artifact path `.../adversarial_review-9c87f3a7/meta/qa_lead.md`.

## Summary

**Critical context**: the tree mutated *during* my review — the design's implementation (scripts/ui_tokens.py, both script edits, 17 tests) appeared at 05:35–05:41 from an unattributed writer, while this run's `implement` stage hasn't executed yet. All findings are timestamped against the state measured.

**Verified** (14 commands, two tree states):
- **B1 reproduced**: registered `uigeometry` command exits 2 (`unrecognized arguments: -o`) — no `geometry.json` in `docs/ui/refactor/`; post-fix e2e from `/tmp` confirms exit 1 + `-o` report + dynamic `{4,8,12}` message, cwd-independent.
- **Post-implementation suite**: 17 geometry tests pass, 42 evals pass, `quality.py --strict` OK.
- **A4 blocked**: `make ci` is red on `pbatch/cli_batch.py` (501 > 500 lines, untracked), plus 20+ CLI tests fail with exit 5 while any campaign holds the lock — acceptance cannot pass in this environment.

**Top findings**:
1. **High** — A4 (`make ci`) unachievable as stated (F1); implement stage will re-run against an already-implemented tree → churn/vacuous-gate risk (F2).
2. **Medium** — retry-loop feedback contract broken: runner feeds **stderr only** into retry prompts, but geometry prints violation *details* to stdout (only the count reaches the agent) — undermines the fail-closed loop B1 is meant to repair. Sibling gates all use stderr.
3. **Medium** — cwd-tokens fallback (a foreign repo's own tokens.json silently governs) and non-px/missing-family fail-closed branches are implemented but untested; `UI_TOKENS_PATH` makes them cheap to pin.
4. **Low** — README:600 "eval 回归 40 例" is stale (no eval case executes the gate); stale `build/lib` copy still hardcodes {6,10,16}.

**Deliverables**: full requirement→test matrix (A1/A2/A3/B1 all mapped and passing), 18 prioritized scenarios, and concrete test code + acceptance assertions for each gap (stderr-feedback, discovery fallback, no-frontend exit 0, non-px tokens, multi-value radius, `!important`, dynamic label, yaml-command parsing).
