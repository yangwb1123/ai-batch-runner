All gates complete. No commit made (per instructions). Here is the change report.

---

# Change Report — direction `package-pbatch-data-files-role-keywords-yaml` (task-1)

## Domain manifest (required first step)

`pi-batch assess "<direction title> + problem"` → **standard → demo 处方档 (0 prescribed rules), workflow L0_direct, productization L0**. This is an infrastructure/packaging direction — neither backend-specs/ nor ui-specs/ manifest was selected by the auto-domain routing, and **no persistence-design gate applies** (the change touches packaging config, not core data). The assess output's mandatory evidence (`impact_map, rollback_plan, test_pass`) is satisfied below.

## Files changed (exactly the approved footprint)

| File | Change |
|---|---|
| `pyproject.toml` | +9 lines: `[tool.setuptools.package-data]` with **explicit list** `pbatch = ["README.md", "role_keywords.yaml", "task_keywords.yaml"]`, keyed to import name `pbatch` (design gate condition 1: no globs — fail closed on future files) |
| `tests/test_packaging.py` | New, 7 tests (below). Only new/untracked test file from this task (other untracked tests pre-date this task) |

No loader changes, no new deps, no version bump, no MANIFEST.in (YAGNI per design). All other dirty-tree files (`pbatch/*.py`, `Makefile`, …) were pre-existing parallel-campaign edits, untouched by me.

## Symbols touched

- `pbatch.relevance.load_role_keywords` / `pbatch.classifier.load_keywords` (loaders) — **unchanged**, still resolve via `Path(__file__).with_name(...)`; data now ships to the installed package so they find the files.
- New: `tests/test_packaging.py` — helpers `_loader_data_refs`, `_minimal_repo_copy`, `_build_sdist`, `_tar_names`; module-scoped fixture `sdist_file`.

## Regression tests (focused, `tests/test_packaging.py`)

1. `test_sdist_ships_declared_data_files` — AC-1 sdist half, **exact member names** (`pbatch/role_keywords.yaml in names`), never counts/substrings (QA F-QA1)
2. `test_loader_referenced_data_files_ship` — code-arch F1 invariant: every `with_name("*.yaml"/"*.md")` ref in `pbatch/*.py` must ship (≈15-line static audit)
3. `test_sdist_data_files_byte_identical_to_repo` — AC-3 restated parity (installed == repo bytes)
4. `test_sdist_isolated_from_live_tree_state` — guards the cwd build trap (below)
5. `test_wrong_package_data_key_fails_closed` — QA F-QA2: `ai_batch_runner` typo must ship 0 data files
6. `test_package_data_declaration_covers_loader_refs` — static half of F1 + F4 (declaration ⊇ loader refs, no `*` in block)
7. `test_wheel_ships_data_files` — AC-1 wheel half, `importorskip("wheel")` (wheel pkg absent locally; sdist proxy is the deterministic offline gate — design-documented, not a silent skip hazard)

**Red run captured (M0, pre-fix): 5 failed** — all data-file assertions red exactly as the gate required. **Post-fix: 6 passed, 1 skipped.**

## Command results

| Gate | Result |
|---|---|
| `pytest tests/test_packaging.py` | ✅ 6 passed, 1 skipped (wheel, by design) |
| `make ci` (check + self-test + test + quality + org) | ⚠️ 932 passed, **19 failed, all lock-contention**: every failure is `exit 5 … Another pi-batch instance is running (pid=1684832 … Refusing to start)` — **3 live 7×24 campaign pipelines hold the repo lock**. Proven environmental: the same 19 tests **pass 185/185 on a pristine HEAD checkout** (`git archive` → /tmp). None touch packaging; none can be affected by this change |
| `python cli.py check` / `check-test` | ✅ rc=0 both |
| `python quality.py --strict .` | ✅ OK |
| `pi-batch org check` / `capabilities check` / `graph extract` | ✅ valid, rc=0 (49 capabilities, 0 cycles) |
| `pytest checks/` | ✅ 37 passed |
| **End-to-end AC-1/AC-2** | ✅ fresh sdist installs into venv → 3 data files in site-packages; from neutral dir `/tmp`: `load_role_keywords()` → **34 roles** (was `{}` pre-fix) |

## Evidence-backed deviations from design

1. **Critical discovery (strengthens the suite, not the fix):** `setuptools.build_meta.build_sdist(dir)` builds from **cwd**, not the passed dir (that's the *output* dir). Building in-process from repo root silently pulled `tests/` + stale `egg-info/SOURCES.txt` (169 members vs 116 from a controlled copy) — the QA-F3/tree-state hazard, live-reproduced. Tests therefore build via **subprocess with `cwd=minimal tmp_path copy`** (pyproject.toml + README + LICENSE + pbatch only), and `test_sdist_isolated_from_live_tree_state` makes the trap fail loudly.
2. **Owner lines for deferred surface (gate condition 5)** recorded in the test module docstring: `{tool_root}` validators, `rule_matcher` ui-specs/backend-specs registries, `examples/` default pipelines, health probe, version identity → follow-up campaign directions #2 (CI packaging gate / U1 support-matrix) and #3 (version identity), per requirements non-goals.
3. Wrong-key test pre-fix failure mode is "block not found to rewrite" (asserts the block exists) — it is a post-fix regression guard by design, its red-capture role is covered by tests 1/2/6.

## Rollback plan (assessor evidence)

Single-block removal of `[tool.setuptools.package-data]` reverts the fix; loaders unchanged; editable installs unaffected; pre-fix wheels stale → rebuild/reinstall.

**Not committed** — per instructions, the change sits uncommitted alongside the pre-existing dirty tree.
