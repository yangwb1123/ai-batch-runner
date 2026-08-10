Requirements specification complete and fully verified.

**Deliverable:** `docs/auto/requirements/checks-d4b833b0-requirements.md` (direction id `d4b833b0` from campaign state)

**Evidence verification — all 5 citations confirmed, with live-line corrections:**
- `checks/filesize.py:54`, `checks/complexity.py:122`, `checks/style.py:61` — the three `root.rglob(...)` scan sites (analysis cited :57/:94/:62; shifted by file evolution)
- `engineering.yaml:8-11` — "quality rules apply to the code we author here" scope comment ✓
- `checks/__init__.py:1-7` harness purpose ✓

**Measurement claims reproduced live:** 294 `.py` scanned (analysis said 293 — noted as timing delta), 102 under `build/`, 3 under `dist/`.

**Two verified refinements folded into the spec (scope unchanged):**
1. Build copies are at `build/lib/pbatch/`, not `build/pbatch/` — the substring-accident mechanism still holds (988-line `pipeline.py` and 969-line `runner.py` are covered only by `"pbatch/pipeline.py"` substring matches). Spec therefore excludes by directory segment name, not literal path.
2. "All four gates" is inaccurate — `root_files.py` uses `iterdir()` (line 21), no rglob; only the three rglob gates are in scope, matching the acceptance criteria. Explicitly pinned in §4.

**Exposure confirmed live:** `dist/design-intelligence-pack/check-*.py` (264/317/288 lines) is scanned with zero ignore/exemption coverage — green only because under budget; a regenerated artifact over 500 lines fails `make ci` nondeterministically.

**Spec structure:** summary → evidence table with live measurements → root-cause (scan discovery has no directory-scope filter; substring ignores are content-based, not scope-based) → scope boundary (out of scope: root_files gate, direction #2, engineering.yaml changes, signature changes) → 5 requirements (shared scan helper, segment-based exclusion set `build/dist/*.egg-info/.git/__pycache__/logs`, determinism, compatibility, hermetic unittest tests via module-namespace monkeypatch) → 3 supplied acceptance checks preserved and made testable with negative controls (600-line file under `build/` ⇒ `run()==0`, same file under `src/` ⇒ `run()==1`) → test plan table → appendix with the measured scan inventory.

**AC3 verified as current baseline:** `python cli.py check` exits 0; `python -m pytest checks/` = 37 passed.
