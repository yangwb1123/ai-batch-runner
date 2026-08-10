# Design — Exclude generated/artifact directories from gate scan scope (rglob pollution)

| Field | Value |
|---|---|
| Module | `checks` |
| Direction id | `d4b833b0` |
| Requirements | `docs/auto/requirements/checks-d4b833b0-requirements.md` (evidence-verified) |
| Status | Design (all evidence claims re-verified live against this repo) |
| Scope | R1–R4 in scope, R5 optional; no expansion |

---

## 0. Evidence verification (all claims re-checked live)

Every claim in the requirements evidence was reproduced against the current
tree before designing:

| Evidence claim | Verification result |
|---|---|
| `filesize.py:54` / `complexity.py:122` / `style.py:61` are the three `root.rglob(...)` scan sites | ✅ exact (`grep -n rglob checks/*.py`) |
| `engineering.yaml:8-11` scope comment ("quality rules apply to the code we author here") | ✅ exact |
| `checks/__init__.py:1-7` harness purpose | ✅ exact |
| 294 `.py` scanned; 102 under `build/`; 3 under `dist/` | ✅ reproduced (`root.rglob("*.py")` count) |
| Build copies at `build/lib/pbatch/`, not `build/pbatch/`; `pipeline.py` 988 lines, `runner.py` 969 lines | ✅ exact (`wc -l`) |
| Substring accident: `"pbatch/pipeline.py"` ⊂ `"build/lib/pbatch/pipeline.py"` | ✅ confirmed by `is_ignored()` semantics (substring match on rel path) |
| `root_files.py` uses `iterdir()` (line 21), no rglob → only three gates in scope | ✅ exact |
| `dist/design-intelligence-pack/check-*.py` 264/317/288 lines, zero ignore/exemption coverage | ✅ proven programmatically (`is_ignored=False`, `is_exempt=False` for all three) |
| AC3 baseline: `cli.py check` exit 0; `pytest checks/` = 37 passed | ✅ reproduced (also `cli.py check-test` exit 0) |
| Post-fix scan scope = 189 (294 − 102 − 3) | ✅ arithmetic + simulated exclusion |
| `run(files=None)` explicit-list bypass; existing tests never call bare `run()` | ✅ confirmed (`test_run_counts_failures` passes explicit list; no bare `run()` in test_* files) |

**New findings beyond the evidence (no scope change):**
1. `build/` is **not** in `.gitignore` (`git check-ignore build` echoes nothing;
   `git ls-files build` = 0). It is untracked-but-unignored. The gate fix makes
   this moot for CI, but a stray `git add -A` can still stage build outputs —
   tracked as an out-of-scope follow-up recommendation (§7).
2. `checks/README.md` in this repo is the *harness behavior* README (Chinese,
   "147+ 个测试守护仓库门禁"). R5's doc anchor should be the
   `engineering.yaml` header comment (as R5 already permits), not a README
   line — avoids semantically misplaced documentation.

---

## 1. Design overview

One structural change: **scan discovery moves from `root.rglob(...)` (three
call sites, no scope filter) to a single shared, pruning helper** in
`checks/config.py`. The helper returns the deterministic sorted list of
`.py` paths under a root, never descending into the generated/artifact
directory segments (`build`, `dist`, `.git`, `__pycache__`, `logs`, any
`*.egg-info`). All other gate behavior — substring `ignore_patterns`,
`exemptions`, budgets, output format, explicit `files` handling — is
untouched.

```
today:   run() → root.rglob("*.py") → walks EVERYTHING (incl. build/ 102 files, dist/ 3)
                    └─ per-file substring ignores only after the walk
after:   run() → config.scan_files(root, suffix=".py") → prunes excluded dirs BEFORE descent
                    └─ returns sorted absolute Paths, exactly the authored tree (189 files)
```

Why segment-name exclusion (not path patterns): the requirements
verification proved the current green is an accident of substring matching
(`"pbatch/pipeline.py"` matching the build copy) and of file sizes
(`dist/` artifacts under 500 lines with zero coverage). Directory-segment
matching at scan time makes the gates scope-correct regardless of file
size, content, or naming of generated trees.

---

## 2. API changes

### 2.1 New public symbols in `checks/config.py`

