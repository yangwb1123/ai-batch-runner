Requirements specification delivered: `docs/requirements/scripts-check-no-refusal-regression.md`

## Evidence verification results (all direction claims confirmed)

| Claim | Result |
|---|---|
| `scripts/check-no-refusal.py` implements fail-closed refusal gate | ✅ exit 0/1/2 contract, 10 markers verified (`scripts/check-no-refusal.py:1-44`) |
| Byte-identical duplicate at `examples/snaplink-platform/scripts/` | ✅ `diff` = IDENTICAL, both 1414 bytes |
| Zero test coverage (tests/, checks/) | ✅ grep: no matches |
| Not registered in pi-batch.yaml validators | ✅ 23 entries, no check-no-refusal (`pi-batch.yaml:122-180`; cited `:128` is a comment line — substance holds) |
| Load-bearing in 8 `validate_cmd` sites, 4 pipelines | ✅ audit-revision:37,56,72,93,112 + audit-fix:38 + requirement:44 + audit-v21:41 |

Two additional findings that shaped the spec:
- **`pi-batch check` doesn't enumerate the validator registry** (`pbatch/selfcheck.py:39-55`) — the "listed by `pi-batch check`" acceptance requires a small selfcheck extension, which the spec now defines as FR-4 (fail-closed `refusal` step, `--steps refusal` filterable, end-to-end tested).
- **The example copy has zero documented consumers** — `{cwd}` resolves to the canonical copy when pipelines run from repo root (per README + `runner.py:487`), so the spec selects the **deletion branch** of acceptance #2, with the byte-identity fallback defined in §6.1 if reviewers reject deletion.

## Spec structure
- **FR-1** `tests/test_no_refusal.py`: markers derived from script source via `runpy` (auto-sync, ≥10 purge guard), empty/whitespace/missing → exit 1, clean → 0, usage → 2, plus REFUSAL-CHECK output pin
- **FR-2** delete duplicate + tests asserting absence and all 8 pipeline references resolve to the canonical copy
- **FR-3** register `refusalcheck` validator, asserted via `pbatch.config.VALIDATORS`
- **FR-4** `pi-batch check --steps refusal` step (registration + smoke test, fail-closed), asserted end-to-end in the test file
- **DOC-1** AGENTS.md validator-count line update
- §5 concrete test matrix, §7 verification commands, §8 risk register — all three supplied acceptance checks preserved verbatim as A1.x/A2.x/A3.x and made testable

Scope is deliberately contained: gate semantics frozen (NG1), stale `config` validator untouched (NG2), no pipeline rewiring (NG3).
