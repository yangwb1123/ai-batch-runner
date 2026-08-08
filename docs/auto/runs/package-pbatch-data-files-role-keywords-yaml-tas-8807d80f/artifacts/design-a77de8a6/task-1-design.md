Design complete: `docs/auto/runs/package-pbatch-data-files-role-keywords-yaml-tas-8807d80f/artifacts/design-a77de8a6/task-1-design.md`

## Verification results (evidence treated as untrusted, all re-checked live)

**Confirmed by reproduction:**
- Wheel gap — 102 `.py` modules, 0 data files (`pip wheel` + zip inspection)
- Sdist gap — 169 members, 0 YAML (`build_meta.build_sdist` + tar inspection)
- AC-2 discriminator — fresh venv from `/tmp`: `load_role_keywords()` → `{}`; from repo root: **34 roles** (cwd-on-`sys.path` false-pass, exactly as claimed)
- AC-3 non-discrimination — `classify '生成登录页 tsx'` prints byte-identical `route to frontend pipeline: True` on the broken wheel
- AC-4 script exits 1 on current repo

**Two evidence claims corrected:**
1. "YAML mirrors defaults exactly" — actually YAML is a strict *subset* (zero extra terms; defaults have 3 terms absent from YAML: `refactoring`/`重构代码`/`admin`). Naive equality fails; parity must be *merged == expected*.
2. `"AssertionError: role keyword map empty"` — that string exists nowhere in the repo; the assert is the acceptance command's own.

## Design highlights

- **Fix**: one 8-line `[tool.setuptools.package-data]` block (`pbatch = ["*.yaml", "*.md"]`), keyed to import name `pbatch` (not distribution name) — **empirically validated in a scratch project** that this alone populates both wheel and sdist; no `MANIFEST.in`, no loader changes, no version bump, no new deps.
- **API changes**: none; data files become part of the installed artifact; `Path(__file__).with_name(...)` loaders keep working.
- **Constraints**: setuptools≥61 already declared; editable installs unaffected; pre-fix wheels stale (rebuild); `wheel` pkg absent locally → in-process `build_wheel` tests need `importorskip`, so the deterministic offline regression test uses `build_sdist` (same manifest machinery).
- **Failure modes**: F1–F8 table — silent-empty packaging on wrong key, PyYAML-confounded venv check, repo-root false-pass, non-discriminating literal AC-3, glob gaps, residual runtime silence (deferred).
- **Migration**: config-only; rebuild → reinstall via pipx/uv; single-block rollback.
- **Acceptance mapping**: AC-1 (wheel members), AC-2 (neutral-dir venv assert, with `cd /tmp` mandated), AC-3 restated as content-parity (`diff` on installed vs repo file) + CLI-parity guard, AC-4 (exact sdist script, exit-1 pre-fix proven), plus `tests/test_packaging.py` regression tests and `make ci`/`quality.py --strict` gates.
