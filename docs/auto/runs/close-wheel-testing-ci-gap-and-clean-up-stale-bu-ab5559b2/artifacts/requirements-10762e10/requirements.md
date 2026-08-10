Requirements specification produced at `docs/auto/runs/close-wheel-testing-ci-gap-and-clean-up-stale-bu-ab5559b2/artifacts/requirements-10762e10/requirements.md`.

## Evidence verification summary

Every cited file/symbol was checked against the repo:

| Citation | Result |
|---|---|
| `tests/test_packaging.py:318-341` | ⚠️ **Line numbers stale** — test is at **207–229** (`importorskip("wheel")` at 214); file is only 229 lines. Behavior confirmed. |
| `tests/test_packaging.py:34-36` docstring | ⚠️ Lines shifted — the "wheel is a dev extra follow-up (campaign direction #2)" statement is at **27–29**. Confirmed. |
| `pyproject.toml` dev extras | ✅ `dev = ["pytest>=8.0", "pytest-cov>=5.0"]` only (lines 15–18) |
| `Makefile:38` | ✅ Recipe `python -m pytest tests/` — but of the **`test:`** target (line 37); `ci:` at line 47 depends on it transitively. Corrected attribution in spec. |
| `.gitignore` | ✅ Lists `*.egg-info/` and `dist/`; **no `build/` rule**; `?? build/` confirmed untracked in `git status` |
| `ai_batch_runner.egg-info/SOURCES.txt` | ✅ Stale 168-line live-tree manifest with 55 `tests/` entries, confirmed |

## Live reproduction (matches direction claims)
- `python -m pytest tests/test_packaging.py -q` → **6 passed, 1 skipped** ✓
- `import wheel` → `ModuleNotFoundError`; `python -m build` also unavailable (relevant to AC-1's "installs wheel and build") ✓
- `git check-ignore build/` → no rule (rc=1) ✓

## Spec contents
- **R1**: add `wheel` + `build` to dev extras (closes the silent-skip CI gap)
- **R2**: add `build/` to `.gitignore` + delete stale `ai_batch_runner.egg-info/` (regenerated on demand; its stale SOURCES.txt is the live-tree-leak failure mode `test_sdist_isolated_from_live_tree_state` guards against)
- **All 4 acceptance criteria preserved** and made testable (fresh-env pytest count 7/0, `make ci` no-skip, `git check-ignore`/`--porcelain`, fresh-venv `pip install .` + `pi-batch classify --json "写一个后端接口"` console-script smoke)
- **Scope guardrails**: out-of-package assets (direction #0), version identity (direction #2), and build-isolation machinery explicitly excluded — no expansion beyond the direction
