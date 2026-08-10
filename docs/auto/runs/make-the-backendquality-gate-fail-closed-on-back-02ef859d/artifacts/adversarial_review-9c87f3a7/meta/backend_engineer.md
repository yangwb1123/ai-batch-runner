Single-file stdlib pattern confirmed (checkers never share code except `ui_tokens`). The implementation is complete — here's the final report.

---

## Required Output

### 1. Analysis (module boundary, data ownership, change points, dependency, pattern)

- **Module boundary**: the `backendquality` gate (`scripts/check-backend-quality.py`) owns project-wide backend scanning. The defect: `--dir` mode returned `[]` for missing dirs and exited 0 on 0 scanned files — a vacuous pass exactly for the Go/Java/Rust repos this gate exists for. Fix confined to the script + its registry entry; callers (health.py, advance.py, org gates) untouched.
- **Data ownership**: script owns its CLI contract — exit codes {0,1}, stderr messages, JSON report (byte-compatible; additive `manifest`/`scan_error`/`status` only). Reads target repo manifests read-only.
- **Consistency**: single process; invariant = report JSON, exit code, stderr agree (all fail-closed paths exit 1).
- **Change points**: manifest set (fixed 4-entry table — data, not abstraction), walk-up depth (constant 6), extension mapping (fixed). No speculative axis.
- **Dependency direction**: stdlib only (`json/re/pathlib/sys`), no new imports.
- **Pattern**: **no pattern — a data-driven table + one detection function suffices** (design-patterns.md §5: problem = vacuous 0-file pass; change point = manifest rules are data edits; simple code suffices; complexity added ≈ 90 lines; tested by 15 regression tests). No interface-per-class, no factory.
- **Persistence**: N/A — no entities; `-o` JSON schema kept additive-only.

### 2. Implementation

- **`scripts/check-backend-quality.py`** (499 lines, budgets green): manifest-aware fail-closed `--dir` — missing dir → exit 1 (`scan error: directory not found`); manifest (go.mod/Cargo.toml/pom.xml/package.json, walk-up ≤6 levels, fixed precedence) + 0 files → exit 1; manifest → scan **only** that language (.go / .rs / .java,.kt / .ts,.tsx,.js); no manifest → exit 0 + explicit `no backend manifest detected; gate skipped`. New opt-in `--manifest NAME` (known = forced language; unknown = union exts, still fail-closed). New functions: `detect_manifest`, `resolve_manifest`, `resolve_dir_scan`, `_scan_paths`, `_emit_report`, `_finish`.
- **`pi-batch.yaml`**: registry → `--dir {cwd} --strict --json -o {cwd}/docs/backend/backend-quality.json` (root from layout; report out of `docs/ui/refactor/`).
- **`tests/test_backend_specs.py`**: 13 new + 1 updated regression tests (all 5 ACs + language pinning, walk-up, precedence, forced/unknown `--manifest`, JSON additive fields, relative-path consistency).
- **`examples/backend-engineering-review.yaml`**: comment updated (no more manual `--dir` edit).
- **`.gitignore`**: `docs/backend/`.

### 3. Self-check vs architecture.md §8 / evolution.md §6 + verification

- §8 budgets: file 499 ≤ 500 hard gate ✓ (400-line reference exceeded — analyzed: standalone single-file contract established by all 10 checkers; splitting would break the portable-copy pattern). Functions ≤50 ✓, cognitive ≤20 ✓, cyclomatic ≤15 ✓.
- evolution §6: dependency direction ✓, no god-file ✓, no scope creep (5 files) ✓, compatibility documented (8 invariants, zero caller changes) ✓.
- **Two bugs found and fixed during implementation**: relative `--dir .` never walked up (`Path(".").parent == Path(".")`) — resolved before detection; and a stray `/tmp/go.mod` on this dev box false-closed the AC-5 skip tests — no-manifest tests now use a scratch dir under the repo root (ancestors verified clean by the design stage).

**Commands actually run**: 135 targeted tests passed · `python cli.py check` exit 0 · `quality.py --strict .` OK · `make self-test` + `checks/` 49 passed · `pi-batch health` → 100.0 HEALTHY · `pi-batch check` → 9 gates passed · all 5 ACs reproduced live with exact exit codes/messages.

**Could not run**: `make ci`'s `test` phase — 21 failures in `test_pi_batch.py`/`test_local_fixes.py`, every one `exit 5: Another pi-batch instance is running (pid=1547473 …)` — the running campaign pipeline holds the repo lock; these CLI-spawning tests cannot run concurrently. Unrelated to this change, honestly reported in the completion report (implementation artifact at `docs/auto/runs/make-the-backendquality-gate-fail-closed-on-back-02ef859d/artifacts/implement-f9bb818d/task-1-implementation.md`, completion gate PASS).