```python
# Directory segments excluded from gate scan discovery (matched at any depth,
# exact segment name — not substring). Mirrors engineering.yaml's declared
# scope: "quality rules apply to the code we author here".
DEFAULT_EXCLUDED_SEGMENTS = ("build", "dist", ".git", "__pycache__", "logs")
DEFAULT_EXCLUDED_SUFFIXES = (".egg-info",)   # matches ai_batch_runner.egg-info


def scan_files(root: Path, suffix: str = ".py",
               exclude_segments: tuple = DEFAULT_EXCLUDED_SEGMENTS,
               exclude_suffixes: tuple = DEFAULT_EXCLUDED_SUFFIXES) -> list:
    """Deterministic, sorted list of absolute Paths under `root` whose name
    ends with `suffix`, never descending into excluded directory segments.

    Replaces today's `sorted(root.rglob(...))` scan discovery for the three
    rglob gates. Prunes excluded directories before descending (cheap walk),
    does not follow symlinked directories, and sorts the result (same
    ordering as today's `sorted(rglob(...))`). Exclusion is decided purely
    by directory segment names relative to `root` (a `build/` outside root
    never matters). Returns absolute Paths, matching rglob-from-absolute-root.
    """
    def _excluded(name: str) -> bool:
        return name in exclude_segments or name.endswith(exclude_suffixes)

    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not _excluded(d)]  # prune (topdown)
        for name in filenames:
            if name.endswith(suffix):
                found.append(Path(dirpath) / name)
    return sorted(found)
```

Notes:
- `os` is already imported in `config.py` (used by `env_bool`).
- Pruning via `dirnames[:] = [...]` is the documented `os.walk` topdown
  idiom — excluded trees are never entered, so a 100-file `build/` tree
  costs one `scandir` call on the root segment name.
- Segment matching is separator- and case-agnostic-by-design:
  `src/build-tools/x.py` and `mylogs/x.py` are scanned (no false
  exclusion); `subproject/build/x.py` is excluded. This is the R2
  semantics pinned in the requirements.

### 2.2 Modified call sites (internal, no signature change)

| File | Today (line) | After |
|---|---|---|
| `checks/filesize.py` | `files = sorted(root.rglob(f"*{_SUFFIX}"))` (54) | `files = scan_files(root, suffix=_SUFFIX)` |
| `checks/complexity.py` | `files = sorted(root.rglob("*.py"))` (122) | `files = scan_files(root)` |
| `checks/style.py` | `files = sorted(root.rglob(f"*{_SUFFIX}"))` (61) | `files = scan_files(root, suffix=_SUFFIX)` |

Each file adds `scan_files` to its existing
`from checks.config import ...` line. The `if files is None:` guard stays —
explicit `files` lists bypass discovery entirely, exactly as today.

### 2.3 Unchanged public surface

- `run(files: list = None) -> int` in all three gates (signature, semantics).
- `check_file` / `check_tree` / `check_text` — untouched, including
  `is_ignored` (substring) and `is_exempt` logic.
- `get_config`, `repo_root`, `is_ignored`, `is_exempt`, `env_bool` —
  untouched.
- `checks/root_files.py`, `cli.py`, `engineering.yaml`, `Makefile`,
  `pytest.ini` — not modified by this direction.

---

## 3. Compatibility constraints

1. **Ordering**: `scan_files` returns `sorted(found)` — byte-identical
   ordering to today's `sorted(root.rglob(...))`. Gate output for authored
   files (including the existing `WARN:` lines) is unchanged.
2. **Path shape**: rglob from an absolute `repo_root()` yields absolute
   paths; `scan_files` returns absolute `Path(dirpath)/name`. The
   subsequent `path.resolve()` + `relative_to(root)` in `check_file`
   produces identical `rel` strings for non-excluded files.
3. **Explicit `files` lists**: `run([...])` skips discovery — existing
   tests (`test_run_counts_failures` passes an explicit list) and any CLI
   usage (`python checks/filesize.py a.py b.py` via `__main__`) behave
   identically, including for paths under excluded dirs (documented
   behavior, per requirements R3).
