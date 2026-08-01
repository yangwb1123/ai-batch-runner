Plan written to `docs/IMPLEMENTATION_PLAN_T0_CLOSEOUT.md`. Every QA finding it acts on was re-verified in this session first.

## Verification performed (evidence ledger)

- `make ci` green — 118 passed (9.46s), quality OK
- `--cov=pbatch`: runner.py **75% (211/280)** — missing-line list matches the review exactly (247, 254–255, 276/278, 296–297, 312/314)
- **QA-5 confirmed live**: `run_validation(["echo","hello"])` → `ok=True, stdout='\n'`; `""` → `ok=True`
- **QA-1/QA-2 confirmed in code**: `revalidate_existing` (runner.py:286–320) has no symlink check; `save_result` (188–190) uses fixed `.tmp` + follow-symlink `write_text` + `rename`
- **T1 unwired confirmed**: `_filter_reused` existence-only (cli.py:370); zero production callers of `revalidate_existing`; all six reuse sites enumerated (pipeline.py:146/352/381/412, cli.py:370, `execute_stage` short-circuit 452–457)
- Existing validator tests byte-untouched (diff = appends only at 774+)

## Plan shape (3 phases)

**M0 — T0 close-out (this revision, 3 commits, before any T1 commit)**
- **T0.1**: QA-1 symlink rejection + QA-2 mkstemp in `save_result` (mirrors F14) — the review's exit criterion #1
- **T0.2**: QA-5 `TypeError` guard + security F4 bounded drain (I pulled this forward from T2 into T0: it lives inside `run_validation`, is 3 lines, removes the suite-hang risk, and unblocks the QA-7 test — flagged as a scoping decision)
- **T0.3**: QA-3a/b/c, QA-4, QA-5, QA-6, QA-7 tests + cheap branch tests to reach **runner.py ≥90%, zero uncovered lines in the three validator functions except line 247**
- **T0.4/T0.5**: `make coverage` gains `--cov=pbatch.runner --cov-fail-under=90` + `-p no:cacheprovider` (mitigation, not root-cause claim — exit-2 anomaly stays Partial); docs

**M1 — T1**: `pbatch/reuse.py::reuse_decision()` unifying all six reuse paths; stale ⇒ delete+regenerate; `--reuse-legacy` escape; QA-1 fix must precede (review's "must land with the F1 fix")

**M2 — T2**: validator feedback; consumes T0.2's partial-timeout output

## Key risks carried

- **R-1 TOCTOU**: symlink check-then-execute race — acceptance bar is the review's (reject symlinks, victim unchanged); fd/`O_NOFOLLOW` is the documented fallback if the threat model demands
- **R-3**: ≥90% may require closing pre-existing gaps beyond the three functions — mitigated with a short branch-test list, fallback = per-function gate + 85%
- **R-5**: decision/execution gate precedence mismatch in T1 — single helper + parity fixtures

Unknowns kept honest: pytest exit-2 anomaly (Partial), pre-commit hook execution (Partial — verification step in rollout, not a task). No capacity evidence → relative sizes only. D4 (feedback cap) is the one open decision gating T2.
