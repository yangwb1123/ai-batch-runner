Prototype validates the design semantics exactly: the six excluded kinds are pruned at any depth (`subproject/build/x.py` excluded), while `src/build-tools/x.py` and `mylogs/x.py` are kept (no false exclusion), and output is sorted.

## Summary

**Evidence verification — all claims in the requirements evidence confirmed live** (see §0 of the design): the three rglob sites at exact lines, the scope comment, 294/102/3 scan counts, 988/969-line build copies with substring-accident coverage, `root_files.py` iterdir line 21, `dist/` artifacts with proven zero ignore/exemption coverage, and AC3 baseline (exit 0 / 37 passed / check-test exit 0). Two new findings folded in: `build/` is not gitignored, and `checks/README.md` is the wrong anchor for R5.

**Design** (`docs/auto/design/checks-d4b833b0-design.md`):
- **API**: new `checks.config.scan_files(root, suffix=".py", exclude_segments=DEFAULT_EXCLUDED_SEGMENTS, exclude_suffixes=(".egg-info",))` — one `os.walk` pass, pruning excluded segments *before* descent, sorted absolute Paths. Constants `DEFAULT_EXCLUDED_SEGMENTS = ("build", "dist", ".git", "__pycache__", "logs")`. Three internal call sites switch from `root.rglob(...)`; nothing else changes.
- **Compatibility**: identical ordering/path shape/output for authored files; explicit `files` lists bypass discovery unchanged; existing 37 tests untouched (none call bare `run()`); `engineering.yaml` patterns keep their original meaning.
- **Failure modes**: 9 documented (false-green on authored `build/`-named dirs → 189-file inventory tripwire; symlink tightening; case-variant evasion; double-exclusion confusion; future dir kinds → one-line tuple extension, etc.).
- **Migration**: M0 baseline → M1 helper (37 still pass) → M2 wire gates → M3 tests (41 pass) → M4 inventory = 189 → M5 `make ci` + `quality.py --strict` → M6 optional doc line → M7 out-of-scope `.gitignore` follow-up.
- **Acceptance mapping**: AC1 → `test_build_dir_excluded_from_run` with negative control (`build/` ⇒ `run()==0`, `src/` ⇒ `run()==1`); AC2 → one fixture test per gate asserting `scan_files(tmp)` contains exactly `src/ok.py`; AC3 → `cli.py check` exit 0, 41 tests pass, `make ci` green.
