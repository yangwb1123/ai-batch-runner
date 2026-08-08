Design complete. Written to `docs/auto/runs/close-wheel-testing-ci-gap-and-clean-up-stale-bu-ab5559b2/artifacts/design-a77de8a6/task-1-design.md` (the pipeline's design-stage artifact path).

## Verification summary — all evidence claims confirmed

| Claim | Result |
|---|---|
| `test_wheel_ships_data_files` at 207–229, `importorskip("wheel")` at 214 (line refs 318–341 stale) | ✅ confirmed |
| Docstring at 27–29 (not 34–36) | ✅ confirmed |
| dev extras = pytest/pytest-cov only; `Makefile:38` is the `test:` target (line 37), `ci:` at 47 | ✅ confirmed |
| `.gitignore` has `*.egg-info/` + `dist/`, no `build/`; `check-ignore` rc=1 | ✅ confirmed |
| SOURCES.txt = 168 lines, 55 `tests/` entries; `?? build/` in porcelain | ✅ confirmed |
| Live repro: 6 passed/1 skipped; wheel & build unavailable | ✅ reproduced |

**New facts established:**
- `.github/workflows/ci.yml` runs `pip install -e ".[dev]"` then `make test` — the concrete CI gap; fixing dev extras propagates with **zero workflow changes**
- **Fix empirically validated**: after installing `wheel>=0.42 build>=1.0` → `7 passed` (0 skipped), no test-code changes needed
- **Hypothesis rejected by experiment**: the stale `build/` dir does *not* shadow an installed `build` package (regular package wins over namespace portion on 3.10) — documented as verified-non-issue so the implementer adds no workaround

## Design highlights

- **API changes**: no public API — 2 manifest edits (`dev = [..., "wheel>=0.42", "build>=1.0"]`, `/build/` root-anchored in `.gitignore`) + deletion of untracked `ai_batch_runner.egg-info/` and `build/` (build/ already removed during validation)
- **Compatibility**: Python ≥3.9 satisfied by both floors; runtime dep set untouched; Makefile/CI/tests unchanged
- **8 failure modes documented** (F1 silent-skip recurrence → AC-1 guard; F3 egg-info live-tree leak → deletion + minimal-copy build pattern; F4 unanchored-gitignore over-ignore → `/build/` anchoring; F7 shadowing → verified non-issue)
- **Migration**: 8 idempotent steps ending in fresh-venv smoke test
- **Acceptance mapping**: AC-1..AC-4 each bound to an exact command/assertion; regression net (all 7 packaging tests) mapped explicitly
- **Scope guardrails**: out-of-package assets, version identity, build isolation, Makefile/CI all explicitly deferred