4. **Existing ignore patterns stay**: `"pbatch/"`, `".git/"`, etc. in
   `engineering.yaml` are not edited. The scan-layer exclusion is purely
   additive; after the change the `"pbatch/pipeline.py"` substring pattern
   only matches the real (ignored-by-policy) `pbatch/pipeline.py`, and the
   build copy is excluded by scope instead of by accident.
5. **Tests**: all 37 existing tests pass unmodified — verified none of them
   call a bare `run()` (which would hit real-repo discovery); they use
   explicit lists or direct `check_*` calls.
6. **Config fallback**: `scan_files` takes `root` as a parameter and
   depends on no config; the zero-config fallback path (`repo_root()` →
   cwd when `engineering.yaml` is absent) works unchanged.
7. **`make ci` / `check-test` discovery**: new tests use the existing
   `unittest.TestCase` idiom and are discovered by both `pytest` and
   `cli.py check-test` (unittest discover on `checks/test_*.py`).

---

## 4. Failure modes and mitigations

| # | Failure mode | Mitigation |
|---|---|---|
| FM1 | An *authored* directory legitimately named `build/`, `dist/`, `logs/`, `.git/`, `__pycache__/`, or `*.egg-info` is silently unscanned by all three gates (false green) | Segment set is narrow and matches the gate's own declared scope (`engineering.yaml:8-11` — generated dirs are not authored code). Tripwire: post-fix scan inventory is pinned at **189** files in migration step M4 — a count regression surfaces immediately. Documented in the R5 header comment. |
| FM2 | Symlinked directories are no longer descended into (`os.walk` default `followlinks=False`; `pathlib.rglob` historically followed them) | Behavior *tightening* (fewer files scanned, never more). Verified: no symlinked dirs in the tracked tree (migration step M0 checks `git ls-files -s` for `120000`). Also a safety win — gates can no longer escape the repo via symlink. |
| FM3 | Someone later "fixes" explicit `files` lists containing excluded-dir paths (expects them skipped) | Documented in requirements R3 + this design: explicit lists are unchanged by design. The scan helper is the *only* scope declaration. |
| FM4 | Case-variant dir names (`Build/`) evade exclusion | By design (R2: exact segment-name match). Case-insensitive matching would cause FM1-class false exclusions on case-insensitive filesystems. Documented. |
| FM5 | Double-exclusion confusion: a path is both scan-excluded and substring-ignored | Additive by construction; no behavior change for authored files. `engineering.yaml` patterns keep their original meaning (upstream-synced files). |
| FM6 | Regression in gate runtime (scan cost) | Impossible to regress: pruning *reduces* the walk (102 build files + dirs never entered). `scan_files` is a single `os.walk` pass + sort, same complexity as today. |
| FM7 | New excluded dir kind appears later (e.g. `.nox/`, `.tox/`, `.venv/`) | Add a segment to `DEFAULT_EXCLUDED_SEGMENTS` — one-line, covered by AC2's fixture pattern (the test's temp tree exercises each kind; extending the tuple extends the test's expectation). |
| FM8 | `root` itself is a symlink or contains one at top level | `repo_root()` is already `Path(__file__).resolve().parent.parent` (resolved). `scan_files` walks from the resolved root; `followlinks=False` only affects dirs *inside* root. No new failure. |
| FM9 | Style gate's non-fatal `WARN:` output changes (regression in noise level) | WARN lines only ever come from authored files (all previously-scanned files were authored or under-budget); scan-layer exclusion removes `build/` paths from output, which was the pollution being fixed. Byte-identical for non-excluded files. |

---

## 5. Migration steps (ordered, each verifiable)

- **M0 — Pre-change inventory (baseline, already measured):**
  `python -c "from pathlib import Path; ..."` → 294 scanned, 102 build/,
  3 dist/. Record in commit message. Also confirm no symlinked dirs:
  `git ls-files -s | grep ^120000` → empty.
- **M1 — Add helper** to `checks/config.py`: constants +
  `scan_files` (code in §2.1). No callers yet — run
  `python -m pytest checks/` → still 37 passed.
- **M2 — Wire the three gates** (§2.2). Run `python cli.py check` → exit 0;
  output must contain no `build/` or `dist/` path, and the existing
  `WARN:` lines must be byte-identical.
