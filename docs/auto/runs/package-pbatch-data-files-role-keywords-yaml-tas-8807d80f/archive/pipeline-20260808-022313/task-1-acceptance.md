All verification complete. Summary of the acceptance gate:

## Acceptance check verification (all re-run live, independently)

| Check | Requirement | Result |
|---|---|---|
| **AC-1** wheel | `pip wheel . --no-deps -w /tmp/w` → wheel contains `pbatch/role_keywords.yaml` + `pbatch/task_keywords.yaml` | ✅ **PASSED live**: wheel has 102 `.py` + all 3 data files (`role_keywords.yaml`, `task_keywords.yaml`, `README.md`) |
| **AC-1** sdist | data files in sdist | ✅ **PASSED live**: all 3 in `ai-batch-runner-0.1.0/pbatch/` (173-member live build; test asserts exact names, not counts) |
| **AC-2** | fresh venv install → `load_role_keywords()` non-empty from neutral dir | ✅ **PASSED live**: `/tmp/v` venv, run from `/tmp` (no cwd-on-sys.path false-pass; installed path confirmed in `site-packages`) → **dict of 34 roles** |
| **AC-3** | `classify '生成登录页 tsx'` venv == repo-tree; restated as content parity | ✅ **PASSED live**: byte-identical output (`frontend_ui`, score 6, route True); installed YAMLs `cmp`-identical to repo copies |
| **AC-4** | `build_sdist` inspection script exits 0 | ✅ **PASSED live**: exit 0, all data files present |
| **Discrimination** | acceptance must fail pre-fix | ✅ **Proven**: package-data block removed in isolated copy → sdist ships **0 YAML** (same script would exit 1) |
| **Regression** | `tests/test_packaging.py` | ✅ 6 passed, 1 skipped (wheel absent locally — documented `importorskip`, AC-1 wheel half live-verified via pip instead) |
| **Regression** | full suite | ✅ 932 passed, **19 failed — all `exit 5` lock-contention** (`Another pi-batch instance is running pid=1684832`, 14 live campaign processes confirmed); 0 packaging failures; failure mode cannot be affected by a pyproject block; evidence records same 19 passing on pristine HEAD checkout |
| **Gates** | `quality.py --strict .`, `cli.py check/check-test`, `pytest checks/` | ✅ OK / rc=0 / 37 passed |

**Footprint verified against design gate conditions**: pyproject.toml is a +9-line `[tool.setuptools.package-data]` block with explicit list (no globs), keyed to import name `pbatch`; `tests/test_packaging.py` = 7 tests with exact-member assertions, isolated tmp_path builds, wrong-key fail-closed test, and loader-audit invariant; no loader changes, no new deps, no version bump, no MANIFEST.in. Deferred surfaces (CI packaging gate, version identity) are explicitly documented with owners as separate campaign directions.

VERDICT: PASS - All four acceptance checks are satisfied by live reproduction (wheel and sdist both ship role_keywords.yaml/task_keywords.yaml/README.md, a fresh venv from a neutral directory loads a 34-role keyword map, classify CLI output is byte-identical between installed and repo-tree runs with installed YAML byte-identical to the repo, and the sdist inspection script exits 0), the acceptance is proven discriminating by a pre-fix simulation that ships zero data files, the new regression suite passes 6/7 with the wheel skip documented by design, and the only full-suite failures are environmental repo-lock contention from live 7×24 pipelines, none touching packaging.
