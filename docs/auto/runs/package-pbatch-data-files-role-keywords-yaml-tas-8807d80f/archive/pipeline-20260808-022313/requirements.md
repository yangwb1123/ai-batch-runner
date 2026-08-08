Done. The evidence-backed requirements specification is written to the pipeline's expected output path:

**`docs/auto/runs/package-pbatch-data-files-role-keywords-yaml-tas-8807d80f/artifacts/requirements-10762e10/requirements.md`**

## What was verified (all cited evidence confirmed live)

| Evidence | Result |
|---|---|
| `pyproject.toml:25` | ✅ `[tool.setuptools.packages.find]` only — no `package-data`, no `MANIFEST.in` |
| `relevance.py:51,54-68` | ✅ `Path(__file__).with_name("role_keywords.yaml")`; `except Exception: continue` → `return {}`; **no built-in fallback** (severest degradation) |
| `classifier.py:136,148-157` | ✅ Same pattern; additive merge over `_DEFAULT_KEYWORDS` — YAML contributions silently dropped |
| YAML/README files | ✅ All three exist in `pbatch/` |
| **Wheel gap** | ✅ **Reproduced**: 102 `.py` modules, **0 data files** |
| **Sdist gap** | ✅ **Reproduced**: 0 YAML files in freshly built sdist |

## Key findings baked into the spec

1. **AC-2 is the true discriminator** — `relevance.py` has no fallback map, so the fresh-venv `load_role_keywords()` assert fails with `AssertionError: role keyword map empty` on the current broken wheel (verified live).
2. **AC-3 calibration** — `_DEFAULT_KEYWORDS` already contains `登录页`/`tsx` and the YAML mirrors defaults exactly (zero extra terms, verified by diff), so AC-3 as literally supplied is non-discriminating; the spec restates it as a precise install-integrity parity check.
3. **Testability flaw found and fixed** — venv commands run from the repo root false-pass via cwd-on-`sys.path` repo import (verified live: same command returns `{}` from `/tmp` but `34 roles` from repo root). Spec now mandates neutral-directory execution.
4. **AC-4 added** for the sdist half of the direction title (`setuptools.build_meta.build_sdist` inspection, exact script proven to exit 1 on current repo).

Scope is preserved: requirements FR-1…FR-7 are packaging-config-only (no loader changes, no new deps, no version bump), and the non-goals section explicitly defers the CI packaging gate (direction #2) and version identity (direction #3) to their own campaign directions.
