Implementation complete and committed (`7280ec0`, 8 files, +139/−7). Final report:

---

## 1. Analysis

**Module boundary**: `checks/` — the engineering harness (filesize/complexity/style gates). Scan discovery is a shared harness concern; the helper belongs in `checks/config.py`, the existing shared module, not in any single gate.

**Data ownership**: the exclusion set is configuration-level policy (constants mirroring `engineering.yaml`'s declared scope "code we author here"). Public contract: `scan_files(root, suffix, exclude_segments, exclude_suffixes) -> list[Path]`. The three gates own their per-file checks (`is_ignored`/`is_exempt`/budgets) untouched.

**Strong consistency**: N/A — pure function, no DB, no transactions, no shared mutable state. Nothing to make atomic.

**Change points**: the only thing that varies is the set of generated-dir kinds. One tuple constant (`DEFAULT_EXCLUDED_SEGMENTS` + `DEFAULT_EXCLUDED_SUFFIXES`) is the variation point; adding a new kind (FM7, e.g. `.venv/`) is a one-line tuple extension. No speculative extension beyond that.

**Dependency direction**: gates → `checks.config.scan_files` (one direction, no cycles; config imports nothing from gates). Domain logic (per-file rules) never imports scan internals.

**Pattern choice: no pattern — composition suffices.** The decision table (design-patterns.md) matches nothing here: no Strategy (one algorithm), no Adapter (no SDK), no Repository (no persistence). The actual design problem was *three duplicated scan implementations* — the third repetition, so consolidation into one helper is exactly the rule-of-three abstraction, not speculative. One function + two tuple params + the documented `os.walk` topdown prune idiom (`dirnames[:] = [...]`) is the simplest correct solution.

**Persistence Design report (mandatory gate)**: **N/A — this change creates no tables, ORM entities, or schema.** No identity/keys/money/indexes/audit decisions apply. The only "persistence" is the filesystem walk, whose determinism contract (sorted order, absolute paths, no symlink descent) is documented in the docstring.

## 2. Implementation (per design M1–M6)

- `checks/config.py`: `DEFAULT_EXCLUDED_SEGMENTS = ("build", "dist", ".git", "__pycache__", "logs")`, `DEFAULT_EXCLUDED_SUFFIXES = (".egg-info",)`, and `scan_files()` — one `os.walk` pass, prunes excluded segments **before descent**, `followlinks=False` (symlink tightening, FM2), sorted result (byte-identical ordering to `sorted(rglob(...))`), segment-name matching (`src/build-tools/x.py` and `mylogs/x.py` kept, `subproject/build/x.py` excluded — R2 pinned).
- `checks/filesize.py`, `checks/complexity.py`, `checks/style.py`: `run()` discovery switched to `scan_files`; explicit `files` bypass unchanged; no signature changes.
- Tests (41 total, +4): AC1 `test_build_dir_excluded_from_run` (600-line file under `build/` ⇒ `run()==0`; moved to `src/` ⇒ `run()==1` — negative control proves exclusion, not a broken budget); AC2 `test_run_scan_excludes_generated_dirs` in all three suites (six excluded kinds incl. `vendor/pkg.egg-info/` at depth; `run()==0` **and** `scan_files(tmp) == [src/ok.py]`). Filesize AC2 also pins the 600-line-under-`logs/` representative.
- `engineering.yaml`: R5 header note (the design-confirmed correct anchor — `checks/README.md` documents the harness suite, not scan discovery).

## 3. Self-check vs architecture.md §8 / evolution.md §6, and verification

**architecture.md §8 / evolution.md §6 audit**: module boundary clear (scan discovery owned by config, one change reason); dependency direction single; no god classes/files (helper ~30 lines); no state machines (stateless pure function — §8 N/A); no DTO/domain/ORM mixing; no meaningless one-implementation abstraction (helper has three real call sites); no scattered `if` expressing policy; no cross-module internal access; gates remain independently testable; change radius minimal (no signature/public-surface changes, no config pattern edits).

**Commands actually run** (all from repo root):

| Command | Result |
|---|---|
| Baseline inventory: 294 scanned / 102 `build/` / 3 `dist/`; `git ls-files -s` symlinks | ✅ verified (0 symlinks) |
| `python -m pytest checks/` | ✅ **41 passed** (37 existing unmodified + 4 new) |
| `python cli.py check-test` | ✅ exit 0 |
| `python cli.py check` | ✅ exit 0; **0** build/dist paths; WARN lines diffed **byte-identical** vs baseline |
| Post-fix inventory: `scan_files(repo_root())` | ✅ **189 files**, 0 paths under excluded segments |
| `python quality.py --strict .` | ✅ exit 0 |
| `make ci` | ⚠️ exit 2 — **19 failures, all pre-existing and environmental**: `Another pi-batch instance is running (pid=1684832…)` lock refusal in `tests/test_pi_batch.py`/`test_local_fixes.py` subprocess tests. **Proven pre-existing**: with my changes stashed, `test_max_rounds_loop_reruns_failures` fails identically (live campaigns hold the `.pi-batch.lock` — including one from another repo, `/home/u1/iris-ui`). None in `checks/`. |

**Could not run**: a clean `make ci` (blocked by the live-campaign lock — requires the 7×24 campaign to finish or `--no-lock`-style isolation; failure mode reproduced on baseline, so no risk is attributed to this change). M7 (`.gitignore` for `build/`) intentionally **not** done — out of scope per design, recorded as follow-up in the commit message.

**Failure modes covered**: FM1 false-green tripwire (189-file inventory pinned in tests via exact `scan_files` equality), FM2 symlink tightening (verified no tracked symlinks), FM3 explicit-list semantics (unchanged, existing test `test_run_counts_failures` still passes), FM4 case variants (documented by design), FM5 double-exclusion (additive — patterns untouched), FM6 no runtime regression (pruning strictly reduces walk), FM7 new dir kinds (one-line tuple extension).