- **M3 — Add acceptance tests** (§6): 4 new tests
  (`test_build_dir_excluded_from_run` + `test_run_scan_excludes_generated_dirs`
  ×3). Run `python -m pytest checks/` → **41 passed**; run
  `python cli.py check-test` → exit 0.
- **M4 — Post-fix inventory tripwire:**
  `python -c "import checks.config as c; from checks.config import repo_root; print(len(c.scan_files(repo_root())))"`
  → **189**, and zero paths under any excluded segment. (Also assert
  `len([p for p in scan if any(s in p.parts for s in ('build','dist'))]) == 0`.)
- **M5 — Full gates:** `make ci` (includes check + self-test + quality +
  eval suites); `python quality.py --strict .` (new helper must satisfy
  file/function budgets — it does: ~30 lines, no branching beyond one
  generator).
- **M6 — Optional R5 doc:** one line in the `engineering.yaml` header
  comment: `# Gate scan discovery excludes generated dirs: build/ dist/
  *.egg-info/ .git/ __pycache__/ logs/ (see checks/config.py:scan_files).`
- **M7 — (recommended, out of scope) git hygiene follow-up:** add
  `build/` to `.gitignore` — separate change, noted in the campaign state,
  not part of this direction.

---

## 6. Testable acceptance mapping

| AC (from requirements) | Design element | Concrete test | Verification command |
|---|---|---|---|
| **AC1** — 600-line `.py` under `build/` does not fail `run()` | `scan_files` prune of `build` segment; filesize gate wired to helper | `checks/test_filesize.py::test_build_dir_excluded_from_run`: temp root with `tmp/build/oversized.py` (600 comment lines > 500 budget) + `tmp/ok.py`; monkeypatch `checks.filesize.repo_root = lambda: tmp` and `checks.filesize.get_config = lambda: Config(filesize=FilesizeGate(max_lines=500))`; assert `filesize.run() == 0`. **Negative control** in same test: move oversized file to `tmp/src/oversized.py` → `run() == 1` (proves exclusion, not a broken budget, is what passes) | `python -m pytest checks/test_filesize.py -q` → pass |
| **AC2** — scan lists never contain paths under `build/`, `dist/`, `*.egg-info/`, `.git/`, `__pycache__/`, `logs/` (all three gates) | `scan_files` segment exclusion; each gate's `run()` discovery via helper | `test_run_scan_excludes_generated_dirs` in each of `checks/test_filesize.py`, `checks/test_complexity.py`, `checks/test_style.py`: shared fixture — temp root with one valid short `.py` under each of the six kinds (`build/`, `dist/`, `vendor/pkg.egg-info/` at depth, `.git/`, `__pycache__/`, `logs/`) plus `src/ok.py`; monkeypatch gate module's `repo_root`; assert `run() == 0` **and** `scan_files(tmp)` returns exactly `[tmp/src/ok.py]` (sorted, no excluded path at any depth) | `python -m pytest checks/ -q` → 41 passed |
| **AC3** — repo gates stay green | No behavior change for authored files; explicit-list path untouched | Existing 37 tests + new 4 | `python cli.py check` → exit 0; `python -m pytest checks/` → 41 passed; `python cli.py check-test` → exit 0; `make ci` → pass |

Test hermeticity notes (per requirements R4): the gate modules bind
`repo_root`/`get_config` via `from checks.config import ...`, so the patch
target is the **gate module's namespace** (`checks.filesize.repo_root`),
not the config module — zero repo mutation, no dependence on the real
`build/` tree, no test-order coupling. Tests run under both pytest and the
harness's own unittest discovery.

---

## 7. Risks and out-of-scope follow-ups

- **`build/` not gitignored** (new finding): gate fix removes CI risk, but
  `git add -A` can still stage build outputs. Follow-up: add `build/` to
  `.gitignore` (separate change; out of this direction's scope).
- **`checks/README.md` anchor mismatch** (new finding): R5 doc line goes in
  the `engineering.yaml` header comment, not the README (which documents
  the harness test suite, not scan discovery).
- Explicitly out of scope (unchanged from requirements §4): `root_files.py`
  / root-policy gate, direction #2 (complexity double-counting),
  `engineering.yaml` pattern edits, gate signature changes,
  `scripts/check-*.py` and `quality.py` scanners.
