Review complete — written to `docs/auto/runs/unify-ui-token-single-source-of-truth-radius-4-8-a866d728/artifacts/adversarial_review-9c87f3a7/meta/devops_engineer.md`. Summary:

## 1. Deployment paths & artifacts
- **Repo-checkout gate use** — ✅ works (re-verified: registered `uigeometry` command → 8px exit 0 + `geometry.json`; 10px exit 1 + report + dynamic `{4,8,12}` message).
- **Portability copy** (AGENTS.md: pi-batch.py + pbatch/ + pi-batch.yaml) — ❌ `scripts/` and `ui-specs/tokens.json` are not in the copy set; **`check-ui-spec.py` now hard-exits 2 at startup in all modes without tokens.json** (verified) — a delivery regression introduced by this change.
- **pip-installed CLI** — ❌ wheel (111 members) contains **no scripts/, no ui-specs/, no tokens.json** (verified); `pi-batch ui-geometry` → "check-ui-geometry.py 未找到".
- No `ops/deploy/`, no tags, no release process, no CI wheel build; `dist/` holds only an unrelated pack; `build/` is stale (still documents `{6,10,16}`).

## 2. Pipeline table (key rows)
| Stage | Evidence | Gap | Proposed gate |
|---|---|---|---|
| Enforcement flip | A1/A3 + 17/17 tests pass | — | keep |
| Recurrence guard | **F2 open**: registry suite still omits `uigeometry` in all 3 tests | exact hole that let `-o` ship in 081ac29 | add to names/empty-dir/violating loops, red-first |
| Delivery assets | wheel lacks tokens.json | undeclared runtime dep | `test_packaging.py` wheel-content + venv smoke |
| Local acceptance A4 | `quality.py --strict` OK on changed files | **`make ci` red**: untracked `pbatch/cli_batch.py` 501 lines | resolve ownership |
| Doc claims | flips done | README "eval 回归 40 例" still false | correct / add real eval case |

## 3. Top findings
- **Critical** — `make check-filesize` FAILS (untracked `cli_batch.py`, 501>500) → A4 acceptance blocked, exogenous.
- **High** — F2 recurrence guard absent (verified); undeclared tokens.json runtime asset breaks portability + installed contexts (verified exit 2).
- **Medium** — retry loop gets counts only on stderr, details on stdout (verified); cwd/`UI_TOKENS_PATH` fallback can silently change enforcement (degenerate families untested); no version identity for a breaking flip.
- **Low/Info** — stale `build/`, hardcoded `/home/u1/...` in `completion_go`, A3 approximates the yaml command, `8.5px` silent pass (F4) unpinned, implementation uncommitted (provenance gap).

## 4. Release/rollback
Preflight: clean tree, committed implement, registry test red-first, wheel-content decision (U1). Migration: doc flips in same commit (done in-tree; grep-clean), pre-flip `--json` inventory of 6/10/16, exemptions untouched. Probe: exact registered command on 8px (exit 0) and 10px (exit 1) fixtures + 24 registry/geometry tests. Rollback pre-commit = atomic discard (8 files + 1 removal); post-commit = `git revert` (A1/A2 pins go red as designed). No state/data migration; reports gitignored.

## 5. Blocking decisions
`ops/deploy/` doesn't exist (no deployment automation at all); U1 wheel support matrix; version tagging; retry-feedback contract; F4 precision edge; cli_batch.py ownership; commit the implementation before acceptance.
